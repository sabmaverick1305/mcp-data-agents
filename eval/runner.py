"""
Evaluation runner — routing accuracy, answer quality, stress testing.

Usage:
    python -m eval.runner                     # run full dataset
    python -m eval.runner --category routing  # filter by category
    python -m eval.runner --stress 20         # 20 concurrent queries stress test
    python -m eval.runner --no-judge          # skip LLM judge (fast routing-only check)
"""
import argparse
import asyncio
import json
import os
import time
from dataclasses import dataclass, field

import anthropic

from eval.dataset import EVAL_DATASET, EvalCase
from eval.judge import QualityScore, score_answer
from observability import QueryTrace
from orchestrator import MCPOrchestrator
from rag.store import RAGStore
from security import check_query
from agents import planner


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class CaseResult:
    case:              EvalCase
    blocked:           bool           = False   # rejected by security layer
    actual_agents:     set[str]       = field(default_factory=set)
    routing_correct:   bool           = False
    answer:            str            = ""
    latency_s:         float          = 0.0
    cost_usd:          float          = 0.0
    keywords_found:    list[str]      = field(default_factory=list)
    keywords_missing:  list[str]      = field(default_factory=list)
    forbidden_found:   list[str]      = field(default_factory=list)
    quality:           QualityScore | None = None
    plan_confidence:   str            = "high"
    error:             str            = ""


@dataclass
class EvalReport:
    total:              int   = 0
    routing_accuracy:   float = 0.0
    avg_quality:        float = 0.0
    avg_latency_s:      float = 0.0
    avg_cost_usd:       float = 0.0
    security_blocked:   int   = 0
    planner_fallbacks:  int   = 0
    keyword_pass_rate:  float = 0.0
    forbidden_fail_rate: float = 0.0
    results:            list[CaseResult] = field(default_factory=list)

    def print_summary(self) -> None:
        print("\n" + "=" * 60)
        print("EVALUATION REPORT")
        print("=" * 60)
        print(f"  Total cases          : {self.total}")
        print(f"  Routing accuracy     : {self.routing_accuracy:.0%}")
        print(f"  Avg quality score    : {self.avg_quality:.2f} / 5.0")
        print(f"  Keyword pass rate    : {self.keyword_pass_rate:.0%}")
        print(f"  Forbidden hit rate   : {self.forbidden_fail_rate:.0%}  (lower is better)")
        print(f"  Avg latency          : {self.avg_latency_s:.2f}s")
        print(f"  Avg cost / query     : ${self.avg_cost_usd:.5f}")
        print(f"  Security blocks      : {self.security_blocked}")
        print(f"  Planner fallbacks    : {self.planner_fallbacks}")
        print("-" * 60)
        for r in self.results:
            status = "✓" if r.routing_correct and not r.forbidden_found else "✗"
            q_score = f"Q={r.quality.overall:.1f}" if r.quality else "Q=n/a"
            print(
                f"  [{status}] [{r.case.category:10s}] {r.case.question[:50]:50s} "
                f"agents={sorted(r.actual_agents)} conf={r.plan_confidence} {q_score}"
            )
            if r.keywords_missing:
                print(f"        MISSING keywords: {r.keywords_missing}")
            if r.forbidden_found:
                print(f"        FORBIDDEN found : {r.forbidden_found}")
            if r.error:
                print(f"        ERROR: {r.error}")
        print("=" * 60 + "\n")

    def to_json(self) -> str:
        return json.dumps({
            "summary": {
                "total":               self.total,
                "routing_accuracy":    self.routing_accuracy,
                "avg_quality":         self.avg_quality,
                "avg_latency_s":       self.avg_latency_s,
                "avg_cost_usd":        self.avg_cost_usd,
                "security_blocked":    self.security_blocked,
                "planner_fallbacks":   self.planner_fallbacks,
                "keyword_pass_rate":   self.keyword_pass_rate,
                "forbidden_fail_rate": self.forbidden_fail_rate,
            },
            "cases": [
                {
                    "question":         r.case.question,
                    "category":         r.case.category,
                    "routing_correct":  r.routing_correct,
                    "actual_agents":    sorted(r.actual_agents),
                    "expected_agents":  sorted(r.case.expected_agents),
                    "plan_confidence":  r.plan_confidence,
                    "quality":          r.quality.to_dict() if r.quality else None,
                    "latency_s":        r.latency_s,
                    "cost_usd":         r.cost_usd,
                    "blocked":          r.blocked,
                    "error":            r.error,
                }
                for r in self.results
            ],
        }, indent=2)


# ── Core evaluation logic ─────────────────────────────────────────────────────

async def _eval_case(
    case: EvalCase,
    client: anthropic.AsyncAnthropic,
    orchestrator: MCPOrchestrator,
    rag: RAGStore,
    use_judge: bool,
) -> CaseResult:
    result = CaseResult(case=case)
    t0 = time.time()

    # Security cases — expect the query to be blocked
    sec = check_query(case.question)
    if not sec:
        result.blocked       = True
        result.routing_correct = (case.expected_agents == set())  # blocked = correct for security cases
        result.latency_s     = round(time.time() - t0, 3)
        return result

    # Security cases that expected a block but weren't blocked
    if case.category == "security" and case.expected_agents == set():
        result.routing_correct = False
        result.error = "Expected security block but query passed check_query."
        result.latency_s = round(time.time() - t0, 3)
        return result

    # Plan the query
    try:
        trace = QueryTrace(question=case.question)
        cached_answer, rag_context = rag.retrieve(case.question)
        plan = await planner.create_plan(
            client, case.question,
            rag_context=rag_context,
            trace=trace,
        )
        result.actual_agents   = set(plan.get("agents", []))
        result.plan_confidence = trace.plan_confidence
        result.routing_correct = (result.actual_agents == case.expected_agents)
    except Exception as e:
        result.error     = f"Planner error: {e}"
        result.latency_s = round(time.time() - t0, 3)
        return result

    # Run the full pipeline to get an answer (best-effort — MCP servers may be down)
    try:
        from agents import benchmark_agent, insight_agent, semantic_agent

        tasks_map       = plan.get("tasks", {})
        parallel_results: dict[str, str] = {}
        sub_traces: dict[str, QueryTrace] = {}

        coros = {}
        for name in ("semantic", "benchmark"):
            if name in plan["agents"]:
                sub_traces[name] = QueryTrace(question=tasks_map.get(name, case.question))
                agent_fn = semantic_agent if name == "semantic" else benchmark_agent
                coros[name] = agent_fn.run(
                    client, orchestrator,
                    tasks_map.get(name, case.question),
                    sub_traces[name],
                )

        if coros:
            gathered = await asyncio.gather(*coros.values(), return_exceptions=True)
            for name, res in zip(coros.keys(), gathered):
                parallel_results[name] = str(res) if isinstance(res, Exception) else res
                if name in sub_traces:
                    trace.merge_agent_trace(name, sub_traces[name])

        if "insight" in plan["agents"]:
            context_blob = "\n\n".join(f"[{k.upper()}]\n{v}" for k, v in parallel_results.items())
            insight_sub  = QueryTrace(question=tasks_map.get("insight", case.question))
            try:
                insight_res = await insight_agent.run(
                    client, orchestrator,
                    tasks_map.get("insight", case.question),
                    context=context_blob,
                    trace=insight_sub,
                )
            except Exception as e:
                insight_res = f"[Agent error: {e}]"
            parallel_results["insight"] = insight_res
            trace.merge_agent_trace("insight", insight_sub)

        # Synthesis
        agent_summaries = "\n\n".join(
            f"## {n.title()} Agent\n{r}" for n, r in parallel_results.items()
        )
        synthesis = (
            f"User question: {case.question}\n\nAgent findings:\n{agent_summaries}\n\n"
            "Synthesize into a clear, concise answer with key numbers. Use markdown."
        )
        syn_response = await client.messages.create(
            model="claude-sonnet-4-6", max_tokens=1024,
            system=(
                "You are a senior data analyst. Synthesize agent findings into a "
                "clear, direct answer with specific numbers. Use markdown."
            ),
            messages=[{"role": "user", "content": synthesis}],
        )
        trace.record_usage(syn_response)
        result.answer   = syn_response.content[0].text
        result.cost_usd = trace.cost

    except Exception as e:
        result.error = f"Pipeline error: {e}"

    # Keyword checks
    answer_lower = result.answer.lower()
    result.keywords_found   = [kw for kw in case.required_keywords if kw.lower() in answer_lower]
    result.keywords_missing = [kw for kw in case.required_keywords if kw.lower() not in answer_lower]
    result.forbidden_found  = [fp for fp in case.forbidden_phrases if fp.lower() in answer_lower]

    # LLM judge
    if use_judge and result.answer and not result.error:
        try:
            result.quality = await score_answer(client, case.question, result.answer)
        except Exception:
            pass

    result.latency_s = round(time.time() - t0, 3)
    return result


async def run_eval(
    categories: list[str] | None = None,
    use_judge: bool = True,
    output_json: str | None = None,
) -> EvalReport:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    client  = anthropic.AsyncAnthropic(api_key=api_key)

    rag = RAGStore(tenant_id="eval")
    rag.seed_domain()

    orchestrator = MCPOrchestrator()
    await orchestrator.start()

    cases = EVAL_DATASET
    if categories:
        cases = [c for c in cases if c.category in categories]

    try:
        tasks   = [_eval_case(c, client, orchestrator, rag, use_judge) for c in cases]
        results = await asyncio.gather(*tasks)
    finally:
        await orchestrator.stop()

    report = _build_report(list(results))
    report.print_summary()

    if output_json:
        with open(output_json, "w") as f:
            f.write(report.to_json())
        print(f"Full report written to {output_json}")

    return report


# ── Stress test ───────────────────────────────────────────────────────────────

async def stress_test(n_concurrent: int = 10) -> dict:
    """
    Fire n_concurrent copies of a representative query simultaneously.
    Measures P50 / P95 latency and whether all responses are non-empty.
    """
    api_key      = os.environ.get("ANTHROPIC_API_KEY", "")
    client       = anthropic.AsyncAnthropic(api_key=api_key)
    orchestrator = MCPOrchestrator()
    await orchestrator.start()

    question = "What is our total revenue and gross margin for 2024?"

    async def single_run(_: int) -> float:
        t0    = time.time()
        trace = QueryTrace(question=question)
        try:
            await planner.create_plan(client, question, trace=trace)
        except Exception:
            pass
        return round(time.time() - t0, 3)

    try:
        t_start  = time.time()
        latencies = await asyncio.gather(*[single_run(i) for i in range(n_concurrent)])
        wall_time = round(time.time() - t_start, 2)
    finally:
        await orchestrator.stop()

    latencies_sorted = sorted(latencies)
    p50 = latencies_sorted[n_concurrent // 2]
    p95 = latencies_sorted[int(n_concurrent * 0.95)]

    return {
        "n_concurrent": n_concurrent,
        "wall_time_s":  wall_time,
        "p50_latency_s": p50,
        "p95_latency_s": p95,
        "min_latency_s": min(latencies_sorted),
        "max_latency_s": max(latencies_sorted),
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_report(results: list[CaseResult]) -> EvalReport:
    total    = len(results)
    if total == 0:
        return EvalReport()

    routing_correct  = sum(1 for r in results if r.routing_correct)
    quality_scores   = [r.quality.overall for r in results if r.quality]
    latencies        = [r.latency_s for r in results]
    costs            = [r.cost_usd for r in results]
    kw_cases         = [r for r in results if r.case.required_keywords]
    forbidden_cases  = [r for r in results if r.case.forbidden_phrases]

    kw_pass = sum(1 for r in kw_cases if not r.keywords_missing) / len(kw_cases) if kw_cases else 1.0
    forb_fail = sum(1 for r in forbidden_cases if r.forbidden_found) / len(forbidden_cases) if forbidden_cases else 0.0

    return EvalReport(
        total              = total,
        routing_accuracy   = round(routing_correct / total, 3),
        avg_quality        = round(sum(quality_scores) / len(quality_scores), 2) if quality_scores else 0.0,
        avg_latency_s      = round(sum(latencies) / total, 2),
        avg_cost_usd       = round(sum(costs) / total, 5),
        security_blocked   = sum(1 for r in results if r.blocked),
        planner_fallbacks  = sum(1 for r in results if r.plan_confidence == "fallback"),
        keyword_pass_rate  = round(kw_pass, 3),
        forbidden_fail_rate = round(forb_fail, 3),
        results            = results,
    )


# ── CLI entry ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run eval suite against the MCP Data Agents pipeline.")
    parser.add_argument("--category", nargs="*", help="Filter by category (routing, quality, edge_case, security)")
    parser.add_argument("--stress",   type=int,  default=0,    help="Run stress test with N concurrent planner calls")
    parser.add_argument("--no-judge", action="store_true",     help="Skip LLM judge (faster, routing-only)")
    parser.add_argument("--output",   type=str,  default=None, help="Write JSON report to this path")
    args = parser.parse_args()

    if args.stress > 0:
        result = asyncio.run(stress_test(args.stress))
        print("\nStress Test Results:")
        for k, v in result.items():
            print(f"  {k}: {v}")
    else:
        asyncio.run(run_eval(
            categories  = args.category,
            use_judge   = not args.no_judge,
            output_json = args.output,
        ))

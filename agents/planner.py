"""
Planner Agent — intent classification and agent routing for the MCP Data Agents pipeline.

The Planner is the first agent invoked on every query. Its job is to read the user's
question and decide which combination of specialist agents should handle it, and what
specific sub-task each agent should execute.

Output contract (JSON):
  {
    "agents":         ["semantic", "benchmark", "insight"],  # subset, never empty
    "tasks":          {"semantic": "...", "benchmark": "...", "insight": "..."},
    "reasoning":      "one sentence explaining the routing decision",
    "synthesis_goal": "what the final synthesised answer should accomplish"
  }

Agent semantics:
  semantic   → Power BI pre-defined KPIs (total_revenue, gross_margin_pct, etc.)
  benchmark  → Tableau dashboards (regional vs target, category rankings)
  insight    → Snowflake ad-hoc SQL for root-cause / drill-down analysis

Retry behaviour:
  Up to 2 attempts. On attempt 2, the validation error from attempt 1 is fed back
  to the model so it can self-correct. If both attempts fail, a safe fallback
  (all three agents, original question as task) is returned so the pipeline never
  stalls.

plan_confidence values written to QueryTrace:
  "high"      — valid JSON plan on first attempt
  "degraded"  — valid JSON only after retry
  "fallback"  — both attempts failed; safe fallback used

Environment:
  MODEL  hardcoded to claude-sonnet-4-6 (low latency, 512 token limit for plan output)
"""
import json

import anthropic

from observability import QueryTrace
from security import wrap_rag_context

MODEL = "claude-sonnet-4-6"
_VALID_AGENTS = {"semantic", "benchmark", "insight"}

_SYSTEM_BASE = """You are a Planner Agent for a multi-source data analytics platform.

Three specialized agents are available:
- "semantic"   → Power BI semantic layer: pre-defined KPIs and metrics
                  (total_revenue, gross_margin_pct, avg_order_value, revenue_growth_mom/yoy, customer_ltv)
- "benchmark"  → Tableau dashboards: regional vs target, category comparisons, top performers, quarterly trends
- "insight"    → Snowflake warehouse: ad-hoc SQL on raw fact and dimension tables

Respond with ONLY a JSON object (no markdown) in this exact shape:
{
  "agents":         ["semantic", "benchmark", "insight"],
  "tasks":          {
    "semantic":  "specific sub-question for the Semantic Agent",
    "benchmark": "specific sub-question for the Benchmark Agent",
    "insight":   "specific sub-question for the Insight Agent"
  },
  "reasoning":      "one sentence explaining your routing decision",
  "synthesis_goal": "what the final answer should accomplish"
}

Only include agents that are genuinely needed."""


def _validate_plan(plan: dict) -> list[str]:
    """Return a list of schema violations. Empty list means plan is valid."""
    errors: list[str] = []

    if not isinstance(plan.get("agents"), list):
        errors.append("'agents' must be a non-empty list.")
        return errors   # can't validate further without agent list

    if not plan["agents"]:
        errors.append("'agents' list is empty — at least one agent is required.")

    unknown = [a for a in plan["agents"] if a not in _VALID_AGENTS]
    if unknown:
        errors.append(f"Unknown agent(s): {unknown}. Valid: {sorted(_VALID_AGENTS)}.")

    if not isinstance(plan.get("tasks"), dict):
        errors.append("'tasks' must be a dict mapping agent name → sub-question.")
    else:
        for agent in plan["agents"]:
            if agent not in plan["tasks"] or not plan["tasks"][agent].strip():
                errors.append(f"Missing or empty task for agent '{agent}'.")

    return errors


def _parse_json(text: str) -> dict | None:
    """Strip optional markdown fences and parse JSON. Returns None on failure."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


_FALLBACK_PLAN = {
    "agents":   list(_VALID_AGENTS),
    "tasks":    {a: "" for a in _VALID_AGENTS},
    "reasoning": "Planner failed after retries — routing to all agents as fallback.",
    "synthesis_goal": "Answer the user's question using all available data sources.",
}


async def create_plan(
    client: anthropic.AsyncAnthropic,
    user_question: str,
    rag_context: str = "",
    conversation_history: list[dict] | None = None,
    trace: QueryTrace | None = None,
) -> dict:
    system = _SYSTEM_BASE

    if conversation_history:
        recent = conversation_history[-3:]
        history_text = "\n".join(
            f"Q: {h['question']}\nA (summary): {h['answer'][:200]}…"
            for h in recent
        )
        system += f"\n\nRecent conversation (use for follow-up resolution):\n{history_text}"

    if rag_context:
        system += f"\n\n{wrap_rag_context(rag_context)}"

    last_error: str = ""

    for attempt in range(2):
        prompt = user_question
        if attempt == 1 and last_error:
            # Feed the validation error back so the model can self-correct
            prompt = (
                f"{user_question}\n\n"
                f"[PLANNER RETRY] Your previous response had these issues:\n{last_error}\n"
                "Respond with a valid JSON plan only."
            )

        response = await client.messages.create(
            model=MODEL,
            max_tokens=512,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )

        if trace:
            trace.record_usage(response)

        raw = response.content[0].text
        plan = _parse_json(raw)

        if plan is None:
            last_error = f"Response was not valid JSON. Got:\n{raw[:300]}"
            if trace:
                trace.plan_confidence = "degraded"
            continue

        errors = _validate_plan(plan)
        if errors:
            last_error = "\n".join(errors)
            if trace:
                trace.plan_confidence = "degraded"
            continue

        # Fill in missing task strings with the original question as safe default
        for agent in plan["agents"]:
            if not plan["tasks"].get(agent, "").strip():
                plan["tasks"][agent] = user_question

        if attempt == 0 and trace:
            trace.plan_confidence = "high"
        elif trace:
            trace.plan_confidence = "degraded"

        return plan

    # Both attempts failed — use safe fallback
    if trace:
        trace.plan_confidence = "fallback"
    fallback = dict(_FALLBACK_PLAN)
    fallback["tasks"] = {a: user_question for a in _VALID_AGENTS}
    return fallback

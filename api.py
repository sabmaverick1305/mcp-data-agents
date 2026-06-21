"""
FastAPI application — REST and SSE streaming layer for the MCP Data Agents system.

This module is the HTTP boundary of the system. It owns:
  - Application lifecycle (startup database seed, singleton creation, graceful shutdown)
  - All 17 API endpoints (query, ingest, cache management, cost, Redis, observability)
  - The core query pipeline (_run_pipeline) wiring auth → security → cache → plan →
    parallel agents → insight → synthesis → cache write → cost ledger → metrics
  - SSE streaming variant of the pipeline (query_stream endpoint)
  - Per-tenant RAG store cache (_rag_cache dict keyed by tenant_id)

Singletons initialised at startup (stored in _state):
  client       AsyncAnthropic or AsyncAnthropicBedrock (from bedrock_client)
  orchestrator MCPOrchestrator — manages stdio MCP server processes
  ledger       CostLedger — SQLite cost attribution
  redis        RedisMemory — L1 cache + session history + rate limit + audit

Auth is injected via FastAPI's Depends(require_auth) on every protected endpoint,
which returns a TenantContext used throughout the pipeline for namespace isolation.

Environment variables consumed here:
  (all auth/redis/chroma/llm vars are delegated to their respective modules)
  SEED_MODE    demo | real — passed to seed_database() on first run
"""
import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, Request, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agents import benchmark_agent, insight_agent, planner, semantic_agent
from auth import require_auth
from bedrock_client import backend_label, default_model, make_client
from logging_config import bind_request_context, get_logger
from cost_ledger import CostLedger
from data.seed import DB_PATH, seed_database
from cache_registry import CacheLayer, CacheRegistry
from context_engine import ContextAssemblyEngine
from long_term_memory import LongTermMemory
from observability import ESTIMATED_PIPELINE_COST_USD, QueryTrace
from orchestrator import MCPOrchestrator
from query_intelligence import QueryIntelligence
from rag.ingest import ingest_text, list_sources, query_documents
from rag.store import RAGStore
from redis_memory import RedisMemory
from state_manager import PipelineState, WorkflowStateStore
from metrics import (
    metrics_response, record_security_block, record_trace,
    update_cache_size, update_mcp_servers,
    record_cache_hit, record_cache_miss, record_partial_reuse,
    record_cache_invalidation, record_context_sources,
)
from security import check_ingest, check_pii, check_query
from tenant import TenantContext, apply_to_trace, get_tenant_from_request

MODEL = default_model()
log   = get_logger(__name__)

_SYNTHESIS_SYSTEM = (
    "You are a senior data analyst presenting findings to a business audience. "
    "Lead with the key insight, support every claim with specific numbers, "
    "use markdown headers and bullet points, and include a data table where it aids clarity. "
    "Be direct — avoid filler phrases."
)

# Cached content block for the synthesis system prompt.
# Static text never changes between calls, so Anthropic's prompt cache
# avoids re-encoding it on every synthesis request.
_SYNTHESIS_SYSTEM_BLOCK = [
    {"type": "text", "text": _SYNTHESIS_SYSTEM, "cache_control": {"type": "ephemeral"}}
]

# ── Shared singletons (tenant-agnostic) ───────────────────────────────────────
_state: dict = {}
_rag_cache: dict[str, RAGStore] = {}   # keyed by tenant_id


def _get_rag(tenant_id: str) -> RAGStore:
    if tenant_id not in _rag_cache:
        store = RAGStore(tenant_id=tenant_id)
        store.seed_domain()
        _rag_cache[tenant_id] = store
    return _rag_cache[tenant_id]


async def _get_history(tenant_id: str) -> list[dict]:
    redis_mem: RedisMemory = _state.get("redis")
    if redis_mem and redis_mem.available:
        history = await redis_mem.get_history(tenant_id)
        if history:
            return history
    # fallback to JSON-backed RAG history
    return _state.setdefault(f"history_{tenant_id}", _get_rag(tenant_id).load_history())


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.path.exists(DB_PATH):
        seed_database()

    _state["client"] = make_client()
    log.info("startup.complete", llm_backend=backend_label())
    _state["orchestrator"] = MCPOrchestrator()
    _state["ledger"]       = CostLedger()

    redis_mem = RedisMemory()
    await redis_mem.connect()        # fail-open: unavailable Redis doesn't abort startup
    _state["redis"] = redis_mem

    # STATE & MEMORY LAYER — initialise after Redis connects so WorkflowStateStore
    # can share the same connection pool.
    _state["workflow"]  = WorkflowStateStore(redis_mem.client)
    _state["ltm"]       = LongTermMemory()

    # QUERY INTELLIGENCE + CACHE REGISTRY — stateless, constructed once
    _state["qi"]             = QueryIntelligence()
    _state["cache_registry"] = CacheRegistry(redis_mem)
    _state["context_engine"] = ContextAssemblyEngine()

    await _state["orchestrator"].start()
    update_mcp_servers(len(_state["orchestrator"].sessions))

    _get_rag("default")

    yield

    await _state["orchestrator"].stop()
    await _state["redis"].close()


app = FastAPI(title="MCP Data Agents API", version="2.0.0", lifespan=lifespan)


# ── Schema ─────────────────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    question: str
    user_id:  str | None = None
    team_id:  str | None = None


class QueryResponse(BaseModel):
    question:  str
    answer:    str
    trace:     dict
    cached:    bool


# ── Core pipeline ─────────────────────────────────────────────────────────────

async def _run_pipeline(
    ctx: TenantContext,
    question: str,
    user_id: str | None = None,
    team_id: str | None = None,
    prefilled_results: dict[str, str] | None = None,  # for replay: skip already-done agents
) -> tuple[str, QueryTrace]:
    tenant       = ctx
    client       = _state["client"]
    orchestrator = _state["orchestrator"]
    ledger       = _state["ledger"]
    redis_mem: RedisMemory         = _state.get("redis")
    workflow:   WorkflowStateStore = _state.get("workflow")
    ltm:        LongTermMemory     = _state.get("ltm")
    rag          = _get_rag(tenant.tenant_id)
    history      = await _get_history(tenant.tenant_id)

    # Generate a session ID for workflow state tracking
    session_id = uuid.uuid4().hex[:12]

    trace = QueryTrace(question=question, session_id=session_id)
    apply_to_trace(tenant, trace)
    if user_id:
        trace.user_id = user_id
    if team_id:
        trace.team_id = team_id

    bind_request_context(tenant.tenant_id, user_id or tenant.user_id)
    log.info("query.start", question_len=len(question), tenant_id=tenant.tenant_id,
             session_id=session_id)

    # ── Intelligence layer singletons ─────────────────────────────────────────
    qi:             QueryIntelligence   = _state["qi"]
    cache_reg:      CacheRegistry       = _state["cache_registry"]
    ctx_engine:     ContextAssemblyEngine = _state["context_engine"]

    # ── Query Intelligence — classify before any cache lookup ─────────────────
    intent = qi.analyze(question)
    tid    = tenant.tenant_id

    if workflow:
        await workflow.begin(session_id, tid, question)

    current_stage = "cache_check"
    try:
        # ── Cache Registry — unified L1 + L2 lookup with intent-aware thresholds
        lookup = await cache_reg.lookup(tid, question, intent, rag)

        if lookup.hit:
            # Full cache hit (Redis L1 or ChromaDB L2)
            trace.cache_hit        = True
            trace.avoided_cost_usd = ledger.rolling_avg_cost(tid)
            ledger.record(trace)
            record_trace(trace)
            record_cache_hit(lookup.layer.value, intent.query_type.value, tid)
            if workflow:
                await workflow.close(session_id, tid, lookup.answer, [])
            return lookup.answer, trace

        # Cache miss — record and proceed to full pipeline
        record_cache_miss(intent.query_type.value, tid)

        # Partial agent reuse: some agents may have cached results from a similar prior query
        partial_from_registry = lookup.agent_results   # {agent_name: result}
        if partial_from_registry:
            record_partial_reuse(tid)

        # Combine partial results from registry with any explicitly prefilled (replay)
        parallel_results: dict[str, str] = {**(prefilled_results or {}), **partial_from_registry}

        # ── RAG context using intent-aware thresholds ─────────────────────────
        rag_context = await cache_reg.get_rag_context(question, intent, rag)

        # ── LTM context ───────────────────────────────────────────────────────
        ltm_context = ltm.get_context_for_planner(tid, user_id, question) if ltm else ""

        # ── Document chunks ───────────────────────────────────────────────────
        doc_chunks = query_documents(question, tenant_id=tid)

        # ── Context Assembly Engine — prioritized, budget-enforced context ────
        ctx_pkg = ctx_engine.assemble(
            question,
            rag_context=rag_context,
            ltm_context=ltm_context,
            history=history,
            doc_chunks=doc_chunks,
        )
        record_context_sources(ctx_pkg.sources_used, tid)

        # ── Plan ─────────────────────────────────────────────────────────────
        current_stage = "planning"
        if workflow:
            await workflow.set_state(session_id, tid, PipelineState.PLANNING)

        plan = await planner.create_plan(
            client, question,
            rag_context=ctx_pkg.to_planner_string(),
            conversation_history=history[-3:] if history else [],
            trace=trace,
        )
        trace.agents_invoked = plan["agents"]
        tasks_map = plan.get("tasks", {})

        # ── Parallel agents (semantic + benchmark) ────────────────────────────
        current_stage = "agents_running"
        if workflow:
            await workflow.set_state(session_id, tid, PipelineState.AGENTS_RUNNING)

        sub_traces: dict[str, QueryTrace] = {}
        coros: dict[str, asyncio.coroutines] = {}

        for name in ("semantic", "benchmark"):
            if name in plan["agents"] and name not in parallel_results:
                sub_traces[name] = QueryTrace(question=tasks_map.get(name, question))
                agent_fn = semantic_agent if name == "semantic" else benchmark_agent
                coros[name] = agent_fn.run(
                    client, orchestrator, tasks_map.get(name, question), sub_traces[name]
                )

        if coros:
            gathered = await asyncio.gather(*coros.values())
            for name, result in zip(coros.keys(), gathered):
                parallel_results[name] = result
                trace.merge_agent_trace(name, sub_traces[name])
                if workflow:
                    sub = sub_traces[name]
                    await workflow.checkpoint_agent(
                        session_id, tid, name, result,
                        sub.input_tokens, sub.output_tokens,
                    )
                # Store in agent-level partial cache for future reuse
                await cache_reg.store_agent_result(
                    tid, name, intent.normalized, result
                )

        # ── Insight agent (sequential — uses parallel results as context) ─────
        if "insight" in plan["agents"] and "insight" not in parallel_results:
            context_blob = "\n\n".join(
                f"[{k.upper()}]\n{v}" for k, v in parallel_results.items()
            )
            insight_sub = QueryTrace(question=tasks_map.get("insight", question))
            insight_result = await insight_agent.run(
                client, orchestrator, tasks_map.get("insight", question),
                context=context_blob, trace=insight_sub,
            )
            if len(insight_result) < 120 or insight_result.startswith("[Agent error"):
                broader = (
                    f"Try a broader analysis for: {tasks_map.get('insight', question)}. "
                    "Use wider date ranges or remove restrictive filters."
                )
                insight_result = await insight_agent.run(
                    client, orchestrator, broader, context=context_blob, trace=insight_sub)
            parallel_results["insight"] = insight_result
            trace.merge_agent_trace("insight", insight_sub)
            if workflow:
                await workflow.checkpoint_agent(
                    session_id, tid, "insight", insight_result,
                    insight_sub.input_tokens, insight_sub.output_tokens,
                )
            await cache_reg.store_agent_result(
                tid, "insight", intent.normalized, insight_result
            )

        # ── Synthesis ─────────────────────────────────────────────────────────
        current_stage = "synthesizing"
        if workflow:
            await workflow.set_state(session_id, tid, PipelineState.SYNTHESIZING)

        agent_summaries = "\n\n".join(
            f"## {n.title()} Agent\n{r}" for n, r in parallel_results.items()
        )
        synthesis = (
            f"User question: {question}\n\nAgent findings:\n{agent_summaries}\n\n"
            "Synthesize into a clear, concise answer with key numbers. Use markdown."
        )

        response = await client.messages.create(
            model=MODEL, max_tokens=2048,
            system=_SYNTHESIS_SYSTEM_BLOCK,
            messages=[{"role": "user", "content": synthesis}],
        )
        trace.record_usage(response)
        answer = response.content[0].text

        if check_pii(answer).allowed:
            rag.store_qa(question, answer, trace.agents_invoked)
            # Write-through via CacheRegistry (Redis L1 + agent cache)
            await cache_reg.register(
                tid, question, intent.normalized, answer,
                trace.agents_invoked, parallel_results,
            )
        if redis_mem:
            await redis_mem.append_history(tid, question, answer)
        history.append({"question": question, "answer": answer})
        rag.save_history(history)

        if ltm:
            ltm.store_analysis(tid, question, answer[:500], trace.agents_invoked)

        ledger.record(trace)
        record_trace(trace)
        update_cache_size(trace.tenant_id, rag.stats()["qa_entries"])

        if workflow:
            await workflow.close(session_id, tid, answer, trace.agents_invoked)

        log.info(
            "query.complete",
            session_id=session_id,
            query_type=intent.query_type.value,
            latency_s=round(trace.latency, 2),
            cost_usd=round(trace.cost, 5),
            cache_hit=trace.cache_hit,
            agents=trace.agents_invoked,
            partial_reuse=list(partial_from_registry.keys()),
        )
        return answer, trace

    except Exception as exc:
        if workflow:
            await workflow.mark_failed(session_id, tid, current_stage, str(exc))
        raise


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    orch = _state.get("orchestrator")
    return {
        "status":      "ok",
        "mcp_servers": list(orch.sessions.keys()) if orch else [],
    }


@app.get("/metrics")
async def metrics():
    """Prometheus scrape endpoint. Point prometheus.yml scrape_configs here."""
    return metrics_response()


@app.get("/stats")
async def stats(ctx: TenantContext = Depends(require_auth)):
    rag = _get_rag(ctx.tenant_id)
    return {
        "rag":     rag.stats(),
        "history": len(await _get_history(ctx.tenant_id)),
        "sources": list_sources(tenant_id=ctx.tenant_id),
    }


@app.get("/history")
async def history(ctx: TenantContext = Depends(require_auth)):
    return _get_rag(ctx.tenant_id).list_qa()


@app.post("/query", response_model=QueryResponse)
async def query(
    req: QueryRequest,
    request: Request,
    ctx: TenantContext = Depends(require_auth),
):
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty.")
    sec = check_query(req.question)
    if not sec:
        record_security_block("prompt_injection")
        raise HTTPException(status_code=400, detail=f"Query rejected: {'; '.join(sec.violations)}")
    redis_mem: RedisMemory = _state.get("redis")
    if redis_mem and not await redis_mem.check_rate_limit(ctx.tenant_id):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again in a minute.")

    answer, trace = await _run_pipeline(
        ctx, req.question, user_id=req.user_id, team_id=req.team_id
    )
    return QueryResponse(
        question=req.question, answer=answer,
        trace=trace.to_dict(), cached=trace.cache_hit,
    )


@app.get("/query/stream")
async def query_stream(
    q: str,
    request: Request,
    ctx: TenantContext = Depends(require_auth),   # auth validated before generator starts
):
    """SSE streaming endpoint."""
    if not q.strip():
        raise HTTPException(status_code=400, detail="q param cannot be empty.")
    sec = check_query(q)
    if not sec:
        record_security_block("prompt_injection")
        raise HTTPException(status_code=400, detail=f"Query rejected: {'; '.join(sec.violations)}")

    async def event_generator():
        tenant       = ctx           # already authenticated + tenant-validated
        client       = _state["client"]
        orchestrator = _state["orchestrator"]
        ledger       = _state["ledger"]
        redis_mem: RedisMemory = _state.get("redis")
        rag          = _get_rag(tenant.tenant_id)
        history      = await _get_history(tenant.tenant_id)

        trace = QueryTrace(question=q)
        apply_to_trace(tenant, trace)

        if redis_mem and not await redis_mem.check_rate_limit(tenant.tenant_id):
            yield f"data: {json.dumps({'type': 'error', 'detail': 'Rate limit exceeded'})}\n\n"
            return

        # Redis L1 check
        if redis_mem and redis_mem.available:
            exact = await redis_mem.get_exact(tenant.tenant_id, q)
            if exact:
                trace.cache_hit        = True
                trace.avoided_cost_usd = ledger.rolling_avg_cost(tenant.tenant_id)
                ledger.record(trace)
                record_trace(trace)
                yield f"data: {json.dumps({'type': 'cached', 'source': 'redis_l1', 'text': exact})}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'trace': trace.to_dict()})}\n\n"
                return

        # ChromaDB L2 check
        cached_answer, rag_context = rag.retrieve(q)
        if cached_answer:
            trace.cache_hit        = True
            trace.avoided_cost_usd = ledger.rolling_avg_cost(tenant.tenant_id)
            ledger.record(trace)
            record_trace(trace)
            yield f"data: {json.dumps({'type': 'cached', 'source': 'chroma_l2', 'text': cached_answer})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'trace': trace.to_dict()})}\n\n"
            return

        doc_chunks = query_documents(q, tenant_id=tenant.tenant_id)
        if doc_chunks:
            rag_context += "\n\n" + "\n\n".join(
                f"[Doc: {c['source']}]\n{c['chunk']}" for c in doc_chunks
            )

        plan = await planner.create_plan(
            client, q, rag_context=rag_context,
            conversation_history=history, trace=trace)
        trace.agents_invoked = plan["agents"]
        tasks_map = plan.get("tasks", {})
        yield f"data: {json.dumps({'type': 'plan', 'agents': plan['agents'], 'confidence': trace.plan_confidence})}\n\n"

        sub_traces: dict[str, QueryTrace] = {}
        coros: dict[str, asyncio.coroutines] = {}
        for name in ("semantic", "benchmark"):
            if name in plan["agents"]:
                sub_traces[name] = QueryTrace(question=tasks_map.get(name, q))
                agent_fn = semantic_agent if name == "semantic" else benchmark_agent
                coros[name] = agent_fn.run(
                    client, orchestrator, tasks_map.get(name, q), sub_traces[name])

        parallel_results: dict[str, str] = {}
        if coros:
            gathered = await asyncio.gather(*coros.values())
            for name, result in zip(coros.keys(), gathered):
                parallel_results[name] = result
                trace.merge_agent_trace(name, sub_traces[name])
                yield f"data: {json.dumps({'type': 'agent', 'name': name, 'result': result})}\n\n"

        if "insight" in plan["agents"]:
            context_blob = "\n\n".join(f"[{k.upper()}]\n{v}" for k, v in parallel_results.items())
            insight_sub  = QueryTrace(question=tasks_map.get("insight", q))
            insight_result = await insight_agent.run(
                client, orchestrator, tasks_map.get("insight", q),
                context=context_blob, trace=insight_sub)
            parallel_results["insight"] = insight_result
            trace.merge_agent_trace("insight", insight_sub)
            yield f"data: {json.dumps({'type': 'agent', 'name': 'insight', 'result': insight_result})}\n\n"

        agent_summaries = "\n\n".join(f"## {n.title()}\n{r}" for n, r in parallel_results.items())
        synthesis = (
            f"User question: {q}\n\nAgent findings:\n{agent_summaries}\n\n"
            "Synthesize into a clear, concise answer. Use markdown."
        )

        answer_buf = ""
        async with client.messages.stream(
            model=MODEL, max_tokens=2048,
            system=_SYNTHESIS_SYSTEM_BLOCK,
            messages=[{"role": "user", "content": synthesis}],
        ) as stream:
            async for text in stream.text_stream:
                answer_buf += text
                yield f"data: {json.dumps({'type': 'text', 'delta': text})}\n\n"
            final = await stream.get_final_message()
            trace.record_usage(final)

        if check_pii(answer_buf).allowed:
            rag.store_qa(q, answer_buf, trace.agents_invoked)
            if redis_mem:
                await redis_mem.set_exact(tenant.tenant_id, q, answer_buf, trace.agents_invoked)
        if redis_mem:
            await redis_mem.append_history(tenant.tenant_id, q, answer_buf)
        history.append({"question": q, "answer": answer_buf})
        rag.save_history(history)
        ledger.record(trace)
        record_trace(trace)
        update_cache_size(trace.tenant_id, rag.stats()["qa_entries"])
        yield f"data: {json.dumps({'type': 'done', 'trace': trace.to_dict()})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ── Ingest ────────────────────────────────────────────────────────────────────

@app.post("/ingest")
async def ingest(file: UploadFile, ctx: TenantContext = Depends(require_auth)):
    if not file.filename.endswith(".txt"):
        raise HTTPException(status_code=400, detail="Only .txt files supported via API.")
    content = (await file.read()).decode("utf-8")
    sec = check_ingest(content, source=file.filename)
    if not sec:
        raise HTTPException(status_code=400, detail=f"Document rejected: {'; '.join(sec.violations)}")
    n = ingest_text(content, source=file.filename, tenant_id=ctx.tenant_id)
    return {"source": file.filename, "chunks_ingested": n, "tenant_id": ctx.tenant_id}


# ── Cache management ──────────────────────────────────────────────────────────

@app.get("/cache/observability")
async def cache_observability(ctx: TenantContext = Depends(require_auth)):
    """
    Unified cache status and observability report across all layers.

    Returns Redis L1 health, ChromaDB L2 entry counts, per-agent cache key counts,
    and a reference to Prometheus metrics for hit/miss rates and cost savings.
    """
    cache_reg: CacheRegistry = _state.get("cache_registry")
    rag        = _get_rag(ctx.tenant_id)
    if not cache_reg:
        return {"status": "cache_registry_unavailable"}

    status = await cache_reg.status(ctx.tenant_id, rag)
    status["prometheus_metrics"] = "/metrics"
    status["metric_names"] = [
        "mcp_agents_cache_hits_total",
        "mcp_agents_cache_misses_total",
        "mcp_agents_partial_cache_reuse_total",
        "mcp_agents_cache_invalidations_total",
        "mcp_agents_avoided_cost_usd_total",
        "mcp_agents_context_sources_total",
    ]
    return status


@app.delete("/cache")
async def clear_cache_entry(question: str, ctx: TenantContext = Depends(require_auth)):
    qi        = _state.get("qi")
    cache_reg: CacheRegistry = _state.get("cache_registry")
    rag       = _get_rag(ctx.tenant_id)
    normalized = qi.normalize(question) if qi else question
    if cache_reg:
        await cache_reg.invalidate(ctx.tenant_id, question, normalized, rag, reason="manual")
    else:
        rag.flag_bad(question)
    record_cache_invalidation("manual", ctx.tenant_id)
    return {"deleted": question, "tenant_id": ctx.tenant_id}


@app.delete("/cache/bulk")
async def rollback_cache(cutoff_ts: float, ctx: TenantContext = Depends(require_auth)):
    """Remove all cache entries created at or after cutoff_ts (Unix timestamp)."""
    deleted = _get_rag(ctx.tenant_id).flag_bad_since(cutoff_ts)
    return {"deleted": deleted, "cutoff_ts": cutoff_ts, "tenant_id": ctx.tenant_id}


@app.get("/cache/health")
async def cache_health(window_secs: float = 3600.0, ctx: TenantContext = Depends(require_auth)):
    return _get_rag(ctx.tenant_id).cache_health(window_secs)


# ── Cost endpoints ────────────────────────────────────────────────────────────

@app.get("/costs/summary")
async def cost_summary(
    start_ts: float | None = None,
    end_ts:   float | None = None,
    ctx: TenantContext = Depends(require_auth),
):
    return _state["ledger"].summary(ctx.tenant_id, start_ts, end_ts)


@app.get("/costs/by-team")
async def cost_by_team(
    start_ts: float | None = None,
    end_ts:   float | None = None,
    ctx: TenantContext = Depends(require_auth),
):
    return _state["ledger"].by_team(ctx.tenant_id, start_ts, end_ts)


@app.get("/costs/cache-roi")
async def cost_cache_roi(ctx: TenantContext = Depends(require_auth)):
    return _state["ledger"].cache_roi(ctx.tenant_id)


@app.get("/costs/by-agent")
async def cost_by_agent(
    start_ts: float | None = None,
    end_ts:   float | None = None,
    ctx: TenantContext = Depends(require_auth),
):
    return _state["ledger"].by_agent(ctx.tenant_id, start_ts, end_ts)


# ── Redis endpoints ───────────────────────────────────────────────────────────

@app.get("/redis/health")
async def redis_health():
    """Redis connection status — intentionally unauthenticated (ops/infra use)."""
    redis_mem: RedisMemory = _state.get("redis")
    if not redis_mem:
        return {"status": "not_initialized"}
    return await redis_mem.health()


@app.get("/redis/audit")
async def redis_audit(limit: int = 50, ctx: TenantContext = Depends(require_auth)):
    redis_mem: RedisMemory = _state.get("redis")
    if not redis_mem or not redis_mem.available:
        return {"events": [], "status": "unavailable"}
    return {"events": await redis_mem.get_audit_log(ctx.tenant_id, limit)}


@app.get("/redis/rate")
async def redis_rate(ctx: TenantContext = Depends(require_auth)):
    redis_mem: RedisMemory = _state.get("redis")
    usage = await redis_mem.get_rate_usage(ctx.tenant_id) if redis_mem else 0
    return {"tenant_id": ctx.tenant_id, "requests_this_minute": usage, "limit": 60}


@app.delete("/redis/cache")
async def redis_invalidate(question: str, ctx: TenantContext = Depends(require_auth)):
    redis_mem: RedisMemory = _state.get("redis")
    if redis_mem:
        await redis_mem.invalidate_exact(ctx.tenant_id, question)
    return {"invalidated": question, "tenant_id": ctx.tenant_id}


# ── Session / Workflow State endpoints ────────────────────────────────────────

@app.get("/sessions")
async def list_sessions(limit: int = 20, ctx: TenantContext = Depends(require_auth)):
    """List the most recent pipeline sessions for the authenticated tenant (newest first)."""
    workflow: WorkflowStateStore = _state.get("workflow")
    if not workflow or not workflow.available:
        return {"sessions": [], "status": "workflow_store_unavailable"}
    sessions = await workflow.list_sessions(ctx.tenant_id, limit=limit)
    return {"sessions": sessions, "tenant_id": ctx.tenant_id}


@app.get("/sessions/{session_id}")
async def get_session(session_id: str, ctx: TenantContext = Depends(require_auth)):
    """Return the full workflow state and completed checkpoints for a session."""
    workflow: WorkflowStateStore = _state.get("workflow")
    if not workflow or not workflow.available:
        raise HTTPException(status_code=503, detail="Workflow store unavailable.")
    state = await workflow.get(session_id, ctx.tenant_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found.")
    checkpoints = await workflow.get_checkpointed_results(session_id, ctx.tenant_id)
    return {
        "workflow":    state,
        "checkpoints": {k: v[:300] + "…" for k, v in checkpoints.items()},
    }


@app.post("/sessions/{session_id}/replay")
async def replay_session(
    session_id: str,
    ctx: TenantContext = Depends(require_auth),
):
    """
    Replay a failed or incomplete session.

    Loads checkpointed agent results from the original session and skips
    re-running agents that already completed. Only synthesis (or missing agents)
    are re-executed. Returns the same QueryResponse shape as POST /query.
    """
    workflow: WorkflowStateStore = _state.get("workflow")
    if not workflow or not workflow.available:
        raise HTTPException(status_code=503, detail="Workflow store unavailable.")

    state = await workflow.get(session_id, ctx.tenant_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found.")

    checkpoints = await workflow.get_checkpointed_results(session_id, ctx.tenant_id)
    question    = state["question"]

    answer, trace = await _run_pipeline(
        ctx, question,
        user_id=None,
        prefilled_results=checkpoints,
    )
    return QueryResponse(
        question=question,
        answer=answer,
        trace=trace.to_dict(),
        cached=trace.cache_hit,
    )


# ── Long-Term Memory endpoints ────────────────────────────────────────────────

@app.get("/memory/analyses")
async def memory_analyses(limit: int = 20, ctx: TenantContext = Depends(require_auth)):
    """List the most recent analyses stored in long-term memory for this tenant."""
    ltm: LongTermMemory = _state.get("ltm")
    if not ltm:
        return {"analyses": [], "status": "ltm_unavailable"}
    return {"analyses": ltm.list_analyses(ctx.tenant_id, limit=limit)}


@app.get("/memory/patterns")
async def memory_patterns(ctx: TenantContext = Depends(require_auth)):
    """List all domain patterns accumulated for this tenant, ranked by evidence."""
    ltm: LongTermMemory = _state.get("ltm")
    if not ltm:
        return {"patterns": [], "status": "ltm_unavailable"}
    return {"patterns": ltm.get_all_patterns(ctx.tenant_id)}


@app.post("/memory/patterns")
async def add_pattern(
    pattern: str,
    evidence_count: int = 1,
    ctx: TenantContext = Depends(require_auth),
):
    """Manually record or reinforce a domain pattern observation."""
    ltm: LongTermMemory = _state.get("ltm")
    if not ltm:
        raise HTTPException(status_code=503, detail="Long-term memory unavailable.")
    ltm.store_pattern(ctx.tenant_id, pattern, evidence_count)
    return {"stored": pattern, "evidence_count": evidence_count}


@app.delete("/memory/patterns")
async def delete_pattern(pattern: str, ctx: TenantContext = Depends(require_auth)):
    """Remove a domain pattern from long-term memory."""
    ltm: LongTermMemory = _state.get("ltm")
    if not ltm:
        raise HTTPException(status_code=503, detail="Long-term memory unavailable.")
    deleted = ltm.delete_pattern(ctx.tenant_id, pattern)
    if not deleted:
        raise HTTPException(status_code=404, detail="Pattern not found.")
    return {"deleted": pattern}


@app.get("/memory/preferences")
async def get_preferences(
    user_id: str,
    ctx: TenantContext = Depends(require_auth),
):
    """Return all stored preferences for a specific user."""
    ltm: LongTermMemory = _state.get("ltm")
    if not ltm:
        return {"preferences": {}}
    return {"preferences": ltm.get_preferences(ctx.tenant_id, user_id), "user_id": user_id}


class PreferenceRequest(BaseModel):
    user_id: str
    key:     str
    value:   str


@app.put("/memory/preferences")
async def set_preference(
    req: PreferenceRequest,
    ctx: TenantContext = Depends(require_auth),
):
    """Upsert a user preference (e.g. answer_depth=detailed, preferred_region=APAC)."""
    ltm: LongTermMemory = _state.get("ltm")
    if not ltm:
        raise HTTPException(status_code=503, detail="Long-term memory unavailable.")
    ltm.store_preference(ctx.tenant_id, req.user_id, req.key, req.value)
    return {"stored": {req.key: req.value}, "user_id": req.user_id}


@app.delete("/memory/preferences")
async def delete_preference(
    user_id: str,
    key: str,
    ctx: TenantContext = Depends(require_auth),
):
    """Remove a specific user preference key."""
    ltm: LongTermMemory = _state.get("ltm")
    if not ltm:
        raise HTTPException(status_code=503, detail="Long-term memory unavailable.")
    ltm.delete_preference(ctx.tenant_id, user_id, key)
    return {"deleted": key, "user_id": user_id}

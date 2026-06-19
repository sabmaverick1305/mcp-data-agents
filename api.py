"""FastAPI layer — REST + SSE streaming for the MCP Data Agents system."""
import asyncio
import json
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agents import benchmark_agent, insight_agent, planner, semantic_agent
from auth import require_auth
from bedrock_client import backend_label, default_model, make_client
from logging_config import bind_request_context, get_logger
from cost_ledger import CostLedger
from data.seed import DB_PATH, seed_database
from observability import ESTIMATED_PIPELINE_COST_USD, QueryTrace
from orchestrator import MCPOrchestrator
from rag.ingest import ingest_text, list_sources, query_documents
from rag.store import RAGStore
from redis_memory import RedisMemory
from metrics import (
    metrics_response, record_security_block, record_trace,
    update_cache_size, update_mcp_servers,
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
) -> tuple[str, QueryTrace]:
    tenant       = ctx
    client       = _state["client"]
    orchestrator = _state["orchestrator"]
    ledger       = _state["ledger"]
    redis_mem: RedisMemory = _state.get("redis")
    rag          = _get_rag(tenant.tenant_id)
    history      = await _get_history(tenant.tenant_id)

    trace = QueryTrace(question=question)
    apply_to_trace(tenant, trace)
    if user_id:
        trace.user_id = user_id
    if team_id:
        trace.team_id = team_id

    bind_request_context(tenant.tenant_id, user_id or tenant.user_id)
    log.info("query.start", question_len=len(question), tenant_id=tenant.tenant_id)

    # ── Redis L1 exact cache ──────────────────────────────────────────────────
    if redis_mem and redis_mem.available:
        exact = await redis_mem.get_exact(tenant.tenant_id, question)
        if exact:
            trace.cache_hit        = True
            trace.avoided_cost_usd = ledger.rolling_avg_cost(tenant.tenant_id)
            ledger.record(trace)
            record_trace(trace)
            return exact, trace

    # ── ChromaDB L2 semantic cache + RAG ─────────────────────────────────────
    cached_answer, rag_context = rag.retrieve(question)
    doc_chunks = query_documents(question, tenant_id=tenant.tenant_id)
    if doc_chunks:
        rag_context += "\n\n" + "\n\n".join(
            f"[Doc: {c['source']}]\n{c['chunk']}" for c in doc_chunks
        )

    if cached_answer:
        trace.cache_hit        = True
        trace.avoided_cost_usd = ledger.rolling_avg_cost(tenant.tenant_id)
        ledger.record(trace)
        record_trace(trace)
        return cached_answer, trace

    # ── Plan ─────────────────────────────────────────────────────────────────
    plan = await planner.create_plan(
        client, question,
        rag_context=rag_context,
        conversation_history=history,
        trace=trace,
    )
    trace.agents_invoked = plan["agents"]
    tasks_map = plan.get("tasks", {})

    # ── Parallel agents (semantic + benchmark) ────────────────────────────────
    sub_traces: dict[str, QueryTrace] = {}
    coros: dict[str, asyncio.coroutines] = {}

    for name in ("semantic", "benchmark"):
        if name in plan["agents"]:
            sub_traces[name] = QueryTrace(question=tasks_map.get(name, question))
            agent_fn = semantic_agent if name == "semantic" else benchmark_agent
            coros[name] = agent_fn.run(
                client, orchestrator, tasks_map.get(name, question), sub_traces[name]
            )

    parallel_results: dict[str, str] = {}
    if coros:
        gathered = await asyncio.gather(*coros.values())
        for name, result in zip(coros.keys(), gathered):
            parallel_results[name] = result
            trace.merge_agent_trace(name, sub_traces[name])

    # ── Insight agent (sequential — uses parallel results as context) ─────────
    if "insight" in plan["agents"]:
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

    # ── Synthesis ─────────────────────────────────────────────────────────────
    agent_summaries = "\n\n".join(
        f"## {n.title()} Agent\n{r}" for n, r in parallel_results.items()
    )
    synthesis = (
        f"User question: {question}\n\nAgent findings:\n{agent_summaries}\n\n"
        "Synthesize into a clear, concise answer with key numbers. Use markdown."
    )

    response = await client.messages.create(
        model=MODEL, max_tokens=2048,
        system=_SYNTHESIS_SYSTEM,
        messages=[{"role": "user", "content": synthesis}],
    )
    trace.record_usage(response)
    answer = response.content[0].text

    if check_pii(answer).allowed:
        rag.store_qa(question, answer, trace.agents_invoked)
        if redis_mem:
            await redis_mem.set_exact(tenant.tenant_id, question, answer, trace.agents_invoked)
    if redis_mem:
        await redis_mem.append_history(tenant.tenant_id, question, answer)
    history.append({"question": question, "answer": answer})
    rag.save_history(history)
    ledger.record(trace)
    record_trace(trace)
    update_cache_size(trace.tenant_id, rag.stats()["qa_entries"])

    log.info(
        "query.complete",
        latency_s=round(trace.latency, 2),
        cost_usd=round(trace.cost, 5),
        cache_hit=trace.cache_hit,
        agents=trace.agents_invoked,
    )
    return answer, trace


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
            system=_SYNTHESIS_SYSTEM,
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

@app.delete("/cache")
async def clear_cache_entry(question: str, ctx: TenantContext = Depends(require_auth)):
    _get_rag(ctx.tenant_id).flag_bad(question)
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

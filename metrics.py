"""
Prometheus metrics exporter for the MCP Data Agents system.

All metrics use the 'mcp_agents_' prefix.

Exposed at GET /metrics — Prometheus scrapes this every 15 s.
Grafana connects to Prometheus as its data source.

Suggested alert rules:
  - mcp_agents_planner_fallbacks_total rate > 3/min  → planner degraded
  - mcp_agents_bad_feedback_total rate > 20%          → cache rollback check
  - mcp_agents_query_latency_seconds p95 > 15         → latency SLO breach
  - mcp_agents_cost_usd_total rate (per hour)         → budget threshold alert
"""
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from fastapi.responses import Response

# ── Counters ──────────────────────────────────────────────────────────────────

QUERIES_TOTAL = Counter(
    "mcp_agents_queries_total",
    "Total queries processed",
    ["tenant_id", "cache_hit", "plan_confidence"],
)

COST_USD_TOTAL = Counter(
    "mcp_agents_cost_usd_total",
    "Cumulative LLM spend in USD",
    ["tenant_id", "team_id"],
)

AVOIDED_COST_TOTAL = Counter(
    "mcp_agents_avoided_cost_usd_total",
    "Cumulative spend avoided by semantic cache in USD",
    ["tenant_id"],
)

AGENT_COST_TOTAL = Counter(
    "mcp_agents_agent_cost_usd_total",
    "LLM spend per agent in USD",
    ["agent_name", "tenant_id"],
)

BAD_FEEDBACK_TOTAL = Counter(
    "mcp_agents_bad_feedback_total",
    "Answers rated bad by users",
    ["tenant_id"],
)

PLANNER_FALLBACKS_TOTAL = Counter(
    "mcp_agents_planner_fallbacks_total",
    "Planner fell back to all-agents routing",
    ["tenant_id"],
)

TOOL_CALLS_TOTAL = Counter(
    "mcp_agents_tool_calls_total",
    "MCP tool invocations",
    ["tool_name", "tenant_id"],
)

SECURITY_BLOCKS_TOTAL = Counter(
    "mcp_agents_security_blocks_total",
    "Queries rejected by the security layer",
    ["reason"],
)

# ── Cache Observability counters ──────────────────────────────────────────────

CACHE_HITS_TOTAL = Counter(
    "mcp_agents_cache_hits_total",
    "Cache hits by layer and query type",
    ["layer", "query_type", "tenant_id"],
)

CACHE_MISSES_TOTAL = Counter(
    "mcp_agents_cache_misses_total",
    "Cache misses requiring full pipeline execution",
    ["query_type", "tenant_id"],
)

PARTIAL_REUSE_TOTAL = Counter(
    "mcp_agents_partial_cache_reuse_total",
    "Queries where at least one agent result was reused from agent cache",
    ["tenant_id"],
)

CACHE_INVALIDATIONS_TOTAL = Counter(
    "mcp_agents_cache_invalidations_total",
    "Cache entries invalidated by feedback or manual action",
    ["reason", "tenant_id"],
)

CONTEXT_SOURCES_TOTAL = Counter(
    "mcp_agents_context_sources_total",
    "Context sources used per assembled context package",
    ["source", "tenant_id"],
)

# ── Histograms ────────────────────────────────────────────────────────────────

QUERY_LATENCY = Histogram(
    "mcp_agents_query_latency_seconds",
    "End-to-end query latency",
    ["tenant_id"],
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 15.0, 30.0],
)

# ── Gauges ────────────────────────────────────────────────────────────────────

CACHE_SIZE = Gauge(
    "mcp_agents_cache_size",
    "Number of Q&A entries in the semantic cache",
    ["tenant_id"],
)

MCP_SERVERS_UP = Gauge(
    "mcp_agents_mcp_servers_up",
    "Number of MCP servers currently connected",
)


# ── Public API ────────────────────────────────────────────────────────────────

def record_trace(trace) -> None:
    """Push all metrics from a completed QueryTrace into Prometheus."""
    tid  = trace.tenant_id or "default"
    team = trace.team_id   or "unattributed"

    QUERIES_TOTAL.labels(
        tenant_id=tid,
        cache_hit=str(trace.cache_hit).lower(),
        plan_confidence=trace.plan_confidence,
    ).inc()

    COST_USD_TOTAL.labels(tenant_id=tid, team_id=team).inc(trace.cost)
    AVOIDED_COST_TOTAL.labels(tenant_id=tid).inc(trace.avoided_cost_usd)
    QUERY_LATENCY.labels(tenant_id=tid).observe(trace.latency)

    for agent_name, agent_cost in trace.agent_costs.items():
        AGENT_COST_TOTAL.labels(agent_name=agent_name, tenant_id=tid).inc(agent_cost.cost)

    for tool_name in trace.tool_calls:
        TOOL_CALLS_TOTAL.labels(tool_name=tool_name, tenant_id=tid).inc()

    if trace.feedback == "bad":
        BAD_FEEDBACK_TOTAL.labels(tenant_id=tid).inc()

    if trace.plan_confidence == "fallback":
        PLANNER_FALLBACKS_TOTAL.labels(tenant_id=tid).inc()


def record_security_block(reason: str) -> None:
    """Increment security block counter — call from the API layer on rejected queries."""
    SECURITY_BLOCKS_TOTAL.labels(reason=reason).inc()


def update_cache_size(tenant_id: str, size: int) -> None:
    """Refresh the cache size gauge — call after store_qa or flag_bad."""
    CACHE_SIZE.labels(tenant_id=tenant_id).set(size)


def update_mcp_servers(count: int) -> None:
    """Set the number of live MCP server connections."""
    MCP_SERVERS_UP.set(count)


def record_cache_hit(layer: str, query_type: str, tenant_id: str) -> None:
    """Increment cache hit counter for a specific layer (redis_l1 / chroma_l2)."""
    CACHE_HITS_TOTAL.labels(
        layer=layer, query_type=query_type, tenant_id=tenant_id
    ).inc()


def record_cache_miss(query_type: str, tenant_id: str) -> None:
    """Increment cache miss counter — full pipeline will run."""
    CACHE_MISSES_TOTAL.labels(query_type=query_type, tenant_id=tenant_id).inc()


def record_partial_reuse(tenant_id: str) -> None:
    """Increment partial agent cache reuse counter."""
    PARTIAL_REUSE_TOTAL.labels(tenant_id=tenant_id).inc()


def record_cache_invalidation(reason: str, tenant_id: str) -> None:
    """Increment invalidation counter (reason: bad_feedback / manual / bulk_rollback)."""
    CACHE_INVALIDATIONS_TOTAL.labels(reason=reason, tenant_id=tenant_id).inc()


def record_context_sources(sources: list[str], tenant_id: str) -> None:
    """Increment context source counters for each source used in assembly."""
    for source in sources:
        CONTEXT_SOURCES_TOTAL.labels(source=source, tenant_id=tenant_id).inc()


def metrics_response() -> Response:
    """Return Prometheus text-format scrape response for GET /metrics."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

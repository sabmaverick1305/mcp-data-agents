"""
Per-query observability — latency, token usage, per-agent cost breakdown, and pipeline tracing.

Two dataclasses implement the observability contract:

AgentCost
  Tracks input/output token counts for a single agent invocation and computes the
  estimated USD cost from the current claude-sonnet-4-6 pricing (3.00 / 15.00 per
  million tokens for input / output). Used by QueryTrace.agent_costs dict.

QueryTrace
  The primary observability object. Created at the start of each query and accumulated
  as the pipeline runs. Covers:
    - Cache hit status and avoided cost estimation
    - Agents invoked + tool calls made
    - Total input/output tokens (accumulated across all Claude API calls)
    - Per-agent token + cost breakdown (via merge_agent_trace)
    - Tenant / user / team identity for cost attribution
    - Plan confidence ("high" / "degraded" / "fallback")
    - Latency (computed lazily as time.time() - started_at)

  QueryTrace.to_dict() is serialised into the /query response JSON (the "trace" field),
  so API consumers can see exactly what happened on every request.

  QueryTrace.print_summary() renders a Rich table in the CLI for developer visibility.

Usage:
  trace = QueryTrace(question=q)
  # ... pipeline runs ...
  trace.record_usage(claude_response)          # accumulate tokens from any API response
  trace.merge_agent_trace("semantic", sub)     # absorb a sub-agent's trace
  trace.record_tool("snowflake__run_sql_query") # log tool call

  # After pipeline:
  ledger.record(trace)     # persist to SQLite
  record_trace(trace)      # push to Prometheus
  trace.print_summary()    # Rich table to stdout (CLI only)
"""
import time
from dataclasses import dataclass, field

from rich.console import Console
from rich.table import Table

# claude-sonnet-4-6 pricing (USD per million tokens, as of 2026)
_INPUT_CPM  = 3.00
_OUTPUT_CPM = 15.00

# Estimated full-pipeline cost used to compute avoided_cost_usd on cache hits.
# Replace with CostLedger.rolling_avg() once the ledger has enough history.
ESTIMATED_PIPELINE_COST_USD = 0.02

console = Console()


@dataclass
class AgentCost:
    """Token usage and cost for one agent invocation."""
    input_tokens:  int = 0
    output_tokens: int = 0

    @property
    def cost(self) -> float:
        return round(
            self.input_tokens  / 1_000_000 * _INPUT_CPM +
            self.output_tokens / 1_000_000 * _OUTPUT_CPM,
            6,
        )

    def to_dict(self) -> dict:
        return {
            "input_tokens":  self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd":      self.cost,
        }


@dataclass
class QueryTrace:
    question:        str
    started_at:      float       = field(default_factory=time.time)
    cache_hit:       bool        = False
    agents_invoked:  list[str]   = field(default_factory=list)
    tool_calls:      list[str]   = field(default_factory=list)
    input_tokens:    int         = 0
    output_tokens:   int         = 0
    feedback:        str | None  = None   # "good" | "bad" | None
    # ── Enterprise fields ─────────────────────────────────────────────────────
    agent_costs:       dict[str, AgentCost] = field(default_factory=dict)
    plan_confidence:   str  = "high"    # "high" | "degraded" | "fallback"
    avoided_cost_usd:  float = 0.0      # savings when cache hit
    tenant_id:         str        = "default"
    user_id:           str | None = None
    team_id:           str | None = None

    # ── Token accumulation ────────────────────────────────────────────────────

    def record_usage(self, response) -> None:
        """Accumulate token counts from any Anthropic API response object."""
        if hasattr(response, "usage") and response.usage:
            self.input_tokens  += getattr(response.usage, "input_tokens",  0)
            self.output_tokens += getattr(response.usage, "output_tokens", 0)

    def record_tool(self, prefixed_name: str) -> None:
        self.tool_calls.append(prefixed_name)

    # ── Per-agent cost tracking ───────────────────────────────────────────────

    def merge_agent_trace(self, agent_name: str, sub: "QueryTrace") -> None:
        """
        Absorb a per-agent sub-trace into this main trace.
        Accumulates total tokens and stores the per-agent breakdown.
        """
        self.input_tokens  += sub.input_tokens
        self.output_tokens += sub.output_tokens
        self.tool_calls    += sub.tool_calls
        self.agent_costs[agent_name] = AgentCost(
            input_tokens=sub.input_tokens,
            output_tokens=sub.output_tokens,
        )

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def latency(self) -> float:
        return round(time.time() - self.started_at, 2)

    @property
    def cost(self) -> float:
        return round(
            self.input_tokens  / 1_000_000 * _INPUT_CPM +
            self.output_tokens / 1_000_000 * _OUTPUT_CPM,
            5,
        )

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "question":        self.question,
            "latency_s":       self.latency,
            "cache_hit":       self.cache_hit,
            "agents":          self.agents_invoked,
            "tool_calls":      self.tool_calls,
            "input_tokens":    self.input_tokens,
            "output_tokens":   self.output_tokens,
            "cost_usd":        self.cost,
            "avoided_cost_usd": self.avoided_cost_usd,
            "plan_confidence": self.plan_confidence,
            "agent_costs":     {k: v.to_dict() for k, v in self.agent_costs.items()},
            "feedback":        self.feedback,
            "tenant_id":       self.tenant_id,
            "user_id":         self.user_id,
            "team_id":         self.team_id,
        }

    def print_summary(self) -> None:
        t = Table(show_header=False, box=None, padding=(0, 2))
        t.add_column("k", style="dim")
        t.add_column("v")
        t.add_row("Latency",          f"{self.latency}s")
        t.add_row("Cache hit",        "yes" if self.cache_hit else "no")
        t.add_row("Plan confidence",  self.plan_confidence)
        t.add_row("Agents",           ", ".join(self.agents_invoked) or "—")
        t.add_row("Tool calls",       str(len(self.tool_calls)))
        t.add_row("Tokens in/out",    f"{self.input_tokens:,} / {self.output_tokens:,}")
        t.add_row("Est. cost",        f"${self.cost:.5f}")
        if self.cache_hit:
            t.add_row("Avoided cost", f"${self.avoided_cost_usd:.5f}")
        if self.agent_costs:
            for name, ac in self.agent_costs.items():
                t.add_row(f"  └ {name}", f"${ac.cost:.5f}")
        t.add_row("Feedback",         self.feedback or "—")
        console.print(t)

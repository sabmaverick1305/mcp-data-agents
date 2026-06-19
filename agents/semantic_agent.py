"""
Semantic Agent — pre-defined KPI and metric retrieval via the Power BI MCP server.

Role in the pipeline:
  Handles questions about well-defined business metrics that are modelled in the
  Power BI semantic layer. These include aggregated KPIs that can be answered without
  ad-hoc SQL — total revenue, gross margin %, average order value, MoM/YoY growth,
  and customer lifetime value.

When the Planner routes here:
  - "What is our gross margin this quarter?" → semantic only
  - "Why did revenue drop in Q1?" → semantic + insight (KPI context + SQL drill-down)
  - "Executive summary of 2024" → semantic + benchmark + insight (all agents)

MCP server used: powerbi_server.py (FastMCP "powerbi-semantic")
MCP tools available:
  list_semantic_models()               — discover available models and measures
  get_semantic_model(model_id)         — fetch full model schema
  get_metric(metric_name, time_period, dimension) — compute a named KPI

This module is intentionally thin — all retry, tool dispatch, and tracing logic lives
in agents/base_agent.run_agent_loop(). Adding new KPI tools only requires updating
powerbi_server.py; this agent file does not change.
"""
import anthropic
from observability import QueryTrace
from orchestrator import MCPOrchestrator
from agents.base_agent import run_agent_loop

SYSTEM = """You are a Semantic Agent with access to Power BI semantic models.
Retrieve and interpret pre-defined business metrics (revenue, margins, growth rates, LTV).
Always state the time period and any dimension used. Be concise and precise with numbers."""


async def run(
    client: anthropic.AsyncAnthropic,
    orchestrator: MCPOrchestrator,
    question: str,
    trace: QueryTrace | None = None,
) -> str:
    return await run_agent_loop(client, orchestrator, ["powerbi"], SYSTEM, question, trace)

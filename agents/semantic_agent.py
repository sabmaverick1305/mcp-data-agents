"""Semantic Agent — KPI and metric questions via Power BI MCP."""
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

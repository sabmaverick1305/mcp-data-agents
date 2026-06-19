"""Benchmark Agent — comparisons and rankings via Tableau MCP."""
import anthropic
from observability import QueryTrace
from orchestrator import MCPOrchestrator
from agents.base_agent import run_agent_loop

SYSTEM = """You are a Benchmark Agent with access to Tableau dashboards.
Provide comparative analysis — regional vs targets, category rankings, top performers, quarterly trends.
Always highlight which entities are above or below benchmark. Be specific about percentages and rankings."""


async def run(
    client: anthropic.AsyncAnthropic,
    orchestrator: MCPOrchestrator,
    question: str,
    trace: QueryTrace | None = None,
) -> str:
    return await run_agent_loop(client, orchestrator, ["tableau"], SYSTEM, question, trace)

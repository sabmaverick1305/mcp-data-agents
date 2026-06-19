"""
Benchmark Agent — comparative analysis and rankings via the Tableau MCP server.

Role in the pipeline:
  Handles questions that compare performance across dimensions or against targets.
  Unlike the Semantic Agent (point-in-time KPIs), the Benchmark Agent focuses on
  relative performance: which regions beat their targets, which categories are lagging,
  who the top performers are, and how metrics trend quarter over quarter.

When the Planner routes here:
  - "Which regions are below target?" → benchmark only
  - "Compare enterprise vs SMB revenue" → semantic + benchmark
  - "Executive summary of 2024" → semantic + benchmark + insight (all agents)

MCP server used: tableau_server.py (FastMCP "tableau-dashboards")
MCP tools available:
  list_dashboards()                           — list available dashboard views
  get_dashboard_summary(dashboard_id)         — metadata and view list
  get_benchmark_data(benchmark_type, period)  — regional_vs_target, category_performance,
                                                segment_comparison, quarterly_trend
  get_top_performers(entity_type, metric, limit, period) — products / customers / regions

Runs in parallel with the Semantic Agent (both are dispatched concurrently by
asyncio.gather in api.py and main.py). Results are merged by the Insight Agent
if it is also invoked.
"""
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

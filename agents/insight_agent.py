"""Insight Agent — ad-hoc SQL analysis on the Snowflake warehouse."""
import anthropic
from observability import QueryTrace
from orchestrator import MCPOrchestrator
from agents.base_agent import run_agent_loop

SYSTEM = """You are an Insight Agent with direct access to the Snowflake data warehouse via SQL.
Tables: sales_fact, product_dim, customer_dim, region_dim, date_dim.
Write efficient SQL queries to answer specific data questions. Interpret results clearly.
When given context from other agents, use it to frame your findings and identify root causes."""


async def run(
    client: anthropic.AsyncAnthropic,
    orchestrator: MCPOrchestrator,
    question: str,
    context: str = "",
    trace: QueryTrace | None = None,
) -> str:
    full_q = f"Context from other agents:\n{context}\n\nYour task: {question}" if context else question
    return await run_agent_loop(client, orchestrator, ["snowflake"], SYSTEM, full_q, trace)

"""
Insight Agent — ad-hoc SQL analysis and root-cause investigation via the Snowflake MCP server.

Role in the pipeline:
  The deepest layer of analysis. While Semantic and Benchmark agents read from curated
  views and pre-defined metrics, the Insight Agent writes bespoke SQL against the raw
  star-schema tables. It runs *after* the parallel pair and receives their output as
  additional context, enabling cross-source correlation.

When the Planner routes here:
  - "List top 5 customers by revenue in 2024" → insight only (raw ranking SQL)
  - "Why did Q1 2024 revenue drop?" → semantic + insight (KPI context + drill-down SQL)
  - "Executive summary of 2024" → semantic + benchmark + insight (all agents)

Context integration:
  When both semantic and benchmark results are available, they are prepended to the
  Insight Agent's question in the format:
    "Context from other agents:\n[SEMANTIC]\n...\n[BENCHMARK]\n...\nYour task: {question}"
  This lets the agent correlate KPIs with transaction-level SQL findings.

MCP server used: snowflake_server.py (FastMCP "snowflake-warehouse")
MCP tools available:
  list_tables()                — discover warehouse tables
  describe_table(table_name)   — column schema for a table
  run_sql_query(query)         — execute SELECT-only SQL (max 500 rows, DDL blocked)

Thin-result guard (api.py / main.py):
  If the returned text is < 120 chars or starts with "[Agent error", the caller
  retries with a broader prompt instructing the agent to use wider date ranges or
  fewer filters. This handles overly-specific SQL that returns no rows.
"""
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

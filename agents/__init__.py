"""
Agent package — the four specialised AI agents that form the analytics pipeline.

Agent roster
------------
planner          Classifies user intent, emits a structured JSON task list, and
                 decides which downstream agents to invoke. Runs first, always.

semantic_agent   Queries pre-defined Power BI KPIs and metrics (total_revenue,
                 gross_margin_pct, avg_order_value, revenue_growth_*, customer_ltv)
                 via the Power BI MCP server. Fast; no raw SQL.

benchmark_agent  Runs comparative / ranking queries via the Tableau MCP server
                 (regional_vs_target, category_performance, top performers).
                 Semantic and Benchmark run in parallel after the Planner.

insight_agent    Issues ad-hoc SQL against the Snowflake MCP server for root-cause
                 analysis. Runs after the parallel pair, receives their output as
                 additional context so it can correlate KPIs with raw data.

All agents share a single async execution loop defined in base_agent.run_agent_loop(),
which handles retry / back-off, tool-call security validation, and token tracing.

Typical call sequence (orchestrated by api.py / main.py):
  plan = await planner.create_plan(client, question, rag_context, history, trace)
  # plan["agents"] → e.g. ["semantic", "benchmark"]
  results = await asyncio.gather(
      semantic_agent.run(client, orchestrator, task_semantic, trace_s),
      benchmark_agent.run(client, orchestrator, task_bench,   trace_b),
  )
  answer = await insight_agent.run(..., context=combined_results, ...)
"""

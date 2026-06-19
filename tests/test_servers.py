"""
Integration tests for the three MCP servers — SQL safety, schema inspection, and data queries.

These are true integration tests: each test spawns the actual server script as a child
process via stdio_client and communicates through a real MCP ClientSession. The
data/warehouse.db must exist (seeded via seed_database) for query tests to return data.

Snowflake server (snowflake_server.py):
  test_snowflake_list_tables        list_tables() returns sales_fact and product_dim
  test_snowflake_select_query       COUNT(*) on sales_fact returns n > 0
  test_snowflake_blocks_drop        DROP TABLE is rejected as a blocked keyword
  test_snowflake_blocks_non_select  DELETE query rejected (must start with SELECT)
  test_snowflake_describe_table     describe_table(sales_fact) includes revenue, gross_profit

Power BI server (powerbi_server.py):
  test_powerbi_list_models          list_semantic_models() includes sales_performance
  test_powerbi_total_revenue_metric get_metric(total_revenue, 2024-Q1) returns a positive value

Tableau server (tableau_server.py):
  test_tableau_list_dashboards      list_dashboards() includes regional_performance
  test_tableau_quarterly_trend      get_benchmark_data(quarterly_trend) returns ≥ 4 quarters

All tests are async (pytest-asyncio) because the MCP SDK uses asyncio throughout.
"""
import asyncio
import json
import sys
import pytest

from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters


def _params(script: str) -> StdioServerParameters:
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "servers", script)
    return StdioServerParameters(command=sys.executable, args=[path])


# ── Snowflake server ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_snowflake_list_tables():
    async with stdio_client(_params("snowflake_server.py")) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("list_tables", {})
            tables = json.loads(res.content[0].text)
            assert "sales_fact" in tables
            assert "product_dim" in tables


@pytest.mark.asyncio
async def test_snowflake_select_query():
    async with stdio_client(_params("snowflake_server.py")) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(
                "run_sql_query",
                {"query": "SELECT COUNT(*) as n FROM sales_fact"},
            )
            rows = json.loads(res.content[0].text)
            assert rows[0]["n"] > 0


@pytest.mark.asyncio
async def test_snowflake_blocks_drop():
    async with stdio_client(_params("snowflake_server.py")) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(
                "run_sql_query",
                {"query": "DROP TABLE sales_fact"},
            )
            result = json.loads(res.content[0].text)
            assert "error" in result


@pytest.mark.asyncio
async def test_snowflake_blocks_non_select():
    async with stdio_client(_params("snowflake_server.py")) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(
                "run_sql_query",
                {"query": "DELETE FROM sales_fact WHERE 1=1"},
            )
            result = json.loads(res.content[0].text)
            assert "error" in result


@pytest.mark.asyncio
async def test_snowflake_describe_table():
    async with stdio_client(_params("snowflake_server.py")) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("describe_table", {"table_name": "sales_fact"})
            schema = json.loads(res.content[0].text)
            col_names = [c["name"] for c in schema["columns"]]
            assert "revenue" in col_names
            assert "gross_profit" in col_names


# ── Power BI server ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_powerbi_list_models():
    async with stdio_client(_params("powerbi_server.py")) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("list_semantic_models", {})
            models = json.loads(res.content[0].text)
            ids = [m["id"] for m in models]
            assert "sales_performance" in ids


@pytest.mark.asyncio
async def test_powerbi_total_revenue_metric():
    async with stdio_client(_params("powerbi_server.py")) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(
                "get_metric",
                {"metric_name": "total_revenue", "time_period": "2024-Q1"},
            )
            result = json.loads(res.content[0].text)
            assert result["data"]["total_revenue"] > 0


# ── Tableau server ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tableau_list_dashboards():
    async with stdio_client(_params("tableau_server.py")) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("list_dashboards", {})
            dashboards = json.loads(res.content[0].text)
            ids = [d["id"] for d in dashboards]
            assert "regional_performance" in ids


@pytest.mark.asyncio
async def test_tableau_quarterly_trend():
    async with stdio_client(_params("tableau_server.py")) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(
                "get_benchmark_data",
                {"benchmark_type": "quarterly_trend"},
            )
            result = json.loads(res.content[0].text)
            assert len(result["data"]) >= 4

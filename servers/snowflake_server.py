"""
MCP server — Snowflake data warehouse simulation (FastMCP "snowflake-warehouse").

Exposes ad-hoc SQL execution against the SQLite warehouse (data/warehouse.db) through
the MCP tool protocol, mirroring the interface of a real Snowflake connection. The
Insight Agent uses this server for root-cause analysis and custom rankings that cannot
be expressed as pre-defined Power BI metrics.

Security:
  All SQL passes through _safe_query() before execution:
    - Must start with SELECT (no DML/DDL)
    - Blocked keyword list: DROP, DELETE, INSERT, UPDATE, CREATE, ALTER, TRUNCATE,
      EXEC, EXECUTE, PRAGMA, ATTACH, DETACH, VACUUM, REINDEX, REPLACE, MERGE,
      CALL, GRANT, REVOKE
    - Maximum query length: 4000 characters
    - Results capped at 500 rows
  This server-side guard is in addition to the application-layer check_tool_call()
  allowlist in security.py.

Available tables (read-only):
  sales_fact       — one row per transaction (revenue, cost, gross_profit)
  product_dim      — product catalogue (category, unit_price, unit_cost)
  customer_dim     — account list (segment, country)
  region_dim       — 4 regions with annual target_revenue quotas
  date_dim         — calendar dates with year / quarter / month / week

MCP tools exposed:
  list_tables()                  → table names in the warehouse
  describe_table(table_name)     → column schema {name, type} for one table
  run_sql_query(query)           → JSON array of rows (max 500)

Production replacement:
  Replace sqlite3.connect(DB_PATH) with a Snowflake connector
  (snowflake-connector-python) or SQLAlchemy engine. The _safe_query() guard
  and tool signatures remain unchanged.
"""
import json
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.fastmcp import FastMCP

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "warehouse.db")

mcp = FastMCP("snowflake-warehouse")

# ── SQL injection protection ───────────────────────────────────────────────────
_BLOCKED = re.compile(
    r"\b(drop|delete|insert|update|create|alter|truncate|exec|execute|"
    r"pragma|attach|detach|vacuum|reindex|replace|merge|call|grant|revoke)\b",
    re.IGNORECASE,
)


def _safe_query(query: str) -> str | None:
    """Return an error string if query is unsafe, else None."""
    stripped = query.strip()
    if not stripped.upper().startswith("SELECT"):
        return "Only SELECT queries are allowed."
    if _BLOCKED.search(stripped):
        return "Query contains a blocked keyword."
    if len(stripped) > 4000:
        return "Query exceeds maximum length (4000 chars)."
    return None


# ── MCP tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def list_tables() -> str:
    """List all tables in the data warehouse."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cur.fetchall()]
    conn.close()
    return json.dumps(tables)


@mcp.tool()
def describe_table(table_name: str) -> str:
    """Get the schema of a specific warehouse table."""
    allowed = {"sales_fact", "product_dim", "customer_dim", "region_dim", "date_dim"}
    if table_name not in allowed:
        return json.dumps({"error": f"Unknown table '{table_name}'"})
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table_name})")
    columns = [{"name": row[1], "type": row[2]} for row in cur.fetchall()]
    conn.close()
    return json.dumps({"table": table_name, "columns": columns})


@mcp.tool()
def run_sql_query(query: str) -> str:
    """
    Execute a SQL SELECT query against the data warehouse.

    Available tables: sales_fact, product_dim, customer_dim, region_dim, date_dim.
    Returns results as a JSON array of row objects (max 500 rows).
    """
    err = _safe_query(query)
    if err:
        return json.dumps({"error": err})
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query)
        rows = [dict(row) for row in cur.fetchmany(500)]
        conn.close()
        return json.dumps(rows, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


if __name__ == "__main__":
    mcp.run()

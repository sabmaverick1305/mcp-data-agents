"""MCP server simulating a Snowflake data warehouse (backed by SQLite)."""
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

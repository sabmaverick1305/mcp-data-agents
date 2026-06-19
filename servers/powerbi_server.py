"""MCP server simulating a Power BI semantic layer with pre-defined business metrics."""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.fastmcp import FastMCP

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "warehouse.db")

mcp = FastMCP("powerbi-semantic")

SEMANTIC_MODELS = {
    "sales_performance": {
        "name": "Sales Performance Model",
        "description": "Core sales KPIs and growth metrics",
        "measures": {
            "total_revenue": "SUM of revenue across all transactions",
            "gross_margin_pct": "Gross Profit / Revenue × 100",
            "avg_order_value": "Total Revenue / Transaction Count",
            "revenue_growth_mom": "Month-over-month revenue growth %",
            "revenue_growth_yoy": "Year-over-year quarterly revenue growth %",
        },
        "dimensions": ["region", "product_category", "customer_segment", "year", "quarter", "month"],
    },
    "customer_analytics": {
        "name": "Customer Analytics Model",
        "description": "Customer lifetime value and segmentation metrics",
        "measures": {
            "customer_ltv": "Average cumulative revenue per customer",
            "revenue_per_customer": "Total revenue / unique customer count",
            "unique_customers": "Count of distinct customers",
        },
        "dimensions": ["segment", "country"],
    },
}


@mcp.tool()
def list_semantic_models() -> str:
    """List all available Power BI semantic models."""
    return json.dumps([
        {"id": k, "name": v["name"], "description": v["description"]}
        for k, v in SEMANTIC_MODELS.items()
    ])


@mcp.tool()
def get_semantic_model(model_id: str) -> str:
    """Get full details of a semantic model including measures and dimensions."""
    if model_id not in SEMANTIC_MODELS:
        return json.dumps({"error": f"Model '{model_id}' not found. Available: {list(SEMANTIC_MODELS)}"})
    return json.dumps(SEMANTIC_MODELS[model_id])


@mcp.tool()
def get_metric(
    metric_name: str,
    time_period: str = "all",
    dimension: str = None,
) -> str:
    """
    Calculate a pre-defined semantic metric.

    Args:
        metric_name: One of: total_revenue, gross_margin_pct, avg_order_value,
                     revenue_growth_mom, revenue_growth_yoy, customer_ltv,
                     revenue_per_customer, unique_customers
        time_period: 'all', '2023', '2024', '2024-Q1', '2023-Q4', '2024-03'
        dimension:   Optional breakdown — 'region', 'category', 'segment'
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Build WHERE clause from time_period
    date_filter = _build_date_filter(time_period)

    try:
        result = _compute_metric(cur, metric_name, date_filter, dimension)
        conn.close()
        return json.dumps({"metric": metric_name, "time_period": time_period,
                           "dimension": dimension, "data": result})
    except Exception as e:
        conn.close()
        return json.dumps({"error": str(e)})


def _build_date_filter(time_period: str) -> str:
    if not time_period or time_period == "all":
        return ""
    if "-Q" in time_period:
        year, q = time_period.split("-Q")
        return f"AND d.year = {year} AND d.quarter = {q}"
    if len(time_period) == 4 and time_period.isdigit():
        return f"AND d.year = {time_period}"
    if len(time_period) == 7:
        year, month = time_period.split("-")
        return f"AND d.year = {year} AND d.month = {month}"
    return ""


def _compute_metric(cur, metric_name: str, date_filter: str, dimension: str):
    if metric_name == "total_revenue":
        return _with_optional_dimension(cur, "ROUND(SUM(s.revenue), 2) as total_revenue",
                                        date_filter, dimension, "total_revenue")

    if metric_name == "gross_margin_pct":
        return _with_optional_dimension(
            cur,
            "ROUND(SUM(s.gross_profit) / NULLIF(SUM(s.revenue), 0) * 100, 2) as gross_margin_pct",
            date_filter, dimension, "gross_margin_pct")

    if metric_name == "avg_order_value":
        cur.execute(f"""
            SELECT ROUND(SUM(s.revenue) / COUNT(*), 2) as avg_order_value
            FROM sales_fact s JOIN date_dim d ON s.date_id = d.date_id
            WHERE 1=1 {date_filter}
        """)
        return dict(cur.fetchone())

    if metric_name == "revenue_growth_mom":
        cur.execute("""
            SELECT d.year, d.month, d.month_name,
                   ROUND(SUM(s.revenue), 2) as monthly_revenue
            FROM sales_fact s JOIN date_dim d ON s.date_id = d.date_id
            GROUP BY d.year, d.month, d.month_name
            ORDER BY d.year, d.month
        """)
        rows = [dict(r) for r in cur.fetchall()]
        for i in range(1, len(rows)):
            prev = rows[i - 1]["monthly_revenue"]
            rows[i]["mom_growth_pct"] = round((rows[i]["monthly_revenue"] - prev) / prev * 100, 2) if prev else 0
        rows[0]["mom_growth_pct"] = None
        return rows

    if metric_name == "revenue_growth_yoy":
        cur.execute("""
            SELECT d.year, d.quarter, ROUND(SUM(s.revenue), 2) as quarterly_revenue
            FROM sales_fact s JOIN date_dim d ON s.date_id = d.date_id
            GROUP BY d.year, d.quarter ORDER BY d.year, d.quarter
        """)
        rows = [dict(r) for r in cur.fetchall()]
        lookup = {(r["year"], r["quarter"]): r["quarterly_revenue"] for r in rows}
        for r in rows:
            prev = lookup.get((r["year"] - 1, r["quarter"]))
            r["yoy_growth_pct"] = round((r["quarterly_revenue"] - prev) / prev * 100, 2) if prev else None
        return rows

    if metric_name == "customer_ltv":
        cur.execute("""
            SELECT ROUND(AVG(customer_revenue), 2) as avg_customer_ltv
            FROM (SELECT customer_id, SUM(revenue) as customer_revenue
                  FROM sales_fact GROUP BY customer_id)
        """)
        return dict(cur.fetchone())

    if metric_name == "revenue_per_customer":
        cur.execute(f"""
            SELECT ROUND(SUM(s.revenue) / COUNT(DISTINCT s.customer_id), 2) as revenue_per_customer
            FROM sales_fact s JOIN date_dim d ON s.date_id = d.date_id
            WHERE 1=1 {date_filter}
        """)
        return dict(cur.fetchone())

    if metric_name == "unique_customers":
        cur.execute(f"""
            SELECT COUNT(DISTINCT s.customer_id) as unique_customers
            FROM sales_fact s JOIN date_dim d ON s.date_id = d.date_id
            WHERE 1=1 {date_filter}
        """)
        return dict(cur.fetchone())

    return {"error": f"Unknown metric: {metric_name}"}


def _with_optional_dimension(cur, select_expr: str, date_filter: str, dimension: str, order_col: str):
    dim_map = {
        "region": ("JOIN region_dim r ON s.region_id = r.region_id", "r.region_name"),
        "category": ("JOIN product_dim p ON s.product_id = p.product_id", "p.category"),
        "segment": ("JOIN customer_dim c ON s.customer_id = c.customer_id", "c.segment"),
    }
    if dimension and dimension in dim_map:
        join_clause, group_col = dim_map[dimension]
        cur.execute(f"""
            SELECT {group_col} as dimension, {select_expr}
            FROM sales_fact s
            JOIN date_dim d ON s.date_id = d.date_id
            {join_clause}
            WHERE 1=1 {date_filter}
            GROUP BY {group_col} ORDER BY {order_col} DESC
        """)
        return [dict(r) for r in cur.fetchall()]
    else:
        cur.execute(f"""
            SELECT {select_expr}
            FROM sales_fact s JOIN date_dim d ON s.date_id = d.date_id
            WHERE 1=1 {date_filter}
        """)
        return dict(cur.fetchone())


if __name__ == "__main__":
    mcp.run()

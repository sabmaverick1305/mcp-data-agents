"""MCP server simulating Tableau — exposes dashboard metadata and benchmark comparisons."""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.fastmcp import FastMCP

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "warehouse.db")

mcp = FastMCP("tableau-dashboards")

DASHBOARDS = {
    "regional_performance": {
        "name": "Regional Sales Performance",
        "description": "Revenue vs annual targets by region with attainment %",
        "views": ["revenue_by_region", "target_attainment", "regional_trends"],
    },
    "product_trends": {
        "name": "Product Category Trends",
        "description": "Category-level revenue, margin, and growth over time",
        "views": ["category_revenue", "category_margin", "top_products"],
    },
    "customer_segments": {
        "name": "Customer Segment Analysis",
        "description": "Revenue and transaction volume by customer segment",
        "views": ["segment_revenue", "revenue_per_customer", "top_customers"],
    },
    "executive_kpis": {
        "name": "Executive KPI Summary",
        "description": "High-level quarterly performance and trend overview",
        "views": ["quarterly_trend", "margin_trend", "growth_summary"],
    },
}


@mcp.tool()
def list_dashboards() -> str:
    """List all available Tableau dashboards."""
    return json.dumps([
        {"id": k, "name": v["name"], "description": v["description"]}
        for k, v in DASHBOARDS.items()
    ])


@mcp.tool()
def get_dashboard_summary(dashboard_id: str) -> str:
    """Get metadata and available views for a Tableau dashboard."""
    if dashboard_id not in DASHBOARDS:
        return json.dumps({"error": f"Dashboard '{dashboard_id}' not found. Available: {list(DASHBOARDS)}"})
    return json.dumps(DASHBOARDS[dashboard_id])


@mcp.tool()
def get_benchmark_data(benchmark_type: str, time_period: str = "all") -> str:
    """
    Fetch benchmark and comparison data from Tableau views.

    Args:
        benchmark_type: One of:
            regional_vs_target      — actual revenue vs annual targets per region
            category_performance    — revenue, margin, unique customers by product category
            segment_comparison      — revenue and per-customer metrics by customer segment
            quarterly_trend         — revenue, profit, and transaction count by quarter
        time_period: 'all', '2023', '2024', '2024-Q1', etc.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    date_filter = _build_date_filter(time_period)

    try:
        if benchmark_type == "regional_vs_target":
            cur.execute(f"""
                SELECT r.region_name,
                       ROUND(SUM(s.revenue), 2)               AS actual_revenue,
                       r.target_revenue,
                       ROUND(SUM(s.revenue) / r.target_revenue * 100, 1) AS attainment_pct
                FROM sales_fact s
                JOIN date_dim d   ON s.date_id   = d.date_id
                JOIN region_dim r ON s.region_id = r.region_id
                WHERE 1=1 {date_filter}
                GROUP BY r.region_name, r.target_revenue
                ORDER BY attainment_pct DESC
            """)
            result = [dict(r) for r in cur.fetchall()]

        elif benchmark_type == "category_performance":
            cur.execute(f"""
                SELECT p.category,
                       ROUND(SUM(s.revenue), 2)                                      AS revenue,
                       ROUND(SUM(s.gross_profit) / NULLIF(SUM(s.revenue), 0) * 100, 1) AS margin_pct,
                       COUNT(DISTINCT s.customer_id)                                 AS unique_customers,
                       COUNT(*)                                                       AS transactions
                FROM sales_fact s
                JOIN date_dim d    ON s.date_id    = d.date_id
                JOIN product_dim p ON s.product_id = p.product_id
                WHERE 1=1 {date_filter}
                GROUP BY p.category
                ORDER BY revenue DESC
            """)
            result = [dict(r) for r in cur.fetchall()]

        elif benchmark_type == "segment_comparison":
            cur.execute(f"""
                SELECT c.segment,
                       COUNT(DISTINCT c.customer_id)                          AS customer_count,
                       ROUND(SUM(s.revenue), 2)                               AS total_revenue,
                       ROUND(SUM(s.revenue) / COUNT(DISTINCT c.customer_id), 2) AS revenue_per_customer,
                       COUNT(*)                                                AS transactions
                FROM sales_fact s
                JOIN date_dim d     ON s.date_id     = d.date_id
                JOIN customer_dim c ON s.customer_id = c.customer_id
                WHERE 1=1 {date_filter}
                GROUP BY c.segment
                ORDER BY total_revenue DESC
            """)
            result = [dict(r) for r in cur.fetchall()]

        elif benchmark_type == "quarterly_trend":
            cur.execute("""
                SELECT d.year, d.quarter,
                       ROUND(SUM(s.revenue), 2)      AS revenue,
                       ROUND(SUM(s.gross_profit), 2) AS gross_profit,
                       COUNT(*)                       AS transactions
                FROM sales_fact s
                JOIN date_dim d ON s.date_id = d.date_id
                GROUP BY d.year, d.quarter
                ORDER BY d.year, d.quarter
            """)
            result = [dict(r) for r in cur.fetchall()]

        else:
            result = {"error": f"Unknown benchmark_type: '{benchmark_type}'. "
                               "Valid: regional_vs_target, category_performance, "
                               "segment_comparison, quarterly_trend"}

        conn.close()
        return json.dumps({"benchmark_type": benchmark_type, "time_period": time_period, "data": result})

    except Exception as e:
        conn.close()
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_top_performers(entity_type: str, metric: str = "revenue",
                       limit: int = 5, time_period: str = "all") -> str:
    """
    Retrieve top performing entities from Tableau views.

    Args:
        entity_type: 'products', 'customers', or 'regions'
        metric:      'revenue', 'transactions', or 'margin_pct' (products only)
        limit:       Number of results (default 5)
        time_period: 'all', '2023', '2024'
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    date_filter = _build_date_filter(time_period)

    try:
        if entity_type == "products":
            order_col = "margin_pct" if metric == "margin_pct" else metric
            cur.execute(f"""
                SELECT p.product_name, p.category,
                       ROUND(SUM(s.revenue), 2) AS revenue,
                       ROUND(SUM(s.gross_profit) / NULLIF(SUM(s.revenue), 0) * 100, 1) AS margin_pct,
                       COUNT(*) AS transactions
                FROM sales_fact s
                JOIN date_dim d    ON s.date_id    = d.date_id
                JOIN product_dim p ON s.product_id = p.product_id
                WHERE 1=1 {date_filter}
                GROUP BY p.product_name, p.category
                ORDER BY {order_col} DESC LIMIT {int(limit)}
            """)
        elif entity_type == "customers":
            cur.execute(f"""
                SELECT c.customer_name, c.segment,
                       ROUND(SUM(s.revenue), 2) AS revenue,
                       COUNT(*) AS transactions
                FROM sales_fact s
                JOIN date_dim d     ON s.date_id     = d.date_id
                JOIN customer_dim c ON s.customer_id = c.customer_id
                WHERE 1=1 {date_filter}
                GROUP BY c.customer_name, c.segment
                ORDER BY {metric} DESC LIMIT {int(limit)}
            """)
        elif entity_type == "regions":
            cur.execute(f"""
                SELECT r.region_name, r.manager,
                       ROUND(SUM(s.revenue), 2) AS revenue,
                       COUNT(*) AS transactions
                FROM sales_fact s
                JOIN date_dim d    ON s.date_id    = d.date_id
                JOIN region_dim r ON s.region_id  = r.region_id
                WHERE 1=1 {date_filter}
                GROUP BY r.region_name, r.manager
                ORDER BY {metric} DESC LIMIT {int(limit)}
            """)
        else:
            conn.close()
            return json.dumps({"error": f"Unknown entity_type: '{entity_type}'"})

        result = [dict(r) for r in cur.fetchall()]
        conn.close()
        return json.dumps({"entity_type": entity_type, "metric": metric,
                           "time_period": time_period, "data": result})

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


if __name__ == "__main__":
    mcp.run()

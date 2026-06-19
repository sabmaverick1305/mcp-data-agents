"""
Streamlit dashboard — Revenue Analytics and Agent Operations monitoring.

Two-tab Streamlit application that visualises both the underlying business data
(from data/warehouse.db) and the agent system's operational metrics (from
data/cost_ledger.db). Auto-refreshes warehouse data every 30 s and ledger
data every 15 s via Streamlit's @st.cache_data(ttl=...) mechanism.

Tab 1 — Revenue Analytics (warehouse.db):
  KPI row            Total revenue, gross profit, avg margin %, best quarter
  Quarterly chart    Grouped bar (revenue + gross profit) + margin % line on secondary axis
  YoY comparison     Side-by-side bar: 2023 vs 2024 by quarter
  Segment pie        Revenue share by customer segment (Enterprise / Mid-Market / SMB)
  Regional stacked   Revenue by region per quarter (stacked bar)
  Category bar       Horizontal bar by product category (Software / Infra / Security / Services)
  Monthly line       Revenue trend line per month, coloured by year
  Top-8 products     Sortable dataframe with revenue, gross profit, units sold

Tab 2 — Agent Operations (cost_ledger.db):
  KPI row            Total queries, LLM spend, cache hit rate, avg latency, avoided cost
  Cumulative cost    Area chart of spend over time
  Cache hit/miss     Donut chart
  Agent routing      Bar chart — how many times each agent was invoked
  Latency histogram  Distribution of end-to-end query latency
  Per-agent cost     Bar (cost per agent) + grouped bar (input vs output tokens)
  Feedback           Bar chart — good / bad / unrated counts
  Recent queries     Dataframe of last 20 queries with time, agents, cost, cache, feedback

Launch:
  python -m streamlit run dashboard.py
  # or via Docker Compose — bound to localhost:8501 by default
"""
import json
import os
import sqlite3
from datetime import datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

WAREHOUSE_DB = os.path.join(os.path.dirname(__file__), "data", "warehouse.db")
LEDGER_DB    = os.path.join(os.path.dirname(__file__), "data", "cost_ledger.db")

st.set_page_config(
    page_title="MCP Data Agents",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
  .metric-card {
    background: #1e1e2e;
    border-radius: 10px;
    padding: 16px 20px;
    border: 1px solid #313244;
  }
  [data-testid="stMetric"] { background: #1e1e2e; border-radius: 8px; padding: 12px; }
  [data-testid="stMetric"] label,
  [data-testid="stMetricLabel"] p,
  [data-testid="stMetricLabel"] { color: #ffffff !important; }
  [data-testid="stMetricValue"],
  [data-testid="stMetricValue"] > div { color: #ffffff !important; }
  [data-testid="stMetricDelta"] { color: #ffffff !important; }
</style>
""", unsafe_allow_html=True)


# ── Data loaders ──────────────────────────────────────────────────────────────

@st.cache_data(ttl=30)
def load_warehouse() -> dict[str, pd.DataFrame]:
    conn = sqlite3.connect(WAREHOUSE_DB)

    quarterly = pd.read_sql_query("""
        SELECT d.year, d.quarter,
               PRINTF('%d Q%d', d.year, d.quarter) AS period,
               ROUND(SUM(s.revenue), 2)      AS revenue,
               ROUND(SUM(s.gross_profit), 2) AS gross_profit,
               ROUND(SUM(s.cost), 2)         AS cost,
               COUNT(*)                      AS transactions
        FROM sales_fact s JOIN date_dim d ON s.date_id = d.date_id
        GROUP BY d.year, d.quarter
        ORDER BY d.year, d.quarter
    """, conn)
    quarterly["gross_margin_pct"] = (
        quarterly["gross_profit"] / quarterly["revenue"] * 100
    ).round(1)

    by_region = pd.read_sql_query("""
        SELECT d.year, d.quarter,
               PRINTF('%d Q%d', d.year, d.quarter) AS period,
               r.region_name,
               ROUND(SUM(s.revenue), 2) AS revenue
        FROM sales_fact s
        JOIN date_dim   d ON s.date_id   = d.date_id
        JOIN region_dim r ON s.region_id = r.region_id
        GROUP BY d.year, d.quarter, r.region_name
        ORDER BY d.year, d.quarter, r.region_name
    """, conn)

    by_category = pd.read_sql_query("""
        SELECT d.year, d.quarter,
               PRINTF('%d Q%d', d.year, d.quarter) AS period,
               p.category,
               ROUND(SUM(s.revenue), 2) AS revenue
        FROM sales_fact s
        JOIN date_dim    d ON s.date_id    = d.date_id
        JOIN product_dim p ON s.product_id = p.product_id
        GROUP BY d.year, d.quarter, p.category
        ORDER BY d.year, d.quarter, p.category
    """, conn)

    by_segment = pd.read_sql_query("""
        SELECT c.segment,
               ROUND(SUM(s.revenue), 2) AS revenue,
               COUNT(DISTINCT s.customer_id) AS customers
        FROM sales_fact s
        JOIN customer_dim c ON s.customer_id = c.customer_id
        GROUP BY c.segment
        ORDER BY revenue DESC
    """, conn)

    top_products = pd.read_sql_query("""
        SELECT p.product_name, p.category,
               ROUND(SUM(s.revenue), 2)  AS revenue,
               ROUND(SUM(s.gross_profit), 2) AS profit,
               SUM(s.quantity)           AS units
        FROM sales_fact s
        JOIN product_dim p ON s.product_id = p.product_id
        GROUP BY p.product_name, p.category
        ORDER BY revenue DESC
        LIMIT 8
    """, conn)

    monthly = pd.read_sql_query("""
        SELECT d.year, d.month, d.month_name,
               ROUND(SUM(s.revenue), 2) AS revenue
        FROM sales_fact s JOIN date_dim d ON s.date_id = d.date_id
        GROUP BY d.year, d.month, d.month_name
        ORDER BY d.year, d.month
    """, conn)
    monthly["label"] = monthly["month_name"] + " " + monthly["year"].astype(str)

    conn.close()
    return dict(
        quarterly=quarterly,
        by_region=by_region,
        by_category=by_category,
        by_segment=by_segment,
        top_products=top_products,
        monthly=monthly,
    )


@st.cache_data(ttl=15)
def load_ledger() -> dict[str, pd.DataFrame]:
    if not os.path.exists(LEDGER_DB):
        return {}

    conn = sqlite3.connect(LEDGER_DB)

    queries = pd.read_sql_query(
        "SELECT * FROM query_costs ORDER BY timestamp DESC", conn
    )
    conn.close()

    if queries.empty:
        return {"queries": queries}

    queries["dt"] = pd.to_datetime(queries["timestamp"], unit="s")
    queries["date"] = queries["dt"].dt.date
    queries["hour"] = queries["dt"].dt.floor("h")

    # Expand agent_breakdown JSON → per-agent cost rows
    agent_rows = []
    for _, row in queries.iterrows():
        if row["agent_breakdown"]:
            try:
                bd = json.loads(row["agent_breakdown"])
                for agent, stats in bd.items():
                    agent_rows.append({
                        "timestamp": row["timestamp"],
                        "agent": agent,
                        "cost_usd": stats.get("cost_usd", 0),
                        "input_tokens": stats.get("input_tokens", 0),
                        "output_tokens": stats.get("output_tokens", 0),
                    })
            except Exception:
                pass

    agent_df = pd.DataFrame(agent_rows) if agent_rows else pd.DataFrame()
    return {"queries": queries, "agent_df": agent_df}


# ── Page header ───────────────────────────────────────────────────────────────

st.title("📊 MCP Data Agents — Dashboard")
st.caption("Revenue analytics + agent operations · auto-refreshes every 30 s")

tab_rev, tab_ops = st.tabs(["💰 Revenue Analytics", "🤖 Agent Operations"])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — REVENUE ANALYTICS
# ══════════════════════════════════════════════════════════════════════════════

with tab_rev:
    wh = load_warehouse()
    q  = wh["quarterly"]

    # KPI row
    total_rev   = q["revenue"].sum()
    total_profit = q["gross_profit"].sum()
    avg_margin  = (total_profit / total_rev * 100) if total_rev else 0
    best_q      = q.loc[q["revenue"].idxmax(), "period"]

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Total Revenue",     f"${total_rev/1e6:.2f}M")
    k2.metric("Gross Profit",      f"${total_profit/1e6:.2f}M")
    k3.metric("Avg Gross Margin",  f"{avg_margin:.1f}%")
    k4.metric("Best Quarter",      best_q)

    st.markdown("---")

    # ── Revenue by Quarter ────────────────────────────────────────────────────
    st.subheader("Revenue & Gross Profit by Quarter")

    fig_q = make_subplots(specs=[[{"secondary_y": True}]])
    fig_q.add_trace(go.Bar(
        x=q["period"], y=q["revenue"],
        name="Revenue", marker_color="#7c3aed",
        text=q["revenue"].apply(lambda v: f"${v/1e6:.2f}M"),
        textposition="outside",
    ), secondary_y=False)
    fig_q.add_trace(go.Bar(
        x=q["period"], y=q["gross_profit"],
        name="Gross Profit", marker_color="#06b6d4",
        text=q["gross_profit"].apply(lambda v: f"${v/1e6:.2f}M"),
        textposition="outside",
    ), secondary_y=False)
    fig_q.add_trace(go.Scatter(
        x=q["period"], y=q["gross_margin_pct"],
        name="Margin %", mode="lines+markers",
        line=dict(color="#f59e0b", width=2.5),
        marker=dict(size=8),
    ), secondary_y=True)
    fig_q.update_layout(
        barmode="group", height=420,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(t=40, b=20),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    fig_q.update_yaxes(title_text="USD", secondary_y=False, tickprefix="$")
    fig_q.update_yaxes(title_text="Margin %", secondary_y=True, ticksuffix="%")
    st.plotly_chart(fig_q, use_container_width=True)

    # ── YoY comparison ────────────────────────────────────────────────────────
    col_yoy, col_seg = st.columns(2)

    with col_yoy:
        st.subheader("YoY Revenue by Quarter")
        yoy = q.copy()
        yoy["Quarter"] = "Q" + yoy["quarter"].astype(str)
        fig_yoy = px.bar(
            yoy, x="Quarter", y="revenue", color="year",
            barmode="group", color_discrete_sequence=["#7c3aed", "#06b6d4"],
            text_auto=".2s",
            labels={"revenue": "Revenue (USD)", "year": "Year"},
        )
        fig_yoy.update_layout(
            height=360, margin=dict(t=20, b=20),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        fig_yoy.update_traces(textposition="outside")
        st.plotly_chart(fig_yoy, use_container_width=True)

    with col_seg:
        st.subheader("Revenue by Customer Segment")
        seg = wh["by_segment"]
        fig_seg = px.pie(
            seg, names="segment", values="revenue",
            color_discrete_sequence=px.colors.qualitative.Set2,
            hole=0.45,
        )
        fig_seg.update_layout(
            height=360, margin=dict(t=20, b=20),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig_seg, use_container_width=True)

    # ── Revenue by Region ─────────────────────────────────────────────────────
    st.subheader("Revenue by Region — Quarterly")
    reg = wh["by_region"]
    fig_reg = px.bar(
        reg, x="period", y="revenue", color="region_name",
        barmode="stack", text_auto=".2s",
        color_discrete_sequence=px.colors.qualitative.Pastel,
        labels={"revenue": "Revenue (USD)", "period": "Quarter", "region_name": "Region"},
    )
    fig_reg.update_layout(
        height=380, margin=dict(t=20, b=20),
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    st.plotly_chart(fig_reg, use_container_width=True)

    # ── Category + Monthly ────────────────────────────────────────────────────
    col_cat, col_mon = st.columns(2)

    with col_cat:
        st.subheader("Revenue by Product Category")
        cat = wh["by_category"].groupby("category")["revenue"].sum().reset_index()
        fig_cat = px.bar(
            cat.sort_values("revenue", ascending=True),
            x="revenue", y="category", orientation="h",
            color="revenue", color_continuous_scale="Purples",
            text_auto=".2s",
            labels={"revenue": "Revenue (USD)", "category": ""},
        )
        fig_cat.update_layout(
            height=320, margin=dict(t=20, b=20),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            coloraxis_showscale=False,
        )
        st.plotly_chart(fig_cat, use_container_width=True)

    with col_mon:
        st.subheader("Monthly Revenue Trend")
        mon = wh["monthly"]
        fig_mon = px.line(
            mon, x="label", y="revenue",
            color="year", color_discrete_sequence=["#7c3aed", "#06b6d4"],
            markers=True,
            labels={"revenue": "Revenue (USD)", "label": "Month"},
        )
        fig_mon.update_layout(
            height=320, margin=dict(t=20, b=20),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            xaxis_tickangle=-45,
        )
        st.plotly_chart(fig_mon, use_container_width=True)

    # ── Top products table ────────────────────────────────────────────────────
    st.subheader("Top Products by Revenue")
    tp = wh["top_products"].copy()
    tp["revenue"]      = tp["revenue"].apply(lambda v: f"${v:,.0f}")
    tp["profit"]       = tp["profit"].apply(lambda v: f"${v:,.0f}")
    tp["units"]        = tp["units"].apply(lambda v: f"{v:,}")
    tp.columns         = ["Product", "Category", "Revenue", "Gross Profit", "Units Sold"]
    st.dataframe(tp, use_container_width=True, hide_index=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — AGENT OPERATIONS
# ══════════════════════════════════════════════════════════════════════════════

with tab_ops:
    ledger = load_ledger()

    if not ledger or ledger.get("queries", pd.DataFrame()).empty:
        st.info("No query data yet. Run some queries through the CLI or API to see metrics here.")
        st.stop()

    df = ledger["queries"]

    # KPI row
    total_queries  = len(df)
    total_cost     = df["cost_usd"].sum()
    cache_hit_rate = df["cache_hit"].mean() * 100
    avg_latency    = df["latency_s"].mean()
    total_avoided  = df["avoided_cost_usd"].sum()

    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Total Queries",    f"{total_queries:,}")
    k2.metric("Total LLM Spend",  f"${total_cost:.4f}")
    k3.metric("Cache Hit Rate",   f"{cache_hit_rate:.1f}%")
    k4.metric("Avg Latency",      f"{avg_latency:.1f}s")
    k5.metric("Cost Avoided",     f"${total_avoided:.4f}")

    st.markdown("---")

    # ── Cost over time ────────────────────────────────────────────────────────
    col_cost, col_cache = st.columns(2)

    with col_cost:
        st.subheader("Cumulative LLM Cost")
        cost_ts = df.sort_values("timestamp")[["dt", "cost_usd"]].copy()
        cost_ts["cumulative"] = cost_ts["cost_usd"].cumsum()
        fig_cost = px.area(
            cost_ts, x="dt", y="cumulative",
            color_discrete_sequence=["#7c3aed"],
            labels={"dt": "Time", "cumulative": "Cumulative Cost (USD)"},
        )
        fig_cost.update_layout(
            height=320, margin=dict(t=20, b=20),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig_cost, use_container_width=True)

    with col_cache:
        st.subheader("Cache Hit vs Miss")
        cache_counts = df["cache_hit"].map({1: "Cache Hit", 0: "Cache Miss"}).value_counts().reset_index()
        cache_counts.columns = ["result", "count"]
        fig_cache = px.pie(
            cache_counts, names="result", values="count",
            color="result",
            color_discrete_map={"Cache Hit": "#06b6d4", "Cache Miss": "#7c3aed"},
            hole=0.45,
        )
        fig_cache.update_layout(
            height=320, margin=dict(t=20, b=20),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig_cache, use_container_width=True)

    # ── Agent routing ─────────────────────────────────────────────────────────
    col_agents, col_lat = st.columns(2)

    with col_agents:
        st.subheader("Agent Routing Distribution")
        agent_counts = (
            df["agents"]
            .str.split(",")
            .explode()
            .str.strip()
            .value_counts()
            .reset_index()
        )
        agent_counts.columns = ["agent", "count"]
        fig_agents = px.bar(
            agent_counts, x="agent", y="count",
            color="agent", text_auto=True,
            color_discrete_sequence=px.colors.qualitative.Set2,
            labels={"agent": "Agent", "count": "Times Invoked"},
        )
        fig_agents.update_layout(
            height=320, showlegend=False, margin=dict(t=20, b=20),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig_agents, use_container_width=True)

    with col_lat:
        st.subheader("Query Latency Distribution")
        fig_lat = px.histogram(
            df, x="latency_s", nbins=20,
            color_discrete_sequence=["#f59e0b"],
            labels={"latency_s": "Latency (s)", "count": "Queries"},
        )
        fig_lat.update_layout(
            height=320, margin=dict(t=20, b=20),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig_lat, use_container_width=True)

    # ── Per-agent cost breakdown ───────────────────────────────────────────────
    agent_df = ledger.get("agent_df", pd.DataFrame())
    if not agent_df.empty:
        st.subheader("Cost & Token Usage by Agent")
        agg = agent_df.groupby("agent").agg(
            cost_usd=("cost_usd", "sum"),
            input_tokens=("input_tokens", "sum"),
            output_tokens=("output_tokens", "sum"),
        ).reset_index()

        col_ac, col_tok = st.columns(2)
        with col_ac:
            fig_ac = px.bar(
                agg, x="agent", y="cost_usd", color="agent",
                text_auto=".4f", color_discrete_sequence=px.colors.qualitative.Pastel,
                labels={"cost_usd": "Cost (USD)", "agent": "Agent"},
            )
            fig_ac.update_layout(
                height=300, showlegend=False, margin=dict(t=20, b=20),
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            )
            st.plotly_chart(fig_ac, use_container_width=True)

        with col_tok:
            fig_tok = go.Figure()
            fig_tok.add_trace(go.Bar(name="Input", x=agg["agent"], y=agg["input_tokens"],
                                     marker_color="#7c3aed"))
            fig_tok.add_trace(go.Bar(name="Output", x=agg["agent"], y=agg["output_tokens"],
                                     marker_color="#06b6d4"))
            fig_tok.update_layout(
                barmode="group", height=300, margin=dict(t=20, b=20),
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                legend=dict(orientation="h", yanchor="bottom", y=1.02),
            )
            st.plotly_chart(fig_tok, use_container_width=True)

    # ── Feedback breakdown ────────────────────────────────────────────────────
    st.subheader("Query Feedback")
    feedback = df["feedback"].fillna("unrated").value_counts().reset_index()
    feedback.columns = ["rating", "count"]
    color_map = {"good": "#22c55e", "bad": "#ef4444", "unrated": "#6b7280"}
    fig_fb = px.bar(
        feedback, x="rating", y="count", color="rating",
        color_discrete_map=color_map, text_auto=True,
        labels={"rating": "Rating", "count": "Queries"},
    )
    fig_fb.update_layout(
        height=280, showlegend=False, margin=dict(t=20, b=20),
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
    )
    st.plotly_chart(fig_fb, use_container_width=True)

    # ── Recent query history ──────────────────────────────────────────────────
    st.subheader("Recent Queries")
    recent = df.head(20)[["dt", "agents", "cost_usd", "cache_hit", "latency_s",
                           "plan_confidence", "feedback"]].copy()
    recent["dt"]        = recent["dt"].dt.strftime("%Y-%m-%d %H:%M")
    recent["cost_usd"]  = recent["cost_usd"].apply(lambda v: f"${v:.4f}")
    recent["cache_hit"] = recent["cache_hit"].map({1: "✅ Hit", 0: "❌ Miss"})
    recent["latency_s"] = recent["latency_s"].apply(lambda v: f"{v:.1f}s")
    recent["feedback"]  = recent["feedback"].fillna("—")
    recent.columns      = ["Time", "Agents", "Cost", "Cache", "Latency", "Confidence", "Feedback"]
    st.dataframe(recent, use_container_width=True, hide_index=True)

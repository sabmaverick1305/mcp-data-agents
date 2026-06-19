"""
SQLite-backed cost ledger for enterprise cost visibility.

Stores one row per query with:
  - Tenant / user / team attribution
  - Per-agent token breakdown
  - Cache hit + avoided cost
  - Plan confidence
  - Feedback signal

Aggregation queries:
  summary()    → totals + cache ROI over a time window
  by_team()    → chargeback breakdown by team
  cache_roi()  → total avoided spend and cache hit rate
  by_agent()   → which agents are driving the most cost
"""
import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager

_DB_PATH = os.path.join(os.path.dirname(__file__), "data", "cost_ledger.db")


class CostLedger:
    def __init__(self, db_path: str = _DB_PATH):
        self._db_path = db_path
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS query_costs (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp        REAL    NOT NULL,
                    tenant_id        TEXT    NOT NULL DEFAULT 'default',
                    user_id          TEXT,
                    team_id          TEXT,
                    question_hash    TEXT,
                    agents           TEXT,
                    input_tokens     INTEGER DEFAULT 0,
                    output_tokens    INTEGER DEFAULT 0,
                    cost_usd         REAL    DEFAULT 0,
                    cache_hit        INTEGER DEFAULT 0,
                    avoided_cost_usd REAL    DEFAULT 0,
                    feedback         TEXT,
                    agent_breakdown  TEXT,
                    plan_confidence  TEXT    DEFAULT 'high',
                    latency_s        REAL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tenant_time "
                "ON query_costs(tenant_id, timestamp)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_team "
                "ON query_costs(team_id, timestamp)"
            )

    # ── Write ─────────────────────────────────────────────────────────────────

    def record(self, trace) -> None:
        """Persist a QueryTrace to the ledger."""
        q_hash = hashlib.md5(
            trace.question.lower().strip().encode()
        ).hexdigest()[:16]

        agent_breakdown = json.dumps(
            {k: v.to_dict() for k, v in trace.agent_costs.items()}
        )

        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO query_costs
                  (timestamp, tenant_id, user_id, team_id, question_hash,
                   agents, input_tokens, output_tokens, cost_usd,
                   cache_hit, avoided_cost_usd, feedback,
                   agent_breakdown, plan_confidence, latency_s)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    trace.started_at,
                    trace.tenant_id,
                    trace.user_id,
                    trace.team_id,
                    q_hash,
                    ",".join(trace.agents_invoked),
                    trace.input_tokens,
                    trace.output_tokens,
                    trace.cost,
                    int(trace.cache_hit),
                    trace.avoided_cost_usd,
                    trace.feedback,
                    agent_breakdown,
                    trace.plan_confidence,
                    trace.latency,
                ),
            )

    # ── Read ──────────────────────────────────────────────────────────────────

    def summary(
        self,
        tenant_id: str | None = None,
        start_ts: float | None = None,
        end_ts: float | None = None,
    ) -> dict:
        """Aggregate cost metrics over an optional time window."""
        where, params = self._where(tenant_id, start_ts, end_ts)
        with self._conn() as conn:
            row = conn.execute(
                f"""
                SELECT
                    COUNT(*)                          AS total_queries,
                    SUM(cache_hit)                    AS cache_hits,
                    SUM(input_tokens)                 AS total_input_tokens,
                    SUM(output_tokens)                AS total_output_tokens,
                    SUM(cost_usd)                     AS total_cost_usd,
                    SUM(avoided_cost_usd)             AS total_avoided_usd,
                    AVG(latency_s)                    AS avg_latency_s,
                    SUM(CASE WHEN feedback='bad'  THEN 1 ELSE 0 END) AS bad_feedback,
                    SUM(CASE WHEN feedback='good' THEN 1 ELSE 0 END) AS good_feedback,
                    SUM(CASE WHEN plan_confidence='fallback' THEN 1 ELSE 0 END)
                                                      AS planner_fallbacks
                FROM query_costs {where}
                """,
                params,
            ).fetchone()

        total   = row["total_queries"] or 0
        hits    = row["cache_hits"] or 0
        return {
            "total_queries":       total,
            "cache_hits":          hits,
            "cache_hit_rate":      round(hits / total, 3) if total else 0.0,
            "total_input_tokens":  row["total_input_tokens"] or 0,
            "total_output_tokens": row["total_output_tokens"] or 0,
            "total_cost_usd":      round(row["total_cost_usd"] or 0, 4),
            "total_avoided_usd":   round(row["total_avoided_usd"] or 0, 4),
            "avg_latency_s":       round(row["avg_latency_s"] or 0, 2),
            "bad_feedback":        row["bad_feedback"] or 0,
            "good_feedback":       row["good_feedback"] or 0,
            "planner_fallbacks":   row["planner_fallbacks"] or 0,
        }

    def by_team(
        self,
        tenant_id: str | None = None,
        start_ts: float | None = None,
        end_ts: float | None = None,
    ) -> list[dict]:
        """Cost breakdown by team — primary chargeback report."""
        where, params = self._where(tenant_id, start_ts, end_ts)
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    COALESCE(team_id, 'unattributed')  AS team,
                    COUNT(*)                            AS queries,
                    SUM(cost_usd)                       AS cost_usd,
                    SUM(avoided_cost_usd)               AS avoided_usd,
                    SUM(cache_hit)                      AS cache_hits,
                    AVG(latency_s)                      AS avg_latency_s
                FROM query_costs {where}
                GROUP BY team
                ORDER BY cost_usd DESC
                """,
                params,
            ).fetchall()

        return [
            {
                "team":          r["team"],
                "queries":       r["queries"],
                "cost_usd":      round(r["cost_usd"] or 0, 4),
                "avoided_usd":   round(r["avoided_usd"] or 0, 4),
                "cache_hits":    r["cache_hits"],
                "avg_latency_s": round(r["avg_latency_s"] or 0, 2),
            }
            for r in rows
        ]

    def cache_roi(self, tenant_id: str | None = None) -> dict:
        """Total avoided spend + effective cache hit rate — use for budget conversations."""
        where, params = self._where(tenant_id)
        with self._conn() as conn:
            row = conn.execute(
                f"""
                SELECT
                    COUNT(*)                AS total,
                    SUM(cache_hit)          AS hits,
                    SUM(avoided_cost_usd)   AS avoided_usd,
                    SUM(cost_usd)           AS actual_usd
                FROM query_costs {where}
                """,
                params,
            ).fetchone()

        total    = row["total"] or 0
        hits     = row["hits"]  or 0
        avoided  = round(row["avoided_usd"] or 0, 4)
        actual   = round(row["actual_usd"] or 0, 4)
        return {
            "total_queries":    total,
            "cache_hits":       hits,
            "cache_hit_rate":   round(hits / total, 3) if total else 0.0,
            "actual_cost_usd":  actual,
            "avoided_cost_usd": avoided,
            "gross_cost_usd":   round(actual + avoided, 4),
            "roi_pct":          round(avoided / (actual + avoided) * 100, 1) if (actual + avoided) else 0.0,
        }

    def by_agent(
        self,
        tenant_id: str | None = None,
        start_ts: float | None = None,
        end_ts: float | None = None,
    ) -> list[dict]:
        """
        Aggregate cost per agent by unpacking the agent_breakdown JSON column.
        Returns sorted list (most expensive agent first).
        """
        where, params = self._where(tenant_id, start_ts, end_ts)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT agent_breakdown FROM query_costs {where} "
                "WHERE agent_breakdown IS NOT NULL AND agent_breakdown != '{}'",
                params,
            ).fetchall()

        totals: dict[str, dict] = {}
        for row in rows:
            try:
                breakdown = json.loads(row["agent_breakdown"])
            except (json.JSONDecodeError, TypeError):
                continue
            for agent, data in breakdown.items():
                if agent not in totals:
                    totals[agent] = {"queries": 0, "input_tokens": 0,
                                     "output_tokens": 0, "cost_usd": 0.0}
                totals[agent]["queries"]       += 1
                totals[agent]["input_tokens"]  += data.get("input_tokens", 0)
                totals[agent]["output_tokens"] += data.get("output_tokens", 0)
                totals[agent]["cost_usd"]      += data.get("cost_usd", 0.0)

        return sorted(
            [
                {
                    "agent":         name,
                    "queries":       v["queries"],
                    "input_tokens":  v["input_tokens"],
                    "output_tokens": v["output_tokens"],
                    "cost_usd":      round(v["cost_usd"], 5),
                }
                for name, v in totals.items()
            ],
            key=lambda x: x["cost_usd"],
            reverse=True,
        )

    def rolling_avg_cost(self, tenant_id: str | None = None, n: int = 100) -> float:
        """Average cost of the last n non-cached queries — used for avoided_cost_usd estimate."""
        where, params = self._where(tenant_id)
        base_where = where + (" AND" if where else " WHERE") + " cache_hit = 0"
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT AVG(cost_usd) FROM "
                f"(SELECT cost_usd FROM query_costs {base_where} "
                f"ORDER BY timestamp DESC LIMIT {n})",
                params,
            ).fetchone()
        return row[0] or 0.02  # fall back to constant if no history

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _where(
        tenant_id: str | None = None,
        start_ts: float | None = None,
        end_ts: float | None = None,
    ) -> tuple[str, list]:
        clauses, params = [], []
        if tenant_id:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if start_ts:
            clauses.append("timestamp >= ?")
            params.append(start_ts)
        if end_ts:
            clauses.append("timestamp <= ?")
            params.append(end_ts)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

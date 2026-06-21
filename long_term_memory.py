"""
Long-term memory store for MCP Data Agents.

STATE & MEMORY LAYER — Long-Term Memory sub-component.

Unlike the short-term Redis session history (7-day rolling window) or the ChromaDB
semantic cache (24-hour TTL), long-term memory accumulates knowledge that is useful
indefinitely:

  Preferences   — Per-user settings (answer depth, preferred regions, output style).
                  Stored in SQLite (data/long_term_memory.db), keyed by tenant+user+key.
                  No TTL; updated in-place on every set.

  Analyses      — Semantic embeddings of past questions the user has explored.
                  Stored in ChromaDB collection ltm_analyses_{tenant_id}.
                  Enables "have we investigated this before?" recall for the planner.
                  Capped at ANALYSIS_MAX_ENTRIES per tenant; oldest evicted when full.

  Patterns      — Domain observations accumulated from repeated query evidence
                  (e.g. "Q1 always has low transaction volume").
                  Stored in SQLite, ranked by evidence_count.
                  Surface the top patterns into every planner call.

Primary consumer: get_context_for_planner()
  Called in _run_pipeline() before planning. Returns a formatted string injected
  into the planner's dynamic context block (uncached). Combines:
    - Semantically similar past analyses (distance < 0.60)
    - Top evidence-backed domain patterns
    - User preference key-value pairs

Usage:
  ltm = LongTermMemory()

  # Store
  ltm.store_preference("acme", "alice", "answer_depth", "detailed")
  ltm.store_analysis("acme", question, answer_summary, ["semantic", "insight"])
  ltm.store_pattern("acme", "Q1 revenue is always low due to sparse transactions")

  # Retrieve
  ctx = ltm.get_context_for_planner("acme", "alice", "What happened in Q1?")
  # → "[Long-term memory...]\n• Similar past question: ...\n[Patterns]\n• ..."

  analyses = ltm.recall_analyses("acme", "revenue drop 2024", limit=3)
  patterns = ltm.recall_patterns("acme", limit=5)
  prefs    = ltm.get_preferences("acme", "alice")

Environment:
  CHROMA_HOST / CHROMA_PORT — inherited from rag.chroma_client (same ChromaDB instance)
  LTM_DB_PATH               — override SQLite path (default: data/long_term_memory.db)
"""
import hashlib
import os
import sqlite3
import time
from typing import Optional

from rag.chroma_client import CHROMA_DIR, get_client

_DEFAULT_LTM_DB = os.path.join(os.path.dirname(CHROMA_DIR), "long_term_memory.db")
LTM_DB_PATH     = os.environ.get("LTM_DB_PATH", _DEFAULT_LTM_DB)

ANALYSIS_MAX_ENTRIES  = 200    # per tenant; oldest evicted when exceeded
ANALYSIS_DISTANCE_CAP = 0.60   # cosine distance above this is considered unrelated


# ── Schema ────────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(LTM_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_schema() -> None:
    with _db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ltm_preferences (
                tenant_id   TEXT    NOT NULL,
                user_id     TEXT    NOT NULL,
                key         TEXT    NOT NULL,
                value       TEXT    NOT NULL,
                updated_at  REAL    NOT NULL,
                PRIMARY KEY (tenant_id, user_id, key)
            );

            CREATE TABLE IF NOT EXISTS ltm_patterns (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id       TEXT    NOT NULL,
                pattern         TEXT    NOT NULL,
                evidence_count  INTEGER DEFAULT 1,
                first_seen_at   REAL    NOT NULL,
                last_seen_at    REAL    NOT NULL,
                UNIQUE (tenant_id, pattern)
            );

            CREATE INDEX IF NOT EXISTS idx_patterns_tenant_evidence
                ON ltm_patterns (tenant_id, evidence_count DESC);
        """)


# ── LongTermMemory ────────────────────────────────────────────────────────────

class LongTermMemory:
    """
    Persistent cross-session memory: user preferences, past analyses, domain patterns.

    SQLite is used for preferences and patterns (lightweight, no extra deps, ACID).
    ChromaDB is used for analyses (semantic search over past questions).
    """

    def __init__(self):
        _init_schema()
        self._chroma = get_client()

    # ── Preferences ───────────────────────────────────────────────────────────

    def store_preference(
        self,
        tenant_id: str,
        user_id: str,
        key: str,
        value: str,
    ) -> None:
        """Upsert a user preference. Replaces the previous value for the same key."""
        with _db() as conn:
            conn.execute(
                """INSERT INTO ltm_preferences
                       (tenant_id, user_id, key, value, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (tenant_id, user_id, key)
                   DO UPDATE SET value = excluded.value,
                                 updated_at = excluded.updated_at""",
                (tenant_id, user_id, key, value, time.time()),
            )

    def get_preferences(self, tenant_id: str, user_id: str) -> dict[str, str]:
        """Return all preferences for a user as {key: value}."""
        with _db() as conn:
            rows = conn.execute(
                "SELECT key, value FROM ltm_preferences "
                "WHERE tenant_id = ? AND user_id = ?",
                (tenant_id, user_id),
            ).fetchall()
        return {r["key"]: r["value"] for r in rows}

    def delete_preference(self, tenant_id: str, user_id: str, key: str) -> None:
        with _db() as conn:
            conn.execute(
                "DELETE FROM ltm_preferences WHERE tenant_id=? AND user_id=? AND key=?",
                (tenant_id, user_id, key),
            )

    # ── Analyses ──────────────────────────────────────────────────────────────

    def _analyses_coll(self, tenant_id: str):
        return self._chroma.get_or_create_collection(
            f"ltm_analyses_{tenant_id}",
            metadata={"hnsw:space": "cosine"},
        )

    def store_analysis(
        self,
        tenant_id: str,
        question: str,
        summary: str,
        agents_used: list[str],
        tags: Optional[list[str]] = None,
    ) -> None:
        """
        Store a completed Q&A pair for long-term semantic recall.

        The question is used as the embedding document (so future similar
        questions can surface this past analysis). The summary is stored
        in metadata for display.
        """
        coll   = self._analyses_coll(tenant_id)
        doc_id = "ltma_" + hashlib.md5(question.lower().strip().encode()).hexdigest()[:16]
        meta   = {
            "question":  question[:300],
            "summary":   summary[:500],
            "agents":    ",".join(agents_used),
            "tags":      ",".join(tags or []),
            "stored_at": time.time(),
        }
        coll.upsert(ids=[doc_id], documents=[question], metadatas=[meta])

        # Evict oldest entries if over the per-tenant cap
        count = coll.count()
        if count > ANALYSIS_MAX_ENTRIES:
            result = coll.get(include=["metadatas"])
            oldest = sorted(
                zip(result["ids"], result["metadatas"]),
                key=lambda pair: pair[1].get("stored_at", 0),
            )
            evict = [doc_id for doc_id, _ in oldest[: count - ANALYSIS_MAX_ENTRIES]]
            if evict:
                coll.delete(ids=evict)

    def recall_analyses(
        self,
        tenant_id: str,
        query: str,
        limit: int = 3,
    ) -> list[dict]:
        """
        Return past analyses semantically similar to `query`.

        Each result: { question, summary, agents, stored_at, distance }.
        Only entries with cosine distance < ANALYSIS_DISTANCE_CAP are returned.
        """
        coll = self._analyses_coll(tenant_id)
        if coll.count() == 0:
            return []
        try:
            res = coll.query(
                query_texts=[query],
                n_results=min(limit, coll.count()),
                include=["distances", "metadatas"],
            )
            out: list[dict] = []
            for dist, meta in zip(res["distances"][0], res["metadatas"][0]):
                if dist < ANALYSIS_DISTANCE_CAP:
                    out.append({
                        "question":  meta.get("question", ""),
                        "summary":   meta.get("summary", ""),
                        "agents":    [a for a in meta.get("agents", "").split(",") if a],
                        "stored_at": meta.get("stored_at"),
                        "distance":  round(dist, 3),
                    })
            return out
        except Exception:
            return []

    def list_analyses(self, tenant_id: str, limit: int = 20) -> list[dict]:
        """Return the most recent analyses (by stored_at) for a tenant."""
        coll = self._analyses_coll(tenant_id)
        if coll.count() == 0:
            return []
        try:
            result = coll.get(include=["metadatas"])
            sorted_meta = sorted(
                result["metadatas"],
                key=lambda m: m.get("stored_at", 0),
                reverse=True,
            )
            return sorted_meta[:limit]
        except Exception:
            return []

    # ── Patterns ──────────────────────────────────────────────────────────────

    def store_pattern(
        self,
        tenant_id: str,
        pattern: str,
        evidence_count: int = 1,
    ) -> None:
        """
        Record or reinforce a domain pattern observation.

        If the exact pattern already exists for this tenant, its evidence_count
        is incremented and last_seen_at is refreshed. Otherwise a new row is created.
        """
        now = time.time()
        with _db() as conn:
            conn.execute(
                """INSERT INTO ltm_patterns
                       (tenant_id, pattern, evidence_count, first_seen_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (tenant_id, pattern)
                   DO UPDATE SET evidence_count = evidence_count + excluded.evidence_count,
                                 last_seen_at   = excluded.last_seen_at""",
                (tenant_id, pattern, evidence_count, now, now),
            )

    def recall_patterns(self, tenant_id: str, limit: int = 5) -> list[str]:
        """Return the top patterns ranked by evidence_count, most evidenced first."""
        with _db() as conn:
            rows = conn.execute(
                "SELECT pattern FROM ltm_patterns WHERE tenant_id = ? "
                "ORDER BY evidence_count DESC LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
        return [r["pattern"] for r in rows]

    def get_all_patterns(self, tenant_id: str) -> list[dict]:
        """Return all patterns with full metadata for the /memory/patterns endpoint."""
        with _db() as conn:
            rows = conn.execute(
                "SELECT pattern, evidence_count, first_seen_at, last_seen_at "
                "FROM ltm_patterns WHERE tenant_id = ? "
                "ORDER BY evidence_count DESC",
                (tenant_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_pattern(self, tenant_id: str, pattern: str) -> bool:
        with _db() as conn:
            result = conn.execute(
                "DELETE FROM ltm_patterns WHERE tenant_id = ? AND pattern = ?",
                (tenant_id, pattern),
            )
        return result.rowcount > 0

    # ── Planner context ───────────────────────────────────────────────────────

    def get_context_for_planner(
        self,
        tenant_id: str,
        user_id: Optional[str],
        question: str,
    ) -> str:
        """
        Return a formatted string for injection into the planner's dynamic context block.

        Combines semantically similar past analyses, top domain patterns, and user
        preferences. Returns an empty string if nothing relevant is found.
        """
        parts: list[str] = []

        # Past analyses — surface questions the user has already explored
        analyses = self.recall_analyses(tenant_id, question, limit=2)
        if analyses:
            lines = ["[Long-term memory — similar past questions]"]
            for a in analyses:
                label = f"• {a['question']}"
                if a.get("agents"):
                    label += f"  (via {', '.join(a['agents'])})"
                lines.append(label)
            parts.append("\n".join(lines))

        # Domain patterns — accumulated observations with evidence
        patterns = self.recall_patterns(tenant_id, limit=3)
        if patterns:
            lines = ["[Observed domain patterns]"]
            lines += [f"• {p}" for p in patterns]
            parts.append("\n".join(lines))

        # User preferences — inform answer style and focus
        if user_id:
            prefs = self.get_preferences(tenant_id, user_id)
            if prefs:
                lines = ["[User preferences]"]
                lines += [f"• {k}: {v}" for k, v in prefs.items()]
                parts.append("\n".join(lines))

        return "\n\n".join(parts)

"""
RAG vector store + semantic response cache.

Collection layout:
  domain_knowledge          — schema/metric docs, shared across all tenants (seeded at startup)
  qa_history_{tenant_id}    — per-tenant Q&A cache + semantic search history

Cache logic (cosine distance on question embeddings):
  < CACHE_THRESHOLD  → return cached answer, skip full pipeline
  < RAG_THRESHOLD    → inject as context, still run full pipeline

TTL:      cached answers older than CACHE_TTL_HOURS are ignored.
Rollback: flag_bad_since(ts) bulk-purges entries created after a timestamp.
Health:   cache_health() returns bad-feedback rate for auto-rollback decisions.
"""
import hashlib
import json
import os
import re
import time

from rag.chroma_client import CHROMA_DIR, get_client

_DATA_DIR    = os.path.dirname(CHROMA_DIR)
_AUDIT_FILE  = os.path.join(_DATA_DIR, "cache_audit.jsonl")

CACHE_THRESHOLD = 0.10
RAG_THRESHOLD   = 0.50
CACHE_TTL_HOURS = 24

_TEMPORAL_RE = re.compile(
    r'\b(20\d{2})\b'
    r'|'
    r'\bQ[1-4]\b'
    r'|'
    r'\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?'
    r'|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\b',
    re.IGNORECASE,
)

_DOMAIN_DOCS = [
    ("schema_sales_fact",
     "sales_fact table columns: sale_id, date_id, product_id, customer_id, "
     "region_id, quantity, revenue, cost, gross_profit. Each row is one transaction.",
     "schema"),
    ("schema_product_dim",
     "product_dim columns: product_id, product_name, category "
     "(Software / Infrastructure / Security / Services), subcategory, unit_price, unit_cost.",
     "schema"),
    ("schema_customer_dim",
     "customer_dim columns: customer_id, customer_name, "
     "segment (Enterprise / Mid-Market / SMB), country.",
     "schema"),
    ("schema_region_dim",
     "region_dim columns: region_id, region_name "
     "(North America / Europe / Asia Pacific / Latin America), "
     "country, manager, target_revenue.",
     "schema"),
    ("schema_date_dim",
     "date_dim columns: date_id (YYYY-MM-DD), year, quarter (1–4), "
     "month (1–12), month_name, week.",
     "schema"),
    ("metric_total_revenue",
     "Total Revenue = SUM(sales_fact.revenue). "
     "Power BI tool: get_metric(metric_name='total_revenue', time_period='2024-Q1', dimension='region'). "
     "Supports time_period: 'all', '2023', '2024', '2024-Q1', '2024-03'. "
     "Supports dimension: 'region', 'category', 'segment'.",
     "metric"),
    ("metric_gross_margin",
     "Gross Margin % = gross_profit / revenue × 100. "
     "Power BI tool: get_metric(metric_name='gross_margin_pct'). "
     "Typical range for this dataset is 85–90%.",
     "metric"),
    ("metric_growth",
     "Revenue growth: revenue_growth_mom (month-over-month) and "
     "revenue_growth_yoy (year-over-year quarterly). Both via Power BI get_metric().",
     "metric"),
    ("metric_customer",
     "Customer metrics: customer_ltv, revenue_per_customer, unique_customers. "
     "Available via Power BI get_metric().",
     "metric"),
    ("benchmark_regional",
     "Regional targets — North America: $5M, Europe: $3.5M, "
     "Asia Pacific: $2.8M, Latin America: $1.5M (annual). "
     "Compare via Tableau get_benchmark_data(benchmark_type='regional_vs_target').",
     "benchmark"),
    ("benchmark_types",
     "Tableau benchmark types: regional_vs_target, category_performance, "
     "segment_comparison, quarterly_trend. "
     "Top performers via get_top_performers(entity_type='products'|'customers'|'regions').",
     "benchmark"),
    ("business_context_q1_2024",
     "Q1 2024 revenue was ~$1.55M, a sharp drop from ~$5.9M in prior quarters. "
     "Q2 2024 recovered to ~$5.6M. "
     "When investigating the Q1 2024 dip, check transaction volume, not just revenue totals.",
     "business_context"),
]


def _temporal_tokens(text: str) -> set[str]:
    tokens = set()
    for m in _TEMPORAL_RE.finditer(text):
        tok = m.group().upper()
        tokens.add(tok[:3] if tok.isalpha() else tok)
    return tokens


def _history_file(tenant_id: str) -> str:
    return os.path.join(_DATA_DIR, f"conversation_history_{tenant_id}.json")


class RAGStore:
    def __init__(self, tenant_id: str = "default"):
        self._tenant_id = tenant_id
        self._client    = get_client()

        # Q&A cache is per-tenant; domain knowledge is shared
        self._qa  = self._client.get_or_create_collection(
            f"qa_history_{tenant_id}", metadata={"hnsw:space": "cosine"})
        self._dom = self._client.get_or_create_collection(
            "domain_knowledge", metadata={"hnsw:space": "cosine"})

    # ── Startup ───────────────────────────────────────────────────────────────

    def seed_domain(self):
        self._dom.upsert(
            ids=[d[0] for d in _DOMAIN_DOCS],
            documents=[d[1] for d in _DOMAIN_DOCS],
            metadatas=[{"type": d[2]} for d in _DOMAIN_DOCS],
        )

    # ── Write ─────────────────────────────────────────────────────────────────

    def store_qa(self, question: str, answer: str, agents_used: list[str]):
        doc_id   = "qa_" + hashlib.md5(question.lower().strip().encode()).hexdigest()[:16]
        cached_at = time.time()
        self._qa.upsert(
            ids=[doc_id],
            documents=[question],
            metadatas=[{
                "question":  question,
                "answer":    answer,
                "agents":    ",".join(agents_used),
                "cached_at": cached_at,
            }],
        )
        self._audit("stored", question, doc_id)

    def flag_bad(self, question: str):
        doc_id = "qa_" + hashlib.md5(question.lower().strip().encode()).hexdigest()[:16]
        try:
            self._qa.delete(ids=[doc_id])
            self._audit("flagged_bad", question, doc_id)
        except Exception:
            pass

    # ── Rollback ──────────────────────────────────────────────────────────────

    def flag_bad_since(self, cutoff_timestamp: float) -> int:
        """
        Bulk-remove all cache entries created at or after cutoff_timestamp.
        Use this to roll back a poisoned cache window.
        Returns the number of entries deleted.
        """
        if self._qa.count() == 0:
            return 0
        result = self._qa.get(include=["metadatas"])
        ids_to_delete = [
            doc_id
            for doc_id, meta in zip(result["ids"], result["metadatas"])
            if meta.get("cached_at", 0) >= cutoff_timestamp
        ]
        if ids_to_delete:
            self._qa.delete(ids=ids_to_delete)
            self._audit("bulk_rollback", f"cutoff={cutoff_timestamp}", f"{len(ids_to_delete)} entries")
        return len(ids_to_delete)

    def cache_health(self, window_secs: float = 3600.0) -> dict:
        """
        Compute bad-feedback rate over the last window_secs seconds from the audit log.
        Returns dict with keys: total_events, flagged_count, flag_rate, recommendation.
        A flag_rate > 0.20 suggests a rollback should be considered.
        """
        cutoff = time.time() - window_secs
        total, flagged = 0, 0

        if not os.path.exists(_AUDIT_FILE):
            return {"total_events": 0, "flagged_count": 0, "flag_rate": 0.0,
                    "recommendation": "ok", "cache_size": self._qa.count()}

        with open(_AUDIT_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("tenant_id") != self._tenant_id:
                    continue
                if ev.get("timestamp", 0) < cutoff:
                    continue
                total += 1
                if ev.get("event") == "flagged_bad":
                    flagged += 1

        flag_rate = flagged / total if total else 0.0
        recommendation = "rollback_advised" if flag_rate > 0.20 else "ok"
        return {
            "total_events":  total,
            "flagged_count": flagged,
            "flag_rate":     round(flag_rate, 3),
            "recommendation": recommendation,
            "cache_size":    self._qa.count(),
        }

    # ── Read ──────────────────────────────────────────────────────────────────

    def retrieve(self, question: str) -> tuple[str | None, str]:
        cached_answer = None
        context_parts: list[str] = []
        ttl_cutoff = time.time() - CACHE_TTL_HOURS * 3600

        if self._qa.count() > 0:
            qa_res = self._qa.query(
                query_texts=[question],
                n_results=min(5, self._qa.count()),
                include=["distances", "metadatas"],
            )
            incoming_temporal = _temporal_tokens(question)
            for dist, meta in zip(qa_res["distances"][0], qa_res["metadatas"][0]):
                age_ok = meta.get("cached_at", 0) > ttl_cutoff
                if dist < CACHE_THRESHOLD and age_ok and cached_answer is None:
                    cached_temporal = _temporal_tokens(meta.get("question", ""))
                    if incoming_temporal and cached_temporal and incoming_temporal != cached_temporal:
                        pass
                    else:
                        cached_answer = meta["answer"]
                        continue
                if dist < RAG_THRESHOLD and age_ok:
                    context_parts.append(
                        f"[Relevant past Q&A — similarity {1 - dist:.0%}]\n"
                        f"Q: {meta['question']}\nA: {meta['answer']}"
                    )

        if self._dom.count() > 0:
            dom_res = self._dom.query(
                query_texts=[question],
                n_results=5,
                include=["documents", "distances"],
            )
            for doc, dist in zip(dom_res["documents"][0], dom_res["distances"][0]):
                if dist < 0.60:
                    context_parts.append(f"[Domain knowledge]\n{doc}")

        return cached_answer, "\n\n".join(context_parts)

    # ── Conversation history ──────────────────────────────────────────────────

    def save_history(self, history: list[dict]) -> None:
        with open(_history_file(self._tenant_id), "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)

    def load_history(self) -> list[dict]:
        path = _history_file(self._tenant_id)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    # ── Stats ─────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        return {
            "qa_entries":  self._qa.count(),
            "domain_docs": self._dom.count(),
            "tenant_id":   self._tenant_id,
        }

    def list_qa(self, limit: int = 20) -> list[dict]:
        if self._qa.count() == 0:
            return []
        result = self._qa.get(limit=limit, include=["metadatas"])
        return [
            {
                "question":  m.get("question"),
                "agents":    m.get("agents"),
                "cached_at": m.get("cached_at"),
            }
            for m in result["metadatas"]
        ]

    # ── Internal audit log ────────────────────────────────────────────────────

    def _audit(self, event: str, question: str, doc_id: str) -> None:
        entry = {
            "timestamp": time.time(),
            "tenant_id": self._tenant_id,
            "event":     event,
            "question":  question[:120],
            "doc_id":    doc_id,
        }
        with open(_AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

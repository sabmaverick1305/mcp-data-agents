"""
Cache Registry — unified cache interface covering all five cache-layer concerns:

  Cache Registry          Single lookup path: Redis L1 → ChromaDB L2 (in order).
  Freshness Policy        Per-query-type TTL + temporal mismatch guard.
  Invalidation Policy     Cross-layer invalidation in one call; reason is logged.
  Similarity Threshold    Delegates to SimilarityRules (intent-aware thresholds).
  Partial Cache Reuse     Per-agent result cache in Redis so a multi-agent query
                          can reuse results from previous similar sessions.

Partial cache reuse — how it works:
  After each agent run, the result is stored in Redis under a key scoped to
  (tenant, agent_name, sha256(normalized_question)). On the next query, if
  the normalized question matches a cached agent key, that agent's result is
  returned as a prefilled_result and the agent is skipped in _run_pipeline.
  This is separate from the full-answer L1/L2 cache — it operates at agent
  granularity so "Q1 revenue" can reuse the semantic agent's Power BI result
  even when the surrounding context or follow-up differs.

Redis key scheme:
  mcp:cache:exact:{tenant}:{sha256(q)[:20]}        — full-answer L1 (existing)
  mcp:agent_cache:{tenant}:{agent}:{sha256(norm)[:16]} — per-agent partial cache

Freshness TTL by query type (overrides the fixed 24h in rag/store.py when
intent is known):
  TEMPORAL / COMPARATIVE  12h  — time-bound answers expire faster
  DIAGNOSTIC / TRENDING   18h
  DIMENSIONAL / RANKING   24h
  SUMMARY / GENERAL       24h
"""
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, TYPE_CHECKING

from query_intelligence import QueryIntent, QueryType

if TYPE_CHECKING:
    from redis_memory import RedisMemory
    from rag.store import RAGStore

# ── Freshness TTL table ────────────────────────────────────────────────────────

_TTL_SECONDS: dict[QueryType, int] = {
    QueryType.TEMPORAL:    12 * 3600,
    QueryType.COMPARATIVE: 12 * 3600,
    QueryType.DIAGNOSTIC:  18 * 3600,
    QueryType.TRENDING:    18 * 3600,
    QueryType.DIMENSIONAL: 24 * 3600,
    QueryType.RANKING:     24 * 3600,
    QueryType.SUMMARY:     24 * 3600,
    QueryType.GENERAL:     24 * 3600,
}

_TEMPORAL_RE = re.compile(
    r'\b(20\d{2})\b'
    r'|\bQ[1-4]\b'
    r'|\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?'
    r'|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\b',
    re.IGNORECASE,
)

AGENT_CACHE_TTL = 18 * 3600   # per-agent results cached for 18h


# ── Public types ───────────────────────────────────────────────────────────────

class CacheLayer(str, Enum):
    REDIS_L1  = "redis_l1"
    CHROMA_L2 = "chroma_l2"
    MISS      = "miss"


@dataclass
class CacheLookupResult:
    hit:              bool
    layer:            CacheLayer      = CacheLayer.MISS
    answer:           Optional[str]   = None
    similarity:       Optional[float] = None   # cosine distance on L2 hit (lower = closer)
    agent_results:    dict[str, str]  = field(default_factory=dict)   # partial reuse
    cache_threshold_used: float       = 0.10
    rag_threshold_used:   float       = 0.50


# ── Freshness Policy ──────────────────────────────────────────────────────────

class FreshnessPolicy:
    """
    Determines whether a cached entry is still valid for the incoming question.

    Two checks:
      1. TTL:  age of the entry vs the query-type-specific TTL.
      2. Temporal mismatch: if the cached question and the incoming question
         both contain time markers but they differ, the entry is stale
         (e.g., a cached answer for Q1 must not satisfy a Q2 question).
    """

    def effective_ttl(self, query_type: QueryType) -> int:
        return _TTL_SECONDS.get(query_type, 24 * 3600)

    def is_fresh(
        self,
        cached_at: float,
        cached_question: str,
        incoming_question: str,
        query_type: QueryType,
    ) -> bool:
        # TTL check
        if time.time() - cached_at > self.effective_ttl(query_type):
            return False

        # Temporal mismatch: if both have time markers but they differ → stale
        cached_t   = {m.group().upper()[:7] for m in _TEMPORAL_RE.finditer(cached_question)}
        incoming_t = {m.group().upper()[:7] for m in _TEMPORAL_RE.finditer(incoming_question)}
        if cached_t and incoming_t and cached_t != incoming_t:
            return False

        return True


# ── CacheRegistry ─────────────────────────────────────────────────────────────

class CacheRegistry:
    """
    Unified cache lookup, write-through, and invalidation across all layers.

    Parameters
    ----------
    redis_mem  : RedisMemory instance (for L1 + agent cache)
    freshness  : FreshnessPolicy instance (default constructed if None)
    """

    def __init__(
        self,
        redis_mem: "RedisMemory",
        freshness: Optional[FreshnessPolicy] = None,
    ):
        self._redis    = redis_mem
        self._freshness = freshness or FreshnessPolicy()

    # ── Lookup ────────────────────────────────────────────────────────────────

    async def lookup(
        self,
        tenant_id: str,
        question: str,
        intent: QueryIntent,
        rag_store: "RAGStore",
    ) -> CacheLookupResult:
        """
        Check all cache layers in order and return the first hit.

        Order: Redis L1 (exact) → ChromaDB L2 (semantic) → partial agent results.

        On a full miss, still returns partial agent results if any are available,
        so _run_pipeline can skip already-cached agents.
        """
        from query_intelligence import SimilarityRules
        rules = SimilarityRules()
        ct, rt = rules.get_thresholds(intent)

        # ── Layer 1: Redis L1 exact match ────────────────────────────────────
        if self._redis and self._redis.available:
            exact = await self._redis.get_exact(tenant_id, question)
            if exact:
                return CacheLookupResult(
                    hit=True, layer=CacheLayer.REDIS_L1, answer=exact,
                    cache_threshold_used=ct, rag_threshold_used=rt,
                )

        # ── Layer 2: ChromaDB L2 semantic match ──────────────────────────────
        # Pass intent-aware thresholds so temporal queries use stricter matching.
        cached_answer, rag_context = rag_store.retrieve(
            question, cache_threshold=ct, rag_threshold=rt
        )
        if cached_answer:
            return CacheLookupResult(
                hit=True, layer=CacheLayer.CHROMA_L2, answer=cached_answer,
                cache_threshold_used=ct, rag_threshold_used=rt,
            )

        # ── Partial agent result reuse ────────────────────────────────────────
        agent_results = await self._get_partial_agent_results(
            tenant_id, intent.normalized
        )

        return CacheLookupResult(
            hit=False, layer=CacheLayer.MISS,
            agent_results=agent_results,
            cache_threshold_used=ct, rag_threshold_used=rt,
        )

    async def get_rag_context(
        self,
        question: str,
        intent: QueryIntent,
        rag_store: "RAGStore",
    ) -> str:
        """
        Return only the RAG context string (not the cached answer) using
        intent-aware thresholds. Used after a full cache miss to get context.
        """
        from query_intelligence import SimilarityRules
        ct, rt = SimilarityRules().get_thresholds(intent)
        _, rag_context = rag_store.retrieve(question, cache_threshold=ct, rag_threshold=rt)
        return rag_context

    # ── Partial agent result cache ────────────────────────────────────────────

    @staticmethod
    def _agent_key(tenant_id: str, agent_name: str, normalized_question: str) -> str:
        h = hashlib.sha256(normalized_question.encode()).hexdigest()[:16]
        return f"mcp:agent_cache:{tenant_id}:{agent_name}:{h}"

    async def store_agent_result(
        self,
        tenant_id: str,
        agent_name: str,
        normalized_question: str,
        result: str,
    ) -> None:
        """Cache an individual agent result for partial reuse on similar future queries."""
        if not (self._redis and self._redis.available):
            return
        try:
            key     = self._agent_key(tenant_id, agent_name, normalized_question)
            payload = json.dumps({"result": result, "cached_at": time.time()})
            await self._redis.client.set(key, payload, ex=AGENT_CACHE_TTL)
        except Exception:
            pass

    async def _get_partial_agent_results(
        self, tenant_id: str, normalized_question: str
    ) -> dict[str, str]:
        """Return {agent_name: result} for all agents that have partial cache hits."""
        if not (self._redis and self._redis.available):
            return {}
        results: dict[str, str] = {}
        for agent in ("semantic", "benchmark", "insight"):
            try:
                key = self._agent_key(tenant_id, agent, normalized_question)
                raw = await self._redis.client.get(key)
                if raw:
                    data = json.loads(raw)
                    if time.time() - data.get("cached_at", 0) < AGENT_CACHE_TTL:
                        results[agent] = data["result"]
            except Exception:
                pass
        return results

    # ── Write-through ─────────────────────────────────────────────────────────

    async def register(
        self,
        tenant_id: str,
        question: str,
        normalized_question: str,
        answer: str,
        agents_used: list[str],
        agent_results: dict[str, str],
    ) -> None:
        """Write answer to Redis L1. Partial agent results are stored separately."""
        if self._redis:
            await self._redis.set_exact(tenant_id, question, answer, agents_used)
        # Store individual agent results for partial reuse on future similar queries
        for agent, result in agent_results.items():
            await self.store_agent_result(tenant_id, agent, normalized_question, result)

    # ── Invalidation ──────────────────────────────────────────────────────────

    async def invalidate(
        self,
        tenant_id: str,
        question: str,
        normalized_question: str,
        rag_store: "RAGStore",
        reason: str = "manual",
    ) -> dict:
        """
        Invalidate a question across all cache layers atomically.

        Returns a summary of what was removed and why.
        """
        removed: list[str] = []

        # Redis L1
        if self._redis and self._redis.available:
            await self._redis.invalidate_exact(tenant_id, question)
            removed.append("redis_l1")

        # ChromaDB L2 (rag_store.flag_bad is synchronous)
        try:
            rag_store.flag_bad(question)
            removed.append("chroma_l2")
        except Exception:
            pass

        # Per-agent result cache
        if self._redis and self._redis.available:
            for agent in ("semantic", "benchmark", "insight"):
                try:
                    key = self._agent_key(tenant_id, agent, normalized_question)
                    await self._redis.client.delete(key)
                except Exception:
                    pass
            removed.append("agent_cache")

        return {"invalidated_from": removed, "reason": reason, "question": question}

    # ── Status ────────────────────────────────────────────────────────────────

    async def status(self, tenant_id: str, rag_store: "RAGStore") -> dict:
        """Aggregate cache status across all layers for a tenant."""
        redis_health = {}
        if self._redis:
            redis_health = await self._redis.health()

        rag_stats = rag_store.stats()

        agent_counts: dict[str, int] = {}
        if self._redis and self._redis.available:
            for agent in ("semantic", "benchmark", "insight"):
                try:
                    pattern = f"mcp:agent_cache:{tenant_id}:{agent}:*"
                    keys = await self._redis.client.keys(pattern)
                    agent_counts[agent] = len(keys)
                except Exception:
                    agent_counts[agent] = -1

        return {
            "tenant_id":    tenant_id,
            "redis_l1":     redis_health,
            "chroma_l2": {
                "qa_entries":  rag_stats.get("qa_entries", 0),
                "domain_docs": rag_stats.get("domain_docs", 0),
            },
            "agent_cache":  agent_counts,
        }

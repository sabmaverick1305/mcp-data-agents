"""
Redis-backed memory layer for MCP Data Agents.

Three responsibilities:
  L1 exact cache   — keyed by SHA-256(question), TTL 24 h.
                     Sits in front of ChromaDB; identical queries never hit the vector index.
  Session history  — per-tenant Redis List, capped at 100 turns, TTL 7 days.
                     Replaces the JSON file written by RAGStore.save_history().
  Rate limiting    — sliding 60-second window counter per tenant (default 60 req/min).
  Audit log        — Sorted Set (score = Unix ts) so you can query "what was cached when".

All methods are async and fail-open: if Redis is unavailable every call is a no-op /
returns a safe default, so the rest of the pipeline continues uninterrupted.

Usage:
  redis_mem = RedisMemory()
  ok = await redis_mem.connect()      # False → Redis down, rest still works
  ...
  await redis_mem.close()

Environment:
  REDIS_URL          redis://localhost:6379  (default)
  REDIS_RATE_LIMIT   60                     requests/tenant/minute
"""
import hashlib
import json
import os
import time
from typing import Optional

import redis.asyncio as aioredis

# ── Config ────────────────────────────────────────────────────────────────────

REDIS_URL       = os.environ.get("REDIS_URL", "redis://localhost:6379")
EXACT_CACHE_TTL = 24 * 3600        # 24 hours
HISTORY_TTL     = 7 * 24 * 3600   # 7 days
HISTORY_MAX_LEN = 100
RATE_LIMIT_RPM  = int(os.environ.get("REDIS_RATE_LIMIT", 60))
AUDIT_TTL       = 30 * 24 * 3600  # 30 days


# ── Key helpers ───────────────────────────────────────────────────────────────

def _exact_key(tenant_id: str, question: str) -> str:
    h = hashlib.sha256(question.strip().lower().encode()).hexdigest()[:20]
    return f"mcp:cache:exact:{tenant_id}:{h}"

def _history_key(tenant_id: str) -> str:
    return f"mcp:history:{tenant_id}"

def _rate_key(tenant_id: str) -> str:
    bucket = int(time.time() // 60)   # one bucket per minute
    return f"mcp:rate:{tenant_id}:{bucket}"

def _audit_key(tenant_id: str) -> str:
    return f"mcp:audit:{tenant_id}"


# ── RedisMemory ───────────────────────────────────────────────────────────────

class RedisMemory:
    def __init__(self, url: str = REDIS_URL):
        self._url    = url
        self._client: aioredis.Redis | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        """
        Open the connection pool and verify reachability.
        Returns True on success, False if Redis is unavailable.
        The class stays usable either way — all methods check self.available.
        """
        try:
            self._client = aioredis.from_url(
                self._url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            await self._client.ping()
            return True
        except Exception as exc:
            print(f"[RedisMemory] Unavailable ({exc}) — falling back to local storage.")
            self._client = None
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    # ── L1 exact cache ────────────────────────────────────────────────────────

    async def get_exact(self, tenant_id: str, question: str) -> Optional[str]:
        """Return the cached answer for an identical question, or None on miss."""
        if not self.available:
            return None
        try:
            raw = await self._client.get(_exact_key(tenant_id, question))
            if raw:
                return json.loads(raw).get("answer")
        except Exception:
            pass
        return None

    async def set_exact(
        self,
        tenant_id: str,
        question: str,
        answer: str,
        agents_used: list[str],
    ) -> None:
        """Store question → answer in the L1 cache."""
        if not self.available:
            return
        try:
            payload = json.dumps({
                "answer":      answer,
                "agents_used": agents_used,
                "cached_at":   time.time(),
                "question":    question,
            })
            await self._client.set(_exact_key(tenant_id, question), payload, ex=EXACT_CACHE_TTL)
            await _zadd_audit(self._client, tenant_id, "stored", question)
        except Exception:
            pass

    async def invalidate_exact(self, tenant_id: str, question: str) -> None:
        """Remove an entry from the L1 cache (bad-feedback path)."""
        if not self.available:
            return
        try:
            await self._client.delete(_exact_key(tenant_id, question))
            await _zadd_audit(self._client, tenant_id, "invalidated", question)
        except Exception:
            pass

    # ── Session history ───────────────────────────────────────────────────────

    async def get_history(self, tenant_id: str) -> list[dict]:
        """Return up to HISTORY_MAX_LEN conversation turns, newest first."""
        if not self.available:
            return []
        try:
            raw = await self._client.lrange(_history_key(tenant_id), 0, HISTORY_MAX_LEN - 1)
            return [json.loads(r) for r in raw]
        except Exception:
            return []

    async def append_history(self, tenant_id: str, question: str, answer: str) -> None:
        """Prepend a Q&A turn and trim to the cap."""
        if not self.available:
            return
        try:
            key   = _history_key(tenant_id)
            entry = json.dumps({"question": question, "answer": answer, "ts": time.time()})
            pipe  = self._client.pipeline()
            pipe.lpush(key, entry)
            pipe.ltrim(key, 0, HISTORY_MAX_LEN - 1)
            pipe.expire(key, HISTORY_TTL)
            await pipe.execute()
        except Exception:
            pass

    # ── Rate limiting ─────────────────────────────────────────────────────────

    async def check_rate_limit(self, tenant_id: str) -> bool:
        """
        Increment the per-tenant per-minute counter.
        Returns True if the request is allowed, False if the limit is exceeded.
        Fails open — returns True when Redis is unavailable.
        """
        if not self.available:
            return True
        try:
            key   = _rate_key(tenant_id)
            pipe  = self._client.pipeline()
            pipe.incr(key)
            pipe.expire(key, 60)
            results = await pipe.execute()
            count = results[0]
            return count <= RATE_LIMIT_RPM
        except Exception:
            return True

    async def get_rate_usage(self, tenant_id: str) -> int:
        """Current requests in the active 60-second window."""
        if not self.available:
            return 0
        try:
            val = await self._client.get(_rate_key(tenant_id))
            return int(val) if val else 0
        except Exception:
            return 0

    # ── Audit log ─────────────────────────────────────────────────────────────

    async def get_audit_log(self, tenant_id: str, limit: int = 50) -> list[dict]:
        """Return the most recent audit events (newest first)."""
        if not self.available:
            return []
        try:
            raw = await self._client.zrevrange(_audit_key(tenant_id), 0, limit - 1)
            return [json.loads(r) for r in raw]
        except Exception:
            return []

    # ── Health ────────────────────────────────────────────────────────────────

    async def health(self) -> dict:
        if not self.available:
            return {"status": "unavailable", "url": self._url}
        try:
            info = await self._client.info("server")
            mem  = await self._client.info("memory")
            return {
                "status":       "ok",
                "url":          self._url,
                "redis_version": info.get("redis_version"),
                "used_memory":  mem.get("used_memory_human"),
                "rate_limit_rpm": RATE_LIMIT_RPM,
            }
        except Exception as exc:
            return {"status": "error", "error": str(exc)}


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _zadd_audit(client: aioredis.Redis, tenant_id: str, event: str, question: str) -> None:
    try:
        key   = _audit_key(tenant_id)
        now   = time.time()
        entry = json.dumps({"event": event, "question": question[:200], "ts": now})
        pipe  = client.pipeline()
        pipe.zadd(key, {entry: now})
        # prune entries older than AUDIT_TTL
        pipe.zremrangebyscore(key, 0, now - AUDIT_TTL)
        pipe.expire(key, AUDIT_TTL)
        await pipe.execute()
    except Exception:
        pass

"""
Workflow state management, checkpointing, and execution replay for MCP Data Agents.

STATE & MEMORY LAYER — Durable, Resilient, Recoverable.

This module implements four architecture components:

  WorkflowStateStore  — Tracks per-query pipeline progress through state transitions.
  Checkpoint Manager  — Saves per-agent results durably; replay skips completed agents.
  Agent Session Store — Per-tenant session index (Sorted Set) with metadata.
  Execution Replay    — Re-runs a failed session from the last good checkpoint.

State machine:
  PENDING → PLANNING → AGENTS_RUNNING → SYNTHESIZING → COMPLETE
                                      ↘
                                        FAILED

All state is persisted in Redis using the same connection as RedisMemory.
Every method is async and fails-open: if Redis is unavailable the pipeline
continues without state tracking. No exceptions propagate to callers.

Redis key scheme:
  mcp:workflow:{tenant_id}:{session_id}             — JSON session record
  mcp:checkpoint:{tenant_id}:{session_id}:{agent}  — JSON per-agent result
  mcp:sessions:{tenant_id}                          — Sorted Set, score = start timestamp

Usage:
  wf = WorkflowStateStore(redis_client)   # redis_client from RedisMemory.client
  session_id = uuid.uuid4().hex[:12]

  await wf.begin(session_id, tenant_id, question)
  await wf.set_state(session_id, tenant_id, PipelineState.PLANNING)
  await wf.checkpoint_agent(session_id, tenant_id, "semantic", result, in_tok, out_tok)
  await wf.close(session_id, tenant_id, answer, agents_invoked)
  # or on failure:
  await wf.mark_failed(session_id, tenant_id, "synthesizing", str(exc))

  # Replay a past session:
  checkpoints = await wf.get_checkpointed_results(old_session_id, tenant_id)
  # checkpoints = {"semantic": "...", "benchmark": "..."}  (only completed ones)

Environment:
  Inherits REDIS_URL from RedisMemory — no additional env vars.
"""
import json
import time
from enum import Enum
from typing import Optional

import redis.asyncio as aioredis

WORKFLOW_TTL   = 7  * 24 * 3600   # 7 days  — active session state
CHECKPOINT_TTL = 30 * 24 * 3600   # 30 days — agent results (kept longer for replay)
SESSION_TTL    = 30 * 24 * 3600   # 30 days — session index


class PipelineState(str, Enum):
    PENDING        = "pending"
    PLANNING       = "planning"
    AGENTS_RUNNING = "agents_running"
    SYNTHESIZING   = "synthesizing"
    COMPLETE       = "complete"
    FAILED         = "failed"


class WorkflowStateStore:
    """
    Redis-backed workflow state, checkpointing, and session index.

    Constructed with a live redis.asyncio.Redis client (shared from RedisMemory).
    If client is None (Redis unavailable) all methods are no-ops.
    """

    def __init__(self, client: Optional[aioredis.Redis] = None):
        self._r = client

    @property
    def available(self) -> bool:
        return self._r is not None

    # ── Key helpers ───────────────────────────────────────────────────────────

    def _wf_key(self, tenant_id: str, session_id: str) -> str:
        return f"mcp:workflow:{tenant_id}:{session_id}"

    def _cp_key(self, tenant_id: str, session_id: str, agent: str) -> str:
        return f"mcp:checkpoint:{tenant_id}:{session_id}:{agent}"

    def _sessions_key(self, tenant_id: str) -> str:
        return f"mcp:sessions:{tenant_id}"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def begin(
        self,
        session_id: str,
        tenant_id: str,
        question: str,
    ) -> None:
        """Create a new workflow record and register it in the session index."""
        if not self.available:
            return
        try:
            now = time.time()
            data = {
                "session_id":       session_id,
                "tenant_id":        tenant_id,
                "question":         question[:500],
                "state":            PipelineState.PENDING.value,
                "started_at":       now,
                "updated_at":       now,
                "completed_agents": [],
                "failure_point":    None,
                "error":            None,
            }
            await self._r.set(self._wf_key(tenant_id, session_id), json.dumps(data),
                              ex=WORKFLOW_TTL)
            idx = self._sessions_key(tenant_id)
            await self._r.zadd(idx, {session_id: now})
            await self._r.expire(idx, SESSION_TTL)
        except Exception:
            pass

    async def set_state(
        self,
        session_id: str,
        tenant_id: str,
        state: PipelineState,
    ) -> None:
        """Advance the pipeline state (e.g. PLANNING → AGENTS_RUNNING)."""
        if not self.available:
            return
        try:
            key = self._wf_key(tenant_id, session_id)
            raw = await self._r.get(key)
            if not raw:
                return
            data = json.loads(raw)
            data["state"]      = state.value
            data["updated_at"] = time.time()
            await self._r.set(key, json.dumps(data), ex=WORKFLOW_TTL)
        except Exception:
            pass

    async def checkpoint_agent(
        self,
        session_id: str,
        tenant_id: str,
        agent_name: str,
        result: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        """
        Persist a completed agent's result as a checkpoint.

        Checkpoints survive WORKFLOW_TTL and are retrieved by replay to skip
        re-running agents that already succeeded in a failed session.
        """
        if not self.available:
            return
        try:
            cp_key = self._cp_key(tenant_id, session_id, agent_name)
            checkpoint = {
                "agent":         agent_name,
                "result":        result,
                "input_tokens":  input_tokens,
                "output_tokens": output_tokens,
                "completed_at":  time.time(),
            }
            await self._r.set(cp_key, json.dumps(checkpoint), ex=CHECKPOINT_TTL)

            # Update completed_agents list in the workflow record
            wf_key = self._wf_key(tenant_id, session_id)
            raw = await self._r.get(wf_key)
            if raw:
                data = json.loads(raw)
                agents = data.get("completed_agents", [])
                if agent_name not in agents:
                    agents.append(agent_name)
                data["completed_agents"] = agents
                data["updated_at"] = time.time()
                await self._r.set(wf_key, json.dumps(data), ex=WORKFLOW_TTL)
        except Exception:
            pass

    async def mark_failed(
        self,
        session_id: str,
        tenant_id: str,
        failure_point: str,
        error: str,
    ) -> None:
        """Record where in the pipeline the session failed and why."""
        if not self.available:
            return
        try:
            key = self._wf_key(tenant_id, session_id)
            raw = await self._r.get(key)
            if not raw:
                return
            data = json.loads(raw)
            data["state"]         = PipelineState.FAILED.value
            data["failure_point"] = failure_point
            data["error"]         = error[:500]
            data["updated_at"]    = time.time()
            await self._r.set(key, json.dumps(data), ex=WORKFLOW_TTL)
        except Exception:
            pass

    async def close(
        self,
        session_id: str,
        tenant_id: str,
        answer: str,
        agents_invoked: list[str],
    ) -> None:
        """Mark session COMPLETE and store the answer preview."""
        if not self.available:
            return
        try:
            key = self._wf_key(tenant_id, session_id)
            raw = await self._r.get(key)
            if not raw:
                return
            data = json.loads(raw)
            data["state"]          = PipelineState.COMPLETE.value
            data["updated_at"]     = time.time()
            data["completed_at"]   = time.time()
            data["agents_invoked"] = agents_invoked
            data["answer_preview"] = answer[:300]
            await self._r.set(key, json.dumps(data), ex=WORKFLOW_TTL)
        except Exception:
            pass

    # ── Read ──────────────────────────────────────────────────────────────────

    async def get(
        self, session_id: str, tenant_id: str
    ) -> Optional[dict]:
        """Return the full workflow record for a session, or None."""
        if not self.available:
            return None
        try:
            raw = await self._r.get(self._wf_key(tenant_id, session_id))
            return json.loads(raw) if raw else None
        except Exception:
            return None

    async def get_checkpointed_results(
        self, session_id: str, tenant_id: str
    ) -> dict[str, str]:
        """
        Return {agent_name: result_text} for all agents that completed checkpoints.

        Used by the replay path: agents present in this dict are skipped;
        only missing agents (or synthesis) are re-run.
        """
        if not self.available:
            return {}
        results: dict[str, str] = {}
        for agent in ("semantic", "benchmark", "insight"):
            try:
                raw = await self._r.get(self._cp_key(tenant_id, session_id, agent))
                if raw:
                    results[agent] = json.loads(raw)["result"]
            except Exception:
                pass
        return results

    async def list_sessions(
        self, tenant_id: str, limit: int = 20
    ) -> list[dict]:
        """Return the most recent sessions for a tenant, newest first."""
        if not self.available:
            return []
        try:
            idx_key = self._sessions_key(tenant_id)
            session_ids = await self._r.zrevrange(idx_key, 0, limit - 1)
            sessions: list[dict] = []
            for sid in session_ids:
                raw = await self._r.get(self._wf_key(tenant_id, sid))
                if raw:
                    sessions.append(json.loads(raw))
            return sessions
        except Exception:
            return []

    async def prune_old_sessions(
        self, tenant_id: str, keep: int = 500
    ) -> int:
        """Remove sessions beyond the most recent `keep` from the index. Returns count removed."""
        if not self.available:
            return 0
        try:
            idx_key = self._sessions_key(tenant_id)
            total = await self._r.zcard(idx_key)
            if total <= keep:
                return 0
            # Remove oldest (lowest score = earliest start time)
            to_remove = total - keep
            await self._r.zpopmin(idx_key, to_remove)
            return to_remove
        except Exception:
            return 0

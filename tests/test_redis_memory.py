"""Tests for RedisMemory — mocked redis client, all public methods."""
import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from redis_memory import RedisMemory, EXACT_CACHE_TTL, RATE_LIMIT_RPM


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_redis():
    """Return a mock async Redis client with commonly-used methods."""
    r = AsyncMock()
    r.ping = AsyncMock(return_value=True)
    r.get  = AsyncMock(return_value=None)
    r.set  = AsyncMock(return_value=True)
    r.delete = AsyncMock(return_value=1)
    r.lrange = AsyncMock(return_value=[])
    r.pipeline = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=AsyncMock(
            lpush=AsyncMock(), ltrim=AsyncMock(), expire=AsyncMock(),
            incr=AsyncMock(return_value=1), zadd=AsyncMock(),
            zremrangebyscore=AsyncMock(), execute=AsyncMock(return_value=[1, True, True])
        )),
        __aexit__=AsyncMock(return_value=False),
        lpush=AsyncMock(), ltrim=AsyncMock(), expire=AsyncMock(),
        incr=AsyncMock(), zadd=AsyncMock(),
        zremrangebyscore=AsyncMock(),
        execute=AsyncMock(return_value=[1, True, True]),
    ))
    r.aclose = AsyncMock()
    return r


@pytest.fixture
async def mem(mock_redis):
    m = RedisMemory(url="redis://localhost:6379")
    with patch("redis.asyncio.from_url", return_value=mock_redis):
        await m.connect()
    return m, mock_redis


# ── connect / available ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_connect_success(mock_redis):
    m = RedisMemory()
    with patch("redis.asyncio.from_url", return_value=mock_redis):
        ok = await m.connect()
    assert ok is True
    assert m.available is True


@pytest.mark.asyncio
async def test_connect_failure_fails_open():
    m = RedisMemory()
    with patch("redis.asyncio.from_url", side_effect=ConnectionRefusedError):
        ok = await m.connect()
    assert ok is False
    assert m.available is False


# ── L1 exact cache ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_exact_miss_returns_none(mem):
    m, r = mem
    r.get.return_value = None
    result = await m.get_exact("acme", "what is revenue?")
    assert result is None


@pytest.mark.asyncio
async def test_get_exact_hit_returns_answer(mem):
    m, r = mem
    payload = json.dumps({"answer": "Revenue was $5M", "agents_used": ["semantic"]})
    r.get.return_value = payload
    result = await m.get_exact("acme", "what is revenue?")
    assert result == "Revenue was $5M"


@pytest.mark.asyncio
async def test_set_exact_calls_redis_set(mem):
    m, r = mem
    await m.set_exact("acme", "what is revenue?", "Revenue was $5M", ["semantic"])
    r.set.assert_awaited_once()
    key, payload = r.set.call_args[0]
    assert "acme" in key
    data = json.loads(payload)
    assert data["answer"] == "Revenue was $5M"
    assert data["agents_used"] == ["semantic"]


@pytest.mark.asyncio
async def test_invalidate_exact_calls_delete(mem):
    m, r = mem
    await m.invalidate_exact("acme", "what is revenue?")
    r.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_unavailable_get_returns_none():
    m = RedisMemory()   # never connected
    result = await m.get_exact("acme", "anything")
    assert result is None


# ── Session history ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_history_empty(mem):
    m, r = mem
    r.lrange.return_value = []
    history = await m.get_history("acme")
    assert history == []


@pytest.mark.asyncio
async def test_get_history_returns_parsed_entries(mem):
    m, r = mem
    entries = [
        json.dumps({"question": "q1", "answer": "a1", "ts": 1000.0}),
        json.dumps({"question": "q2", "answer": "a2", "ts": 1001.0}),
    ]
    r.lrange.return_value = entries
    history = await m.get_history("acme")
    assert len(history) == 2
    assert history[0]["question"] == "q1"


# ── Rate limiting ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rate_limit_allows_first_request(mem):
    m, r = mem
    pipe = MagicMock()
    pipe.incr  = AsyncMock()
    pipe.expire = AsyncMock()
    pipe.execute = AsyncMock(return_value=[1, True])
    r.pipeline.return_value = pipe
    allowed = await m.check_rate_limit("acme")
    assert allowed is True


@pytest.mark.asyncio
async def test_rate_limit_blocks_over_limit(mem):
    m, r = mem
    pipe = MagicMock()
    pipe.incr   = AsyncMock()
    pipe.expire = AsyncMock()
    pipe.execute = AsyncMock(return_value=[RATE_LIMIT_RPM + 1, True])
    r.pipeline.return_value = pipe
    allowed = await m.check_rate_limit("acme")
    assert allowed is False


@pytest.mark.asyncio
async def test_rate_limit_fails_open_when_unavailable():
    m = RedisMemory()   # never connected
    allowed = await m.check_rate_limit("acme")
    assert allowed is True

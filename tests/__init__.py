"""
Test suite for the MCP Data Agents system — 36 pytest tests across 7 modules.

test_auth.py            API key mode (valid key, missing key, wrong key, default tenant),
                        AUTH_MODE=none passthrough, and tenant allowlist enforcement.

test_security.py        Prompt injection detection (check_query), RAG document injection
                        (check_ingest — the primary indirect injection use case), RAG context
                        wrapping (wrap_rag_context), tool call allowlisting, and PII scanning.

test_redis_memory.py    RedisMemory with a mocked async Redis client: connect/available,
                        L1 exact cache get/set/invalidate, session history, rate limiting
                        (allow / block / fail-open when Redis is down).

test_rag.py             RAGStore with an in-process ChromaDB (tmp_path): domain seeding,
                        cache miss, cache hit (exact and near-match), TTL expiry, flag_bad
                        invalidation, and stats reporting.

test_ingest.py          Ingest pipeline: chunk count, source listing, relevance retrieval,
                        long-text multi-chunk splitting, unsupported extension error.

test_servers.py         Live MCP server integration tests via stdio_client: Snowflake
                        (list_tables, SELECT query, DROP block, non-SELECT block, describe),
                        Power BI (list_models, get_metric), Tableau (list_dashboards, trend).

test_bedrock_client.py  Client factory: Anthropic direct mode (type check, model ID, label)
                        and AWS Bedrock mode (type check, model ID format, custom BEDROCK_MODEL_ID).

Run all tests:
    pytest
Run with coverage:
    pytest --cov=. --cov-report=term-missing
"""

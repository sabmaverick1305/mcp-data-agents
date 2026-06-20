# MCP Data Agents — Claude Code Instructions

## Problem Statement

This system solves a specific analytics challenge: business users ask natural-language questions
about revenue, margins, regional performance, and growth trends across a B2B SaaS company.
The data lives across three tools (Power BI, Tableau, Snowflake), so a single query must
orchestrate multiple specialist agents in parallel and synthesise their outputs into one answer.

**Core scenario under investigation:** Q1 2024 revenue dropped to ~$1.55M from ~$5.9M in prior
quarters. The root cause is sparse transaction volume (1–4 txns/day vs 5–14 normal), not price
changes. Agents must cross-reference KPIs (Power BI), regional benchmarks (Tableau), and raw
SQL (Snowflake) to surface this.

---

## Architecture

```
User question
    │
    ▼
Redis L1 exact cache  (SHA-256 key, 24h TTL)  ──hit──► return cached answer
    │ miss
    ▼
ChromaDB L2 semantic cache  (cosine < 0.10, 24h TTL)  ──hit──► return cached answer
    │ miss
    ▼
Planner Agent  (claude-sonnet-4-6, 512 tokens)
    │  emits JSON: { agents, tasks, reasoning, synthesis_goal }
    │
    ├──► Semantic Agent  (Power BI MCP server)   ──┐
    ├──► Benchmark Agent (Tableau MCP server)    ──┤  parallel
    │                                              │
    ▼                                              │
Insight Agent   (Snowflake MCP server, sequential)◄┘
    │  receives parallel results as context
    ▼
Synthesis (claude-sonnet-4-6, 2048 tokens)
    │
    ▼
Store in Redis L1 + ChromaDB L2 + Cost Ledger
```

**Token efficiency:** Static system prompts (planner, agents, synthesis) use Anthropic prompt
caching (`cache_control: ephemeral`). Identical system text is never re-encoded on consecutive
calls within the 5-minute cache window.

---

## Key Files

| File | Role |
|---|---|
| `api.py` | FastAPI app: all endpoints, `_run_pipeline()`, startup/shutdown |
| `agents/planner.py` | Routes questions → agents via JSON plan; contains `PROBLEM_CONTEXT` |
| `agents/base_agent.py` | Shared agent loop: retry, tool dispatch, tracing, prompt caching |
| `agents/semantic_agent.py` | Power BI KPI retrieval |
| `agents/benchmark_agent.py` | Tableau benchmark queries |
| `agents/insight_agent.py` | Snowflake ad-hoc SQL |
| `rag/store.py` | ChromaDB L2 semantic cache + domain knowledge; `CACHE_THRESHOLD=0.10` |
| `redis_memory.py` | Redis L1 exact cache + session history + rate limiting |
| `orchestrator.py` | Manages MCP server stdio processes; tool name: `server__tool` |
| `servers/` | Simulated MCP servers (replace with real Power BI / Tableau / Snowflake) |
| `data/seed.py` | Star-schema SQLite seeder (sales_fact, region_dim, product_dim, …) |
| `cost_ledger.py` | SQLite per-tenant/team/agent cost attribution |
| `observability.py` | `QueryTrace` — token counts, latency, cache savings, agent attribution |
| `security.py` | Prompt injection guard, PII scanner, tool call allowlist |
| `auth.py` | API key auth + JWT/JWKS path for Azure AD (Power BI production) |
| `tenant.py` | `TenantContext` — namespace isolation across all caches and ledgers |
| `eval/` | LLM-as-judge evaluation suite (36 test cases, routing + quality scoring) |

---

## Cache Behaviour — Do Not Change Thresholds Without Testing

| Layer | Key | Hit condition | TTL |
|---|---|---|---|
| Redis L1 | SHA-256(question) | Exact match | 24h |
| ChromaDB L2 | Cosine embedding | distance < 0.10 | 24h |
| ChromaDB RAG | Cosine embedding | distance < 0.50 | 7d |

Lowering `CACHE_THRESHOLD` below 0.10 risks returning wrong answers for similar-but-different
questions (e.g. "Q1 2024" vs "Q2 2024"). The temporal token guard in `rag/store.py` handles
most of these, but the threshold is the last line of defence.

---

## Development Conventions

- **All agent system prompts are static strings** — keep them stable. Dynamic content
  (RAG context, history) is appended as a separate uncached block.
- **Fail-open everywhere** — Redis down, ChromaDB error, MCP timeout all return safe defaults;
  the pipeline continues. Never raise an exception that kills a query.
- **Tenant isolation** — every cache collection, ledger record, and history list is namespaced
  by `tenant_id`. Never use a shared key.
- **No side effects in tests** — tests use `tmp_path` / `monkeypatch` to isolate ChromaDB and
  env vars. Production `data/chroma/` and `data/warehouse.db` are never touched by the test suite.
- **Cost attribution first** — every `client.messages.create()` call must go through a
  `QueryTrace` and call `trace.record_usage(response)`. Missing attribution breaks the ledger.

---

## Environment Variables

```
ANTHROPIC_API_KEY      # Required for direct Anthropic calls
AUTH_MODE              # api_key (default) | none | jwt
API_KEYS               # Comma-separated valid keys when AUTH_MODE=api_key
TENANT_ID              # Default tenant namespace
ALLOWED_TENANTS        # Comma-separated allowlist (empty = allow all)
REDIS_URL              # redis://localhost:6379 (default)
REDIS_RATE_LIMIT       # Requests/tenant/minute (default 60)
CHROMA_HOST            # Empty = local PersistentClient; set for remote ChromaDB
USE_BEDROCK            # true = use AWS Bedrock instead of direct Anthropic
BEDROCK_REGION         # AWS region for Bedrock (default us-east-1)
BEDROCK_MODEL_ID       # Override Bedrock model ID
SEED_MODE              # demo (default) | real
```

---

## Running Locally

```bash
# Seed the warehouse
python data/seed.py

# Start the API
uvicorn api:app --reload --port 8000

# Or the interactive Streamlit dashboard
streamlit run dashboard.py

# Run tests
pytest tests/ -v

# Run eval suite
python -m eval.runner
```

---

## Prompt Caching Pattern

All static system prompts use Anthropic's prompt caching to avoid re-encoding on every call:

```python
# DO: pass system as a list with cache_control on the static block
system = [
    {"type": "text", "text": STATIC_INSTRUCTIONS, "cache_control": {"type": "ephemeral"}},
    {"type": "text", "text": dynamic_rag_context},   # no cache_control — changes per request
]

# DON'T: concatenate into a single string (forces full re-encoding every call)
system = STATIC_INSTRUCTIONS + "\n\n" + dynamic_rag_context
```

The cached prefix covers everything up to and including the `cache_control` block. Dynamic
content (RAG results, conversation history) must always follow the cached block, never precede it.

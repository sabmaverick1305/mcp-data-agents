 █████╗  ██████╗ ███████╗███╗   ██╗████████╗███████╗
██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝██╔════╝
███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║   ███████╗
██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║   ╚════██║
██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║   ███████║
╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝   ╚══════╝
              Multi-agent analytics · Powered by MCP

multi-tenant · two-tier cache · RAG · AWS Bedrock · Kubernetes-ready · 36 tests · ReAct chain-of-thought

MCP Data Agents is a production-grade analytics system that routes natural-language questions through a parallel agent pipeline — Planner → Semantic + Benchmark → Insight → Synthesis — backed by a two-tier cache (Redis L1 + ChromaDB L2), per-tenant RAG, and a live Streamlit dashboard. Swap Anthropic API ↔ AWS Bedrock with one env var.

## What it does

- **Multi-agent orchestration** — Planner dispatches to Semantic + Benchmark agents in parallel; Insight agent synthesizes; Claude renders the final answer
- **Two-tier cache** — Redis L1 exact match (SHA-256, sub-ms) → ChromaDB L2 semantic cosine (≥ 0.85 threshold, ~10ms) → full pipeline (~8s)
- **RAG** — per-tenant ChromaDB collections; ingest any `.txt` document via REST
- **Chain-of-thought + follow-ups** — after every answer, 3 AI-generated next questions + "Visualize this" opens a Plotly chart in-browser automatically
- **MCP servers** — PowerBI, Tableau, Snowflake protocol adapters for tool-calling
- **Auth** — API key / JWT (JWKS) / none; per-tenant allowlisting; Redis rate limiting (60 req/min sliding window)
- **Security** — 14-pattern prompt injection detector, PII scanner, tool allowlist, RAG namespace isolation
- **Observability** — Prometheus metrics, Grafana dashboards, structlog JSON → CloudWatch / ELK, SQLite cost ledger, Redis audit log
- **Streamlit dashboard** — Revenue Analytics + Agent Operations tabs with real-time warehouse and ledger data
- **Kubernetes** — EKS with HPA (2–10 pods), IRSA, NetworkPolicy default-deny, PodDisruptionBudget, ResourceQuota

## How it works (30 seconds)

```
  natural language question
        │
        ▼
  ┌──────────────────────────────────────────────────────────┐
  │  FastAPI  ·  auth  ·  rate-limit  ·  injection check     │
  └──────────────────────────────────────────────────────────┘
        │
        ▼
  ┌──────────────────────────────────────────────────────────┐
  │  Cache                                                   │
  │  Redis L1  →  exact SHA-256 match        sub-ms · 24h   │
  │  ChromaDB L2  →  semantic cosine ≥ 0.85  ~10ms  · 7d    │
  └──────────────────────────────────────────────────────────┘
        │  miss
        ▼
  ┌──────────────────────────────────────────────────────────┐
  │  Planner Agent  →  intent + agent task list              │
  │    ├── Semantic Agent    (RAG · ChromaDB per-tenant)     │
  │    └── Benchmark Agent   (SQL · warehouse.db)            │
  │              ↓  parallel                                 │
  │         Insight Agent    (cross-agent synthesis)         │
  │              ↓                                           │
  │         Claude  (Anthropic API  or  AWS Bedrock)         │
  └──────────────────────────────────────────────────────────┘
        │
        ▼
  follow-up suggestions (1–3) + "Visualize" → Plotly HTML in browser
  cost ledger · Prometheus metrics · Redis session history · audit log
```

- **Planner** — reads query intent, emits an agent task list and plan confidence score
- **Semantic Agent** — vector search over tenant RAG store (ChromaDB)
- **Benchmark Agent** — structured SQL against `data/warehouse.db`
- **Insight Agent** — synthesizes agent outputs when both run
- **Synthesis** — final call to Claude with all context; yields streamed SSE or synchronous JSON

## Quick start

```bash
# 1 — Install
pip install -r requirements.txt

# 2 — Set the LLM key
export ANTHROPIC_API_KEY="sk-ant-..."

# 3 — Start the API (seeds the database on first run)
uvicorn api:app --reload

# 4 — Ask a question
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -H "X-API-Key: dev-key-change-me" \
  -H "X-Tenant-ID: demo" \
  -d '{"question": "What drove the Q3 2023 revenue spike?"}'

# 5 — Launch the Streamlit dashboard
python -m streamlit run dashboard.py

# 6 — Interactive CLI with chain-of-thought + follow-up menu
python main.py
```

Or via Docker Compose — Redis, ChromaDB, Prometheus, Grafana included:

```bash
cp .env.example .env          # add ANTHROPIC_API_KEY and API_KEYS
docker compose up -d
# Grafana: http://localhost:3000  (admin / admin)
```

## Cache performance

| Tier | Match type | Latency | TTL | Eviction |
|------|-----------|---------|-----|----------|
| Redis L1 | Exact SHA-256 | < 1 ms | 24 h | LRU 256 MB |
| ChromaDB L2 | Semantic cosine ≥ 0.85 | ~10 ms | 7 d | manual |
| Full pipeline | — | ~8 s | — | — |

Good-feedback answers are written to both tiers. Bad-feedback answers are invalidated from L1 immediately. Rate limit: 60 req/min per tenant sliding window.

## API

| Method | Path | Auth | Description |
|--------|------|:----:|-------------|
| `POST` | `/query` | ✅ | Synchronous query |
| `GET` | `/query/stream` | ✅ | SSE streaming query |
| `POST` | `/ingest` | ✅ | Ingest `.txt` into tenant RAG |
| `GET` | `/stats` | ✅ | Tenant RAG stats + source list |
| `GET` | `/history` | ✅ | Q&A history |
| `GET` | `/costs/summary` | ✅ | Cost summary |
| `GET` | `/costs/by-team` | ✅ | Team-level spend |
| `GET` | `/costs/by-agent` | ✅ | Per-agent token + cost breakdown |
| `DELETE` | `/cache` | ✅ | Invalidate one cached answer |
| `DELETE` | `/cache/bulk` | ✅ | Rollback cache after timestamp |
| `GET` | `/cache/health` | ✅ | Semantic cache hit-rate metrics |
| `GET` | `/redis/audit` | ✅ | Audit log (last N entries) |
| `GET` | `/redis/rate` | ✅ | Current rate usage for tenant |
| `DELETE` | `/redis/cache` | ✅ | Invalidate Redis L1 entry |
| `GET` | `/redis/health` | ❌ | Redis health (ops, no auth) |
| `GET` | `/metrics` | ❌ | Prometheus scrape endpoint |
| `GET` | `/health` | ❌ | Liveness probe |

## Auth

Three modes via `AUTH_MODE` env var:

| Mode | Mechanism | Use case |
|------|-----------|----------|
| `api_key` | `X-API-Key` header checked against `API_KEYS` (comma-separated) | Default |
| `jwt` | `Authorization: Bearer <token>` verified against `JWKS_URL` | Production SSO |
| `none` | Pass-through | Local dev only |

Tenant allowlisting: set `ALLOWED_TENANTS="tenant-a,tenant-b"` to restrict access. Empty = all authenticated tenants allowed. JWT claims `tid` / `sub` / `team` are extracted automatically.

## Security

Every query passes through `security.py` before reaching the pipeline:

- **Prompt injection** — 14 regex patterns covering jailbreak, instruction override, and system-prompt extraction attempts; detected queries return 400
- **PII scanner** — strips email, phone, SSN, and credit card patterns before RAG ingestion
- **Tool allowlist** — only declared MCP tools can be invoked; arbitrary tool names blocked at the orchestrator
- **RAG isolation** — tenant ChromaDB collections are namespace-scoped; cross-tenant reads are structurally impossible

## Deployment

### Docker Compose (local / staging)

```bash
docker compose up -d
```

Five services: `app`, `redis` (LRU 256 MB), `chromadb`, `prometheus`, `grafana`. All health-checked with `depends_on` conditions.

### Kubernetes (EKS / production)

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/secret.yaml   # fill ANTHROPIC_API_KEY + API_KEYS first
kubectl apply -f k8s/
```

| Feature | Detail |
|---------|--------|
| HPA | 2 → 10 pods · CPU 60% / RAM 75% · 300 s scale-down stabilization |
| IRSA | Pod gets Bedrock credentials from IAM role — no long-lived keys in cluster |
| NetworkPolicy | Default-deny namespace · explicit allow: app↔redis, app↔chromadb, app→443 |
| PDB | `minAvailable: 1` for app, Redis, ChromaDB — no full outage during rolling deploy |
| ResourceQuota | Namespace ceiling: 8 CPU / 16 Gi RAM / 20 pods / 50 Gi storage |
| LimitRange | Defaults 500m/512Mi · min 50m/64Mi · max 2/4Gi per container |

### AWS Bedrock

Two env vars, zero code changes:

```bash
USE_BEDROCK=true
BEDROCK_REGION=us-east-1
# Optional: BEDROCK_MODEL_ID=us.anthropic.claude-3-5-sonnet-20241022-v2:0
```

Credential chain: IRSA → instance profile → env vars. The factory in `bedrock_client.py` handles client construction; the rest of the pipeline is unaffected.

## CI / CD

`.github/workflows/ci.yml` — three jobs, triggered on push / PR to `main`:

| Job | What it does |
|-----|-------------|
| `test` | `ruff` lint + `pytest` with a Redis service container |
| `build-and-push` | Docker build → ECR tagged `sha-<git-hash>` + `latest` via AWS OIDC (no long-lived credentials) |
| `deploy` | `kubectl apply` all manifests, waits for rollout to complete |

Required GitHub secrets: `AWS_DEPLOY_ROLE_ARN`, `AWS_REGION`, `EKS_CLUSTER_NAME`.

## Observability

| Signal | Tool | Location |
|--------|------|---------|
| Metrics | Prometheus | `GET /metrics` · scraped every 15 s |
| Dashboards | Grafana | `localhost:3000` (Docker Compose) |
| Structured logs | structlog JSON | stdout → CloudWatch / ELK in prod |
| Cost audit | SQLite ledger | `data/cost_ledger.db` |
| Redis audit | Sorted Set (30 d retention) | `GET /redis/audit` |
| Query history | Redis List (7 d) + JSON fallback | `GET /history` |

## Project layout

```
mcp-data-agents/
├── api.py                   # FastAPI entrypoint — all HTTP endpoints
├── main.py                  # Interactive CLI (chain-of-thought + follow-up menu)
├── dashboard.py             # Streamlit dashboard (Revenue + Agent Operations tabs)
├── orchestrator.py          # MCP server session manager
├── security.py              # Prompt injection + PII filters
├── auth.py                  # API key / JWT authentication
├── redis_memory.py          # Redis L1 cache · session history · rate limit · audit
├── bedrock_client.py        # Anthropic / Bedrock client factory
├── logging_config.py        # structlog JSON config (LOG_FORMAT=json|pretty)
├── cost_ledger.py           # SQLite spend attribution per tenant/team/agent
├── metrics.py               # Prometheus metric definitions
├── observability.py         # QueryTrace + AgentCost dataclasses
├── tenant.py                # Tenant identity (used by SSE endpoint)
├── agents/
│   ├── planner.py           # Intent classification → agent task list
│   ├── semantic_agent.py    # RAG vector search (ChromaDB)
│   ├── benchmark_agent.py   # SQL queries against warehouse.db
│   ├── insight_agent.py     # Cross-agent synthesis
│   └── base_agent.py        # Shared agent interface
├── rag/
│   ├── store.py             # Per-tenant ChromaDB collection management
│   ├── ingest.py            # Document ingestion + retrieval
│   └── chroma_client.py     # Local PersistentClient / remote HttpClient factory
├── servers/
│   ├── powerbi_server.py    # MCP PowerBI adapter (simulated)
│   ├── tableau_server.py    # MCP Tableau adapter (simulated)
│   └── snowflake_server.py  # MCP Snowflake adapter (simulated)
├── data/
│   ├── seed.py              # Warehouse seed data + DB_PATH
│   └── warehouse.db         # SQLite analytics store
├── tests/                   # pytest suite — 36 tests
├── k8s/                     # 11 Kubernetes manifests
├── docker/                  # Prometheus + Grafana provisioning config
├── .github/workflows/       # CI/CD pipeline
└── docker-compose.yml       # Local 5-service stack
```

## When to use · When to skip

**Great fit if you…**
- need a reference architecture for multi-agent analytics — auth, caching, observability, K8s, CI/CD all wired up
- want to evaluate MCP protocol for tool-calling against BI / data warehouse integrations
- are benchmarking Anthropic API vs AWS Bedrock performance and cost on the same workload
- want a chain-of-thought CLI with automatic follow-up chaining and in-browser visualization

**Skip it if you…**
- need real PowerBI / Tableau / Snowflake connectors — the MCP adapters here are simulated
- want a managed vector store — ChromaDB runs locally; there's no hosted embedding service
- only need a single-agent chatbot — the parallel orchestration overhead is not justified

## Contributing

```bash
git clone https://github.com/<you>/mcp-data-agents.git
cd mcp-data-agents
pip install -r requirements.txt
export ANTHROPIC_API_KEY="sk-ant-..."
pytest
```

## License

MIT

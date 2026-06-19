# MCP Data Agents - Complete Tool Inventory

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                     USER INTERFACES                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  CLI (main.py)          │  REST API (api.py)                    │
│  ─────────────          │  ──────────────                        │
│  • Interactive REPL     │  • POST /query                         │
│  • Rich Markdown render │  • GET /query/stream (SSE)            │
│  • File ingestion       │  • POST /ingest (upload files)        │
│  • Feedback collection  │  • GET /metrics (Prometheus)          │
│                         │  • GET /stats, /history, /cache/...   │
│                         │  • GET /costs/* (cost tracking)        │
│                         │  • DELETE /cache (cache management)    │
│                         │                                         │
└─────────────────────────────────────────────────────────────────┘
```

---

## Tool Categories & Inventory

### 1️⃣ MCP SERVERS & TOOLS (3 Servers, 11 Tools)

#### **Snowflake Server** (`snowflake_server.py`)
```
MCP Instance: snowflake-warehouse

Tools:
├── list_tables()
│   └─ Lists: sales_fact, product_dim, customer_dim, region_dim, date_dim
│
├── describe_table(table_name)
│   └─ Returns: schema, column names, data types
│
└── run_sql_query(query)
    ├─ SELECT only (read-only)
    ├─ Max 500 rows, 4000 chars
    ├─ SQL injection protection
    └─ 15s timeout per call
```

#### **Tableau Server** (`tableau_server.py`)
```
MCP Instance: tableau-dashboards

Tools:
├── list_dashboards()
│   └─ Dashboards: regional_performance, product_trends, customer_segments, executive_kpis
│
├── get_dashboard_summary(dashboard_id)
│   └─ Returns: metadata & available views
│
├── get_benchmark_data(benchmark_type, time_period)
│   ├─ Types: regional_vs_target, category_performance, segment_comparison, quarterly_trend
│   └─ Periods: 'all', '2023', '2024', '2024-Q1', '2024-03'
│
└── get_top_performers(entity_type, metric, limit, time_period)
    ├─ Entities: products, customers, regions
    └─ Metrics: revenue, transactions, margin_pct
```

#### **Power BI Server** (`powerbi_server.py`)
```
MCP Instance: powerbi-semantic

Semantic Models:
├── sales_performance
│   ├─ Measures: total_revenue, gross_margin_pct, avg_order_value,
│   │             revenue_growth_mom, revenue_growth_yoy
│   └─ Dimensions: region, product_category, customer_segment, year, quarter, month
│
└── customer_analytics
    ├─ Measures: customer_ltv, revenue_per_customer, unique_customers
    └─ Dimensions: segment, country

Tool: get_metric(metric_name, time_period, dimension)
```

---

### 2️⃣ AGENT EXECUTION TOOLS (4 Agents)

| Agent | File | Purpose | Data Source |
|-------|------|---------|-------------|
| **Planner** | `planner.py` | Route questions to agents | Claude reasoning |
| **Semantic** | `semantic_agent.py` | KPI interpretation | Power BI metrics |
| **Benchmark** | `benchmark_agent.py` | Comparative analysis | Tableau dashboards |
| **Insight** | `insight_agent.py` | Root cause analysis | Snowflake SQL queries |

All use: `base_agent.py` (async loop, retries, token tracking)

---

### 3️⃣ DATA TOOLS (Storage & Retrieval)

#### **SQLite Databases**

**Warehouse** (`data/warehouse.db`)
- `sales_fact` - 6,033+ transaction records
- `product_dim` - 8 products
- `customer_dim` - 10 customers
- `region_dim` - 4 regions
- `date_dim` - 547 days (2023-2024)

**Cost Ledger** (`data/cost_ledger.db`)
- `query_costs` - Token usage, cost, cache hits, tenant attribution

#### **Vector Database (ChromaDB)** (`data/chroma/`)

Collections:
- `domain_knowledge` - Shared schema docs, metrics, benchmarks
- `qa_history_{tenant_id}` - Cached Q&A with 24h TTL
- `documents_{tenant_id}` - Ingested documents (chunked, 500 chars)

Operations:
- `retrieve()` - Cache + context lookup
- `store_qa()` - Cache new answers
- `query_documents()` - Search ingested docs
- `flag_bad()` - Delete poisoned cache

#### **Dataset Loading** (`data/`)

- `datasets.py` - DatasetLoader base class, Kaggle, UCI, Synthetic implementations
- `download_datasets.py` - CLI tool to fetch datasets
- `seed.py` - Database seeding (demo or real mode)

---

### 4️⃣ EXTERNAL API INTEGRATIONS

#### **Anthropic Claude API**
```
Model: claude-sonnet-4-6
Used in: Planner, Semantic, Benchmark, Insight, Synthesis, Quality Judge
Features: Tool use, streaming, token tracking, error handling
```

#### **Model Context Protocol (MCP)**
```
Framework for: Snowflake, Tableau, Power BI integration
Server Manager: orchestrator.py
Per-call Timeout: 15s
Communication: stdio
```

---

### 5️⃣ SECURITY TOOLS (`security.py`)

```
check_query(text)           - Injection detection, size limits
check_ingest(content)       - Embedded instruction detection
check_pii(text)            - SSN, CC, email, phone, API key scanning
check_tool_call(name, args) - Tool allowlisting
wrap_rag_context(context)  - XML delimiters + data-only instruction
```

---

### 6️⃣ OBSERVABILITY & COST TOOLS

#### **Prometheus Metrics** (`metrics.py`)
- Counters: queries, cost, avoided_cost, agent_cost, bad_feedback, tool_calls
- Histograms: query_latency
- Gauges: cache_size, mcp_servers_up

#### **Query Tracing** (`observability.py`)
- QueryTrace class - Captures latency, tokens, cost, agents, feedback
- AgentCost - Per-agent token counting

#### **Cost Ledger** (`cost_ledger.py`)
- `record()` - Persist traces
- `summary()` - Cost aggregation
- `by_team()` - Chargeback reports
- `cache_roi()` - Savings analysis
- `by_agent()` - Per-agent breakdown

---

### 7️⃣ TESTING & EVALUATION

```
eval/runner.py   - Full evaluation framework, stress testing
eval/dataset.py  - 14 test cases (routing, quality, security, edge cases)
eval/judge.py    - LLM quality scoring (relevance, specificity, format, groundedness)

tests/test_*.py  - Unit tests for servers, RAG, ingest, security
```

---

### 8️⃣ DEPENDENCIES

**Core (12):**
anthropic, mcp, chromadb, fastapi, uvicorn, httpx, rich, pandas, pytest, pytest-asyncio, prometheus_client, sqlite3

**Optional (3):**
pypdf, openpyxl, faker

---

### 9️⃣ API ENDPOINTS (12+)

```
Query Processing:
  POST /query               - Synchronous query
  GET /query/stream         - SSE streaming

Information:
  GET /health              - Service status
  GET /metrics             - Prometheus metrics
  GET /stats               - RAG stats
  GET /history             - Q&A history

Content:
  POST /ingest             - Upload files

Cache:
  DELETE /cache            - Remove entry
  DELETE /cache/bulk       - Rollback by timestamp
  GET /cache/health        - Cache quality

Costs:
  GET /costs/summary       - Total spend
  GET /costs/by-team       - Team breakdown
  GET /costs/cache-roi     - Cache savings
  GET /costs/by-agent      - Per-agent breakdown
```

---

## Summary Statistics

| Category | Count |
|----------|-------|
| MCP Servers | 3 |
| MCP Tools | 11 |
| Agents | 4 |
| API Endpoints | 12+ |
| Database Tables | 9 |
| ChromaDB Collections | 3+ |
| Security Checks | 4 |
| Dependencies | 15 |
| Files | 40+ |
| Prometheus Metrics | 12 |
| External APIs | 2 |

---

## Tool Grouping by Purpose

**Query & Analytics:**
- Snowflake (SQL queries)
- Tableau (Comparative analysis, benchmarks)
- Power BI (KPI metrics)

**AI/LLM:**
- Anthropic Claude
- Agent Framework (Planner, Semantic, Benchmark, Insight)
- Tool Orchestration (MCP)

**Data Storage:**
- SQLite (Warehouse, Cost Ledger)
- ChromaDB (RAG cache, ingested documents)

**Monitoring:**
- Prometheus (metrics export)
- Query traces (latency, tokens, cost)
- Cost ledger (spend tracking)

**Security:**
- Input validation (queries, documents)
- PII detection
- Tool allowlisting
- RAG context isolation

**Infrastructure:**
- FastAPI (REST API)
- Uvicorn (ASGI server)
- AsyncIO (concurrent execution)
- pytest (testing)

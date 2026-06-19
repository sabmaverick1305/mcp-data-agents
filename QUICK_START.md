# MCP Data Agents - Quick Start Guide

## 🚀 Get Started in 2 Minutes

### Prerequisites
- Python 3.10+
- Anthropic API key (get it from https://console.anthropic.com/account/keys)

### Installation

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set your API key
export ANTHROPIC_API_KEY="sk-ant-..."

# 3. Run the system (pick one option below)
```

---

## Run Options

### Option A: Interactive CLI (Recommended for first-time)

```bash
# With real data (6,033 records)
SEED_MODE=real python main.py

# Or with demo data (3,210 records, default)
python main.py
```

**What you'll see:**
```
╭────────────────────────────────────────────────────────────────╮
│                                                                │
│    MCP Data Agents                                             │
│    Multi-agent analytics · RAG · Semantic Cache · Streaming    │
│                                                                │
╰────────────────────────────────────────────────────────────────╯

[green]RAG ready.[/green] [dim]12 domain docs · 0 cached Q&As[/dim]
[green]MCP servers ready.[/green] [dim]['powerbi-semantic', 'tableau-dashboards', 'snowflake-warehouse'][/dim]

Try asking:
  • Why did revenue drop in Q1 2024?
  • Which regions are below target and what's driving it?
  • What are our top 5 products by revenue in 2024?
  /ingest <path>  — add a document to RAG
  /docs           — list ingested documents

You ▶
```

**Try a query:**
```
You ▶ Why did revenue drop in Q1 2024?

[Processing...]
▶ Planner Agent starting…
[green]✓[/green] [bold]Planner Agent[/bold] done

▶ Semantic Agent starting…
[green]✓[/green] [bold]Semantic Agent[/bold] done

▶ Insight Agent starting…
[green]✓[/green] [bold]Insight Agent[/bold] done

[bold]Answer[/bold]
Q1 2024 Revenue Analysis...
[table with results]

Rate this answer — [g]ood / [b]ad / [s]kip: g
[green]Saved to memory.[/green]

Trace
├─ Latency: 8.43s
├─ Tokens: 2,847 input, 1,234 output
├─ Cost: $0.084
└─ Cache: Miss
```

---

### Option B: REST API Server

```bash
# Start the API server
SEED_MODE=real python -m uvicorn api:app --reload

# Server starts at http://localhost:8000
```

**Query the API:**
```bash
# In another terminal:
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: demo" \
  -H "X-User-ID: alice" \
  -d '{"question":"What is our total revenue?"}'
```

**API Endpoints:**
- `POST /query` — Synchronous query
- `GET /query/stream` — Streaming SSE
- `POST /ingest` — Upload documents
- `GET /metrics` — Prometheus metrics
- `GET /stats` — Statistics
- `GET /history` — Q&A history
- `GET /costs/summary` — Cost tracking
- `DELETE /cache` — Clear cache

---

### Option C: Run Evaluation Tests

```bash
# Run 14 test cases (routing, quality, security)
SEED_MODE=real python -m eval.runner --no-judge

# With full LLM quality scoring
SEED_MODE=real python -m eval.runner --output eval_results.json

# Stress test (20 concurrent queries)
SEED_MODE=real python -m eval.runner --stress 20
```

---

### Option D: Add Real Data (UCI Dataset)

```bash
# Download UCI Online Retail dataset (500K+ records)
python data/download_datasets.py --dataset uci

# Seed database with real data
SEED_MODE=real python data/seed.py

# Run evaluation with massive dataset
SEED_MODE=real python -m eval.runner
```

---

## Sample Queries to Try

### In CLI Mode

```
You ▶ What is our total revenue?
You ▶ Which regions are below their revenue targets?
You ▶ Compare enterprise vs SMB segment performance
You ▶ Give me an executive summary of 2024 sales
You ▶ Why did Q1 2024 have lower revenue than Q2?
You ▶ Top 5 customers by revenue in 2024
You ▶ What is our average order value?
You ▶ Which product categories are most profitable?
```

After each answer, rate it:
- `g` or `good` — Save to memory for future similar queries
- `b` or `bad` — Remove from cache (query was incorrect)
- `s` or `skip` — Just continue (don't cache)

---

## File Management in CLI

```
You ▶ /ingest /path/to/document.txt
[green]Ingested 12 chunks from /path/to/document.txt[/green]

You ▶ /docs
  • /path/to/document.txt
  • /path/to/other_document.pdf

You ▶ exit
[dim]Goodbye.[/dim]
```

---

## Data Modes

### Demo Mode (Default)
```bash
python main.py
# or
SEED_MODE=demo python main.py

✓ 3,210 synthetic sales records
✓ 8 products, 10 customers, 4 regions
✓ Includes Q1 2024 revenue dip for testing
✓ Loads in <1 second
```

### Real Mode (Kaggle Data)
```bash
SEED_MODE=real python main.py

✓ 6,033 total records (demo + Kaggle)
✓ Real sales transactions from Kaggle dataset
✓ Diverse product/customer base
✓ Loads in ~2 seconds
```

### Full Real Mode (Kaggle + UCI)
```bash
# First: Download UCI dataset
python data/download_datasets.py --dataset uci

# Then: Seed with all data
SEED_MODE=real python data/seed.py

SEED_MODE=real python main.py

✓ 506,000+ total records
✓ Real e-commerce transaction data
✓ Production-scale dataset
✓ Loads in ~30 seconds
```

---

## System Architecture

```
User Input (CLI/API)
    ↓
[Security Check] (injection detection, PII scan)
    ↓
[RAG Cache] (ChromaDB semantic search)
    ├─ Hit? → Return cached answer
    └─ Miss? → Continue pipeline
    ↓
[Planner Agent] (route to semantic/benchmark/insight agents)
    ↓
[Parallel Agents] (run concurrently)
├─ Semantic Agent (Power BI metrics)
├─ Benchmark Agent (Tableau dashboards)
└─ Insight Agent (Snowflake SQL)
    ↓
[Synthesis] (Claude combines results)
    ↓
[Store & Track]
├─ Cache Q&A in ChromaDB
├─ Record cost in SQLite
├─ Export to Prometheus
└─ Save trace
    ↓
Response
├─ Markdown answer with tables
├─ Cost breakdown
├─ Latency
└─ Trace metadata
```

---

## Troubleshooting

### Error: ANTHROPIC_API_KEY is not set
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

### Error: MCP servers not starting
```bash
# Make sure you have all dependencies
pip install -r requirements.txt

# Check if ports are available (stdio, no ports used)
```

### Error: Cannot decode CSV with encoding
The dataset loader automatically tries multiple encodings (UTF-8, Latin-1, ISO-8859-1, CP1252). If still failing:
```bash
# Check the file manually
file data/raw/*.csv

# Or convert encoding
iconv -f CP1252 -t UTF-8 file.csv > file_utf8.csv
```

### Q&A cache not working
```bash
# Clear cache to reset
curl -X DELETE http://localhost:8000/cache/bulk?cutoff_ts=0

# Or in CLI
You ▶ exit
```

---

## Monitoring & Metrics

### View Cost Summary
```bash
curl http://localhost:8000/costs/summary
```

### View Prometheus Metrics
```bash
curl http://localhost:8000/metrics | grep mcp_agents
```

### View Cache Health
```bash
curl http://localhost:8000/cache/health
```

---

## Next Steps

1. **Try the CLI first** — Get a feel for how agents work
2. **Run evaluation** — See routing accuracy and quality scores
3. **Add real data** — Download UCI dataset for production testing
4. **Upload documents** — Use `/ingest` to add company data
5. **Deploy API** — Run the REST server for production use

---

## Architecture Components

| Component | Tool | Purpose |
|-----------|------|---------|
| **Data Sources** | Snowflake, Tableau, Power BI | Provide metrics, dashboards, SQL queries |
| **Agents** | 4 specialized agents | Route questions, analyze data, generate insights |
| **Cache** | ChromaDB | Semantic cache for Q&A, RAG for documents |
| **Database** | SQLite | Warehouse data, cost ledger |
| **API** | FastAPI | REST endpoints for querying, metrics, costs |
| **Monitoring** | Prometheus | Track queries, cost, latency, cache hits |
| **AI** | Claude API | Planner, synthesis, quality scoring |

---

## Key Features

✅ **Multi-Agent Routing** — Planner decides which agents to use
✅ **Parallel Execution** — Semantic + Benchmark run together
✅ **Semantic Caching** — Avoid redundant queries via ChromaDB
✅ **Cost Tracking** — Per-query cost calculation & attribution
✅ **Security** — Input validation, PII detection, tool allowlisting
✅ **Multi-Tenant** — Per-tenant RAG, history, cost tracking
✅ **Streaming** — Real-time SSE responses
✅ **Feedback Loop** — User ratings train the cache

---

## Useful Commands

```bash
# Start CLI with real data
SEED_MODE=real python main.py

# Start API server
SEED_MODE=real python -m uvicorn api:app --reload

# Run evaluation
SEED_MODE=real python -m eval.runner --output results.json

# Run tests
pytest -v

# Download datasets
python data/download_datasets.py

# Seed database
python data/seed.py --mode real

# Query API
curl -X POST http://localhost:8000/query \
  -H "X-Tenant-ID: demo" \
  -d '{"question":"..."}'
```

---

**Ready to run? Start with:**
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
SEED_MODE=real python main.py
```

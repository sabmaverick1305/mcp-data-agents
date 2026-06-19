#!/usr/bin/env python3
"""MCP Data Agents — Architecture Diagram (Kafka-style column layout)."""
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

# ── Canvas ────────────────────────────────────────────────────────────
fig, ax = plt.subplots(figsize=(22, 14))
ax.set_xlim(0, 22)
ax.set_ylim(0, 14)
ax.axis('off')
ax.set_facecolor('#F8F9FA')
fig.patch.set_facecolor('#F8F9FA')

# ── Palette ───────────────────────────────────────────────────────────
G,  LG = '#2E7D32', '#F1F8E9'   # green  – Data Sources
B,  LB = '#1565C0', '#E3F2FD'   # blue   – Ingestion
O,  LO = '#E65100', '#FFF8E1'   # orange – Agent Orchestration
P,  LP = '#6A1B9A', '#F3E5F5'   # purple – Consumers
T,  LT = '#00695C', '#E0F2F1'   # teal   – Sinks
M,  LM = '#37474F', '#ECEFF1'   # slate  – Monitoring

# ── Primitives ────────────────────────────────────────────────────────
def rbox(x, y, w, h, fc, ec, lw=1.2, ls='-', r=0.05):
    ax.add_patch(FancyBboxPatch(
        (x, y), w, h, boxstyle=f'round,pad={r}',
        facecolor=fc, edgecolor=ec, linewidth=lw, linestyle=ls, zorder=2))

def hdr(x, y, w, color, num, title, fs=9.0):
    """Colored section header."""
    rbox(x, y, w, 0.46, color, color, r=0.04)
    ax.text(x + w / 2, y + 0.23, f'{num}. {title}',
            ha='center', va='center', fontsize=fs,
            color='white', fontweight='bold', zorder=3)

def cbox(x, y, w, h, title, items, tc, ec,
         fc='white', tfs=7.8, ifs=7.0, item_gap=0.21):
    """Component box with bullet items."""
    rbox(x, y, w, h, fc, ec)
    ax.text(x + w / 2, y + h - 0.15, title,
            ha='center', va='top', fontsize=tfs,
            color=tc, fontweight='bold', zorder=3)
    for i, item in enumerate(items):
        ax.text(x + 0.10, y + h - 0.34 - i * item_gap,
                f'• {item}', ha='left', va='top',
                fontsize=ifs, color='#333333', zorder=3)

def arr(x1, y, x2, label='', color='#555555'):
    """Horizontal arrow with optional label."""
    ax.annotate('', xy=(x2, y), xytext=(x1, y),
                arrowprops=dict(arrowstyle='->', color=color, lw=1.8), zorder=4)
    if label:
        ax.text((x1 + x2) / 2, y + 0.13, label,
                ha='center', va='bottom', fontsize=6.5, color=color, zorder=4)

# ── Layout constants ──────────────────────────────────────────────────
HY  = 12.60   # header row y (bottom of header)
CHT = 12.10   # content area top
CHB = 5.00    # content area bottom

SX = [0.30, 4.05, 7.80, 13.65, 17.45]
SW = [3.35, 3.35, 5.45, 3.40, 4.10]

# ── Title ─────────────────────────────────────────────────────────────
ax.text(11, 13.72, 'MCP Data Agents — Architecture & Workflow',
        ha='center', va='center', fontsize=17,
        fontweight='bold', color='#1A237E', zorder=5)
ax.text(11, 13.30,
        'Multi-Agent Analytics Platform  ·  Claude / AWS Bedrock'
        '  ·  Multi-Tenant  ·  Production-Grade',
        ha='center', va='center', fontsize=9.5, color='#555555', zorder=5)
ax.plot([0.5, 21.5], [13.08, 13.08], color='#3B82F6', lw=2.0, zorder=5)

# ── Section headers & dashed borders ─────────────────────────────────
nums   = ['1', '2', '3', '4', '5']
titles = ['Data Sources', 'Ingestion Layer',
          'MCP Agent Orchestration', 'Consumers', 'Data Sinks / Output']
cols   = [G,  B,  O,  P,  T]
bgs    = [LG, LB, LO, LP, LT]

for i in range(5):
    hdr(SX[i], HY, SW[i], cols[i], nums[i], titles[i])
    rbox(SX[i], CHB, SW[i], CHT - CHB,
         bgs[i], cols[i], lw=1.4, ls='--', r=0.06)

# ══════════════════════════════════════════════════════════════════════
# SECTION 1 – Data Sources
# ══════════════════════════════════════════════════════════════════════
bx1, bw1 = SX[0] + 0.13, SW[0] - 0.26
s1_boxes = [
    ('Warehouse DB',
     ['SQLite · warehouse.db', 'Revenue & Sales Data', 'Demo Dataset (seeded)']),
    ('Business Documents',
     ['Strategy Reports', 'KPI Definitions', 'Glossaries & Policies']),
    ('External Inputs',
     ['APIs / Microservices', 'IoT / Sensor Streams', 'Clickstream Logs']),
    ('Vector Store',
     ['ChromaDB embeddings', 'Per-tenant RAG index', 'Semantic search ready']),
]
y = CHT - 0.12
for title, items in s1_boxes:
    h = 0.28 + len(items) * 0.22
    y -= h
    cbox(bx1, y, bw1, h, title, items, G, G)
    y -= 0.15

# ══════════════════════════════════════════════════════════════════════
# SECTION 2 – Ingestion Layer
# ══════════════════════════════════════════════════════════════════════
bx2, bw2 = SX[1] + 0.13, SW[1] - 0.26
s2_boxes = [
    ('FastAPI /ingest',
     ['POST /ingest endpoint', 'Multipart file upload', 'Tenant-scoped writes']),
    ('RAG Pipeline',
     ['rag/ingest.py', 'Chunk & embed text', 'ChromaDB write path']),
    ('MCP Connectors',
     ['powerbi_server.py', 'tableau_server.py', 'snowflake_server.py']),
    ('Data Seed',
     ['data/seed.py', 'Demo warehouse seed', 'Auto-runs on startup']),
]
y = CHT - 0.12
for title, items in s2_boxes:
    h = 0.28 + len(items) * 0.22
    y -= h
    cbox(bx2, y, bw2, h, title, items, B, B)
    y -= 0.15

# ══════════════════════════════════════════════════════════════════════
# SECTION 3 – MCP Agent Orchestration  (wider centre)
# ══════════════════════════════════════════════════════════════════════
bx3, bw3 = SX[2] + 0.13, SW[2] - 0.26
bwh = (bw3 - 0.10) / 2   # half-width for parallel agents

y3 = CHT - 0.12

# Auth guard
h = 0.28 + 3 * 0.22
y3 -= h
cbox(bx3, y3, bw3, h,
     'Auth Layer  (auth.py)',
     ['API Key / JWT (JWKS) / none modes',
      'Tenant allowlist validation (ALLOWED_TENANTS)',
      'Rate limit 60 req/min · Redis sliding window'],
     '#1A237E', '#3949AB', fc='#E8EAF6')
y3 -= 0.13

# Cache layer
h = 0.28 + 2 * 0.22
y3 -= h
cbox(bx3, y3, bw3, h,
     'Cache Layer',
     ['Redis L1  ·  exact SHA-256  ·  sub-ms  ·  24h TTL',
      'ChromaDB L2  ·  cosine ≥ 0.85  ·  ~10ms  ·  7d TTL'],
     '#E65100', '#FF8F00', fc='#FFFDE7')
ax.text(bx3 + bw3 * 0.72, y3 + h - 0.13,
        'cache miss ↓', fontsize=6.5, color='#FF8F00',
        style='italic', ha='center', va='top', zorder=3)
y3 -= 0.13

# Planner
h = 0.28 + 3 * 0.22
y3 -= h
cbox(bx3, y3, bw3, h,
     'Planner Agent  (agents/planner.py)',
     ['Classifies query intent & complexity',
      'Selects agents: semantic / benchmark / both',
      'Emits task list + plan confidence score'],
     O, O)
y3 -= 0.10

ax.text(SX[2] + SW[2] / 2, y3 + 0.07,
        '←   parallel execution   →',
        ha='center', va='center', fontsize=7.0,
        color=O, style='italic', zorder=3)
y3 -= 0.12

# Parallel agents
par_h = 0.28 + 4 * 0.22
y3 -= par_h
cbox(bx3, y3, bwh, par_h, 'Semantic Agent',
     ['rag/store.py', 'ChromaDB vector search',
      'Per-tenant collection', 'Cosine similarity rank'],
     O, O)
cbox(bx3 + bwh + 0.10, y3, bwh, par_h, 'Benchmark Agent',
     ['SQL on warehouse.db', 'Structured KPI queries',
      'Revenue / segment data', 'Schema-aware SQL gen'],
     O, O)
y3 -= 0.13

# Insight agent
h = 0.28 + 2 * 0.22
y3 -= h
cbox(bx3, y3, bw3, h,
     'Insight Agent  (agents/insight_agent.py)',
     ['Cross-agent synthesis when both agents run',
      'Combines semantic + benchmark results'],
     O, O)
y3 -= 0.13

# Claude / Bedrock
h = 0.28 + 3 * 0.22
y3 -= h
cbox(bx3, y3, bw3, h,
     'Claude  (Anthropic API  /  AWS Bedrock)',
     ['Final answer synthesis  ·  SSE stream or sync JSON',
      'bedrock_client.py factory  ·  swap via USE_BEDROCK=true',
      'claude-sonnet-4-6  or  us.anthropic.claude-3-5-sonnet'],
     '#BF360C', '#BF360C', fc='#FFF3E0')
y3 -= 0.13

# ReAct / Follow-ups
h = 0.28 + 2 * 0.22
y3 -= h
cbox(bx3, y3, bw3, h,
     'ReAct  ·  Chain-of-Thought  (main.py)',
     ['3 AI follow-up suggestions after every answer',
      'Pick 1–3 to auto-chain  ·  "Visualize" → Plotly HTML'],
     '#4527A0', '#4527A0', fc='#EDE7F6')
y3 -= 0.10

# Security (compact banner)
h = 0.40
y3 -= h
cbox(bx3, y3, bw3, h,
     '14-pattern injection detector  ·  PII scanner  ·  Tool allowlist  (security.py)',
     [], '#B71C1C', '#C62828', fc='#FFEBEE', tfs=7.2)

# Vertical flow arrows inside section 3
cx3 = SX[2] + SW[2] / 2

# ══════════════════════════════════════════════════════════════════════
# SECTION 4 – Consumers
# ══════════════════════════════════════════════════════════════════════
bx4, bw4 = SX[3] + 0.13, SW[3] - 0.26
s4_boxes = [
    ('Consumer Group 1',
     ['REST API', 'POST /query', 'Sync JSON response', 'X-API-Key auth']),
    ('Consumer Group 2',
     ['SSE Stream', 'GET /query/stream', 'Server-Sent Events', 'Real-time tokens']),
    ('Consumer Group 3',
     ['Streamlit Dashboard', 'Revenue Analytics tab',
      'Agent Operations tab', 'Plotly charts']),
    ('Consumer Group N',
     ['CLI  (main.py)', 'Chain-of-thought mode',
      'Follow-up menu 1–3', '"Visualize" → browser']),
]
y = CHT - 0.12
for title, items in s4_boxes:
    h = 0.28 + len(items) * 0.22
    y -= h
    cbox(bx4, y, bw4, h, title, items, P, P)
    y -= 0.15

# ══════════════════════════════════════════════════════════════════════
# SECTION 5 – Data Sinks
# ══════════════════════════════════════════════════════════════════════
bx5, bw5 = SX[4] + 0.13, SW[4] - 0.26
s5_boxes = [
    ('Cost Ledger',
     ['SQLite · cost_ledger.db', 'Per-query row', 'Tenant / Team / Agent']),
    ('Conversation Store',
     ['Redis List · 100 turns', '7-day TTL per tenant', 'GET /history fallback']),
    ('Audit Log',
     ['Redis Sorted Set', '30-day retention', 'GET /redis/audit']),
    ('Visualizations',
     ['Plotly HTML chart', 'Opens in browser', 'Bar / Line / Scatter']),
    ('Prometheus Metrics',
     ['GET /metrics endpoint', 'Cost · latency · cache', 'Grafana dashboards']),
]
y = CHT - 0.12
for title, items in s5_boxes:
    h = 0.28 + len(items) * 0.22
    y -= h
    cbox(bx5, y, bw5, h, title, items, T, T)
    y -= 0.12

# ══════════════════════════════════════════════════════════════════════
# Cross-section arrows (horizontal, mid-section)
# ══════════════════════════════════════════════════════════════════════
AY = 9.10   # arrow y level
labels_arr = ['Ingest\nData', 'Query +\nContext', 'Synthesized\nAnswer', 'Persist\nResults']
for i in range(4):
    arr(SX[i] + SW[i], AY, SX[i + 1], labels_arr[i])

# Side labels matching Kafka style
ax.text(SX[2] - 0.05, AY + 0.32, 'Publish\nQuery',
        ha='right', va='center', fontsize=7.0, color='#555555', style='italic', zorder=4)
ax.text(SX[3] + SW[3] + 0.05, AY + 0.32, 'Consume\nAnswer',
        ha='left', va='center', fontsize=7.0, color='#555555', style='italic', zorder=4)

# Replication / HA annotation (mirrors Kafka's dashed replication arrows)
for i in [1, 2]:
    ax.annotate('', xy=(SX[2] + SW[2], CHT - 1.2 - i * 1.4),
                xytext=(SX[2] + 0.20, CHT - 1.2 - i * 1.4),
                arrowprops=dict(arrowstyle='<->', color='#BDBDBD',
                                lw=0.9, linestyle='dashed'), zorder=2)
ax.text(SX[2] + SW[2] / 2, CHT - 0.95,
        'Cache Replication  (High Availability)',
        ha='center', va='center', fontsize=6.2, color='#9E9E9E',
        style='italic', zorder=3)

# ══════════════════════════════════════════════════════════════════════
# SECTION 6 – Monitoring, Security & Deployment  (full-width bottom)
# ══════════════════════════════════════════════════════════════════════
MY, MH, MHH = 0.72, 4.06, 0.42
rbox(0.30, MY, 21.25, MH, LM, M, lw=1.4, r=0.07)
rbox(0.30, MY + MH - MHH, 21.25, MHH, M, 'none', lw=0, r=0.05)
ax.text(11, MY + MH - MHH / 2,
        '6.  Monitoring, Security & Deployment Ecosystem',
        ha='center', va='center', fontsize=10.5,
        fontweight='bold', color='white', zorder=3)

panels6 = [
    ('Monitoring',
     ['Prometheus /metrics',
      'Grafana dashboards',
      '15 s scrape interval',
      'Cost · latency · cache',
      'Alert rules for SLOs']),
    ('Security & Auth',
     ['API Key / JWT (JWKS)',
      'Entra ID / Azure AD',
      'Rate limit 60 req/min',
      'Tenant allowlisting',
      'Prompt injection · PII']),
    ('Observability',
     ['structlog JSON logs',
      'stdout → CloudWatch/ELK',
      'QueryTrace per request',
      'Cost ledger (SQLite)',
      'Redis audit log (30 d)']),
    ('Deployment',
     ['Docker Compose (5 svc)',
      'Kubernetes EKS',
      'HPA 2–10 pods',
      'IRSA · OIDC · NetworkPolicy',
      'PDB · ResourceQuota']),
    ('CI / CD',
     ['GitHub Actions pipeline',
      'Docker build → ECR',
      'AWS OIDC (no secrets)',
      'kubectl apply all k8s/',
      'Rollout status wait']),
    ('MCP Ecosystem',
     ['PowerBI MCP server',
      'Tableau MCP server',
      'Snowflake MCP server',
      'Schema Registry',
      'Tool allowlist guard']),
]

n6   = len(panels6)
pw6  = (21.25 - 0.40) / n6          # panel slot width
ph6  = MH - MHH - 0.24              # panel height

for i, (ptitle, pitems) in enumerate(panels6):
    px = 0.30 + 0.20 + i * pw6
    py = MY + 0.12
    pw = pw6 - 0.15

    rbox(px, py, pw, ph6, 'white', '#90A4AE', lw=1.0, r=0.05)
    ax.text(px + pw / 2, py + ph6 - 0.12,
            ptitle, ha='center', va='top',
            fontsize=8.0, color=M, fontweight='bold', zorder=3)

    # Auto-space items to fill the panel height evenly
    n_items  = len(pitems)
    usable   = ph6 - 0.32               # below title
    spacing  = usable / (n_items + 0.3)

    for j, item in enumerate(pitems):
        ax.text(px + 0.10,
                py + ph6 - 0.32 - j * spacing,
                f'• {item}',
                ha='left', va='top',
                fontsize=6.8, color='#37474F', zorder=3)

# ══════════════════════════════════════════════════════════════════════
# Summary flow strip (very bottom, mirrors Kafka bottom row)
# ══════════════════════════════════════════════════════════════════════
STRIP_Y = 0.07
strip_entries = [
    ('Data Sources',         'Generate & Store',              G),
    ('Ingestion Layer',      'Embed & Index',                 B),
    ('Agent Orchestration',  'Plan → Execute → Synthesize',  O),
    ('Consumers',            'Request Answers',               P),
    ('Sinks / Outputs',      'Deliver Business Value',        T),
]
sw_each = (21.20 - 0.10) / len(strip_entries)

for i, (stitle, ssub, scol) in enumerate(strip_entries):
    sx = 0.40 + i * sw_each
    sw = sw_each - 0.28
    rbox(sx, STRIP_Y, sw, 0.58, scol, 'none', lw=0, r=0.04)
    ax.text(sx + sw / 2, STRIP_Y + 0.37, stitle,
            ha='center', va='center',
            fontsize=7.5, color='white', fontweight='bold', zorder=3)
    ax.text(sx + sw / 2, STRIP_Y + 0.17, ssub,
            ha='center', va='center',
            fontsize=6.5, color='white', zorder=3)
    if i < len(strip_entries) - 1:
        ax.annotate('',
                    xy=(sx + sw + 0.20, STRIP_Y + 0.29),
                    xytext=(sx + sw + 0.02, STRIP_Y + 0.29),
                    arrowprops=dict(arrowstyle='->',
                                    color='#757575', lw=1.4), zorder=4)

# ══════════════════════════════════════════════════════════════════════
# Save
# ══════════════════════════════════════════════════════════════════════
plt.tight_layout(pad=0.2)
plt.savefig('architecture.png', dpi=180, bbox_inches='tight',
            facecolor='#F8F9FA', edgecolor='none')
print('architecture.png saved.')

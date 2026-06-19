"""
Conversational CLI — interactive terminal interface for the MCP Data Agents system.

Provides a rich, streaming command-line experience with the full agent pipeline,
chain-of-thought visibility, automatic follow-up suggestions, and in-browser
Plotly visualization.

Feature walkthrough:
  1. Startup       Seeds the warehouse on first run, initialises RAG store, MCP servers,
                   and Redis (with fail-open fallback to JSON history).
  2. Query         Runs the full Planner → Semantic+Benchmark (parallel) → Insight → Synthesis
                   pipeline. Synthesis streams token-by-token into a Rich Live panel.
  3. Cache display Redis L1 hit → instant return with label. ChromaDB L2 hit → same.
                   Cache miss shows how many context chunks were injected.
  4. Follow-ups    After each non-cached answer, Claude generates 3 contextual next questions.
                   Typing [1], [2], or [3] auto-chains to the next question without prompting.
  5. Visualization Typing the visualize option ([4] or the last numbered choice) asks Claude
                   to generate a SQL + chart spec, runs the SQL against warehouse.db via
                   pandas, builds a Plotly figure, and opens it in the default browser.
  6. Feedback      [g]ood saves to both RAG cache and Redis L1. [b]ad invalidates from both.
                   Skip or numeric selection auto-saves without explicit feedback.
  7. Trace panel   After every query, prints a Rich table: latency, cache hit, plan confidence,
                   agents invoked, tool calls, token counts, estimated cost, per-agent breakdown.

Slash commands:
  /ingest <path>   Ingest a .txt or .pdf file into the default tenant RAG store
  /docs            List all previously ingested document sources

Environment:
  ANTHROPIC_API_KEY   Required (unless USE_BEDROCK=true)
  USE_BEDROCK         Switch to AWS Bedrock backend (true | false)
  REDIS_URL           Optional Redis for L1 cache + session history
"""
import asyncio
import json
import os
import re
import sqlite3
import tempfile
import webbrowser

import anthropic
import pandas as pd
import plotly.express as px
from rich.columns import Columns
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

from agents import benchmark_agent, insight_agent, planner, semantic_agent
from bedrock_client import backend_label, default_model, make_client
from cost_ledger import CostLedger
from data.seed import DB_PATH, seed_database
from observability import ESTIMATED_PIPELINE_COST_USD, QueryTrace
from orchestrator import MCPOrchestrator
from rag.ingest import ingest_file, list_sources, query_documents
from rag.store import RAGStore
from redis_memory import RedisMemory
from security import check_ingest, check_query

console = Console()
MODEL = default_model()

EXAMPLE_QUERIES = [
    "Why did revenue drop in Q1 2024?",
    "Which regions are below target and what's driving it?",
    "What are our top 5 products by revenue in 2024?",
    "Compare customer segment performance — enterprise vs SMB",
    "Give me an executive summary of 2024 sales performance",
]


# ── Answer rendering ──────────────────────────────────────────────────────────

def _parse_md_table(lines: list[str]) -> Table | None:
    """Convert markdown table lines into a Rich Table. Returns None if invalid."""
    data_rows = [l for l in lines if not re.match(r"^\|[-:| ]+\|$", l.strip())]
    if len(data_rows) < 2:
        return None
    def cells(row): return [c.strip() for c in row.strip().strip("|").split("|")]
    headers = cells(data_rows[0])
    t = Table(show_header=True, header_style="bold cyan", box=None)
    for h in headers:
        t.add_column(h)
    for row in data_rows[1:]:
        t.add_row(*cells(row))
    return t


def render_answer(answer: str) -> None:
    """Render markdown answer; replace markdown tables with Rich Table widgets."""
    lines = answer.split("\n")
    text_buf, table_buf, in_table = [], [], False

    def flush_text():
        if text_buf:
            console.print(Markdown("\n".join(text_buf)))
            text_buf.clear()

    def flush_table():
        if table_buf:
            t = _parse_md_table(table_buf)
            if t:
                console.print(t)
            else:
                console.print(Markdown("\n".join(table_buf)))
            table_buf.clear()

    for line in lines:
        is_table_row = bool(re.match(r"^\|", line.strip()))
        if is_table_row:
            if not in_table:
                flush_text()
                in_table = True
            table_buf.append(line)
        else:
            if in_table:
                flush_table()
                in_table = False
            text_buf.append(line)

    flush_table() if in_table else flush_text()


# ── Chain-of-thought helpers ──────────────────────────────────────────────────

_FOLLOWUP_SYSTEM = (
    "You are a data analysis assistant. Given a business question and the answer that was just "
    "delivered, suggest exactly 3 short, distinct follow-up questions that would naturally deepen "
    "or broaden the analysis. Think like an analyst: if the answer was about quarterly revenue, "
    "suggest breakdowns by region, by product, YoY comparisons, or target gaps. "
    "Return ONLY a JSON array of 3 strings. No markdown, no explanation, no trailing text."
)

_VIZ_SYSTEM = """You are a SQL + visualization expert for a SQLite sales warehouse.

Schema:
  sales_fact(sale_id, date_id TEXT, product_id, customer_id, region_id,
             quantity, revenue REAL, cost REAL, gross_profit REAL)
  date_dim(date_id TEXT PK, year, quarter, month, month_name, week)
  region_dim(region_id PK, region_name, country, manager, target_revenue)
  product_dim(product_id PK, product_name, category, subcategory, unit_price, unit_cost)
  customer_dim(customer_id PK, customer_name, segment, country)

Given a business question and its answer, return ONLY a JSON object (no markdown) with:
  "sql":        a valid SQLite SELECT that retrieves the data to plot,
  "chart_type": one of "bar" | "line" | "pie" | "area",
  "x":          column name for x-axis / labels,
  "y":          column name for y-axis / values,
  "color":      column name for color grouping (or null),
  "title":      short descriptive chart title

Keep the SQL simple, readable, and always alias aggregates (e.g. SUM(revenue) AS revenue)."""


async def get_followups(
    client: anthropic.AsyncAnthropic,
    question: str,
    answer: str,
) -> list[str]:
    """Return 3 contextual follow-up questions based on the question+answer."""
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=300,
        system=_FOLLOWUP_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Question: {question}\n\nAnswer (summary):\n{answer[:800]}",
        }],
    )
    raw = resp.content[0].text.strip()
    raw = re.sub(r"```json?\n?|```", "", raw).strip()
    suggestions = json.loads(raw)
    return [s for s in suggestions if isinstance(s, str)][:3]


async def generate_viz(
    client: anthropic.AsyncAnthropic,
    question: str,
    answer: str,
) -> None:
    """Ask Claude to produce a SQL + chart spec, run it, and open in browser."""
    console.print("[dim]Generating visualization…[/dim]")

    resp = await client.messages.create(
        model=MODEL,
        max_tokens=600,
        system=_VIZ_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Question: {question}\n\nAnswer:\n{answer[:1000]}",
        }],
    )
    raw = resp.content[0].text.strip()
    raw = re.sub(r"```json?\n?|```", "", raw).strip()

    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as e:
        console.print(f"[red]Viz spec parse error:[/red] {e}\n{raw}")
        return

    try:
        conn = sqlite3.connect(DB_PATH)
        df = pd.read_sql_query(spec["sql"], conn)
        conn.close()
    except Exception as e:
        console.print(f"[red]SQL error:[/red] {e}")
        return

    if df.empty:
        console.print("[yellow]Query returned no data — skipping viz.[/yellow]")
        return

    x, y     = spec.get("x"), spec.get("y")
    color    = spec.get("color") or None
    title    = spec.get("title", question)
    ctype    = spec.get("chart_type", "bar")

    try:
        if ctype == "line":
            fig = px.line(df, x=x, y=y, color=color, title=title, markers=True)
        elif ctype == "pie":
            fig = px.pie(df, names=x, values=y, title=title)
        elif ctype == "area":
            fig = px.area(df, x=x, y=y, color=color, title=title)
        else:
            fig = px.bar(df, x=x, y=y, color=color, title=title, text_auto=".2s",
                         barmode="group" if color else "relative")

        fig.update_layout(
            template="plotly_dark",
            margin=dict(t=60, b=40),
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
        )

        with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w") as f:
            f.write(fig.to_html(include_plotlyjs="cdn"))
            tmp_path = f.name

        webbrowser.open(f"file://{tmp_path}")
        console.print(f"[green]Chart opened in browser.[/green] [dim]{tmp_path}[/dim]")

    except Exception as e:
        console.print(f"[red]Chart render error:[/red] {e}")


# ── Core pipeline ─────────────────────────────────────────────────────────────

async def process_query(
    client: anthropic.AsyncAnthropic,
    orchestrator: MCPOrchestrator,
    rag: RAGStore,
    question: str,
    conversation_history: list[dict],
    redis_mem: RedisMemory | None = None,
    tenant_id: str = "default",
) -> tuple[str, QueryTrace]:
    """Run the full pipeline. Returns (answer, trace). Does NOT store in RAG."""

    trace = QueryTrace(question=question)

    # ── 0. Redis L1 exact cache ───────────────────────────────────────────────
    console.print(Rule("[bold magenta]Cache[/bold magenta]", style="magenta"))
    if redis_mem and redis_mem.available:
        exact = await redis_mem.get_exact(tenant_id, question)
        if exact:
            trace.cache_hit        = True
            trace.avoided_cost_usd = ESTIMATED_PIPELINE_COST_USD
            console.print("  [bold cyan]Redis L1 hit[/bold cyan] — exact match returned instantly.")
            return f"*(From Redis L1 cache)*\n\n{exact}", trace
        console.print("  [dim]Redis L1 miss — checking ChromaDB L2…[/dim]")
    else:
        console.print("  [dim]Redis unavailable — checking ChromaDB L2…[/dim]")

    # ── 1. ChromaDB L2 semantic cache ─────────────────────────────────────────
    cached_answer, rag_context = rag.retrieve(question)

    # Enrich rag_context with document store
    doc_chunks = query_documents(question)
    if doc_chunks:
        rag_context += "\n\n" + "\n\n".join(
            f"[Ingested document — {c['source']}]\n{c['chunk']}" for c in doc_chunks
        )

    if cached_answer:
        trace.cache_hit        = True
        trace.avoided_cost_usd = ESTIMATED_PIPELINE_COST_USD
        console.print("  [bold green]Cache hit[/bold green] — returning stored answer instantly.")
        return f"*(From semantic cache)*\n\n{cached_answer}", trace

    chunk_count = rag_context.count("[")
    label = f"{chunk_count} context chunk(s)" if chunk_count else "no prior context"
    console.print(f"  [dim]Cache miss · {label} injected.[/dim]")

    # ── 2. Plan ───────────────────────────────────────────────────────────────
    console.print(Rule("[bold blue]Planner Agent[/bold blue]", style="blue"))
    plan = await planner.create_plan(
        client, question,
        rag_context=rag_context,
        conversation_history=conversation_history,
        trace=trace,
    )
    trace.agents_invoked = plan["agents"]
    console.print(f"  Routing to : [yellow]{', '.join(plan['agents'])}[/yellow]")
    console.print(f"  Goal       : [dim]{plan.get('synthesis_goal', '')}[/dim]")
    console.print(f"  Reasoning  : [dim]{plan.get('reasoning', '')}[/dim]")

    # ── 3. Specialized agents ─────────────────────────────────────────────────
    console.print(Rule("[bold blue]Specialized Agents[/bold blue]", style="blue"))

    async def tracked(label: str, coro):
        console.print(f"  ▶ [bold]{label}[/bold] starting…")
        result = await coro
        console.print(f"  [green]✓[/green] [bold]{label}[/bold] done")
        return result

    tasks_map  = plan.get("tasks", {})
    sub_traces: dict[str, QueryTrace] = {}
    coros: dict[str, asyncio.coroutines] = {}

    for name in ("semantic", "benchmark"):
        if name in plan["agents"]:
            label    = name.title()
            sub_traces[name] = QueryTrace(question=tasks_map.get(name, question))
            agent_fn = semantic_agent if name == "semantic" else benchmark_agent
            coros[name] = tracked(
                f"{label} Agent",
                agent_fn.run(client, orchestrator, tasks_map.get(name, question), sub_traces[name]),
            )

    parallel_results: dict[str, str] = {}
    if coros:
        gathered = await asyncio.gather(*coros.values())
        for name, result in zip(coros.keys(), gathered):
            parallel_results[name] = result
            trace.merge_agent_trace(name, sub_traces[name])

    if "insight" in plan["agents"]:
        context_blob = "\n\n".join(
            f"[{k.upper()} AGENT]\n{v}" for k, v in parallel_results.items()
        )
        console.print("  ▶ [bold]Insight Agent[/bold] starting…")
        insight_sub = QueryTrace(question=tasks_map.get("insight", question))
        insight_result = await insight_agent.run(
            client, orchestrator,
            tasks_map.get("insight", question),
            context=context_blob,
            trace=insight_sub,
        )
        if len(insight_result) < 120 or insight_result.startswith("[Agent error"):
            console.print("  [yellow]⚠ Insight result thin — retrying with broader scope[/yellow]")
            broader = (
                f"The previous query returned insufficient data. "
                f"Try a broader analysis for: {tasks_map.get('insight', question)}. "
                f"Use wider date ranges or remove restrictive filters."
            )
            insight_result = await insight_agent.run(
                client, orchestrator, broader, context=context_blob, trace=insight_sub
            )
        console.print("  [green]✓[/green] [bold]Insight Agent[/bold] done")
        parallel_results["insight"] = insight_result
        trace.merge_agent_trace("insight", insight_sub)

    # ── 4. Streaming synthesis ────────────────────────────────────────────────
    console.print(Rule("[bold blue]Synthesis[/bold blue]", style="blue"))

    agent_summaries = "\n\n".join(
        f"## {name.title()} Agent\n{result}" for name, result in parallel_results.items()
    )
    synthesis_content = (
        f"User question: {question}\n\n"
        f"Agent findings:\n{agent_summaries}\n\n"
    )
    if rag_context:
        synthesis_content += f"Memory context:\n{rag_context}\n\n"
    synthesis_content += (
        "Synthesize these findings into a clear, direct answer for a business audience. "
        "Lead with the key insight, support with numbers, use markdown headers and bullet points. "
        "Include a data table where it helps clarity."
    )

    answer_buf = ""
    with Live(Panel(Markdown(""), title="[bold]Answer[/bold]", border_style="green", padding=(1, 2)),
              console=console, refresh_per_second=12) as live:
        async with client.messages.stream(
            model=MODEL,
            max_tokens=2048,
            system=(
                "You are a senior data analyst presenting findings to a business audience. "
                "Lead with the key insight, support every claim with specific numbers, "
                "use markdown headers and bullet points, and include a data table where it aids clarity. "
                "Be direct — avoid filler phrases."
            ),
            messages=[{"role": "user", "content": synthesis_content}],
        ) as stream:
            async for text in stream.text_stream:
                answer_buf += text
                live.update(Panel(Markdown(answer_buf), title="[bold]Answer[/bold]",
                                  border_style="green", padding=(1, 2)))
            final_msg = await stream.get_final_message()
            trace.record_usage(final_msg)

    return answer_buf, trace


# ── CLI entry point ───────────────────────────────────────────────────────────

async def main():
    console.print(Panel.fit(
        "[bold white]MCP Data Agents[/bold white]\n"
        "[dim]Multi-agent analytics · RAG · Semantic Cache · Streaming[/dim]",
        style="blue", padding=(1, 4),
    ))

    if not os.path.exists(DB_PATH):
        console.print("[yellow]First run — seeding database…[/yellow]")
        seed_database()

    from bedrock_client import USE_BEDROCK
    if not USE_BEDROCK:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            console.print("[red bold]Error:[/red bold] ANTHROPIC_API_KEY is not set.")
            console.print("  export ANTHROPIC_API_KEY=sk-ant-…")
            return

    client = make_client()
    console.print(f"[dim]LLM backend: {backend_label()}[/dim]")

    console.print("[dim]Initializing RAG store…[/dim]")
    rag = RAGStore(tenant_id="default")
    rag.seed_domain()
    s = rag.stats()
    console.print(f"[green]RAG ready.[/green] [dim]{s['domain_docs']} domain docs · {s['qa_entries']} cached Q&As[/dim]")

    ledger = CostLedger()

    console.print("[dim]Starting MCP servers…[/dim]")
    orchestrator = MCPOrchestrator()
    await orchestrator.start()
    console.print(f"[green]MCP servers ready.[/green] [dim]{list(orchestrator.sessions)}[/dim]\n")

    console.print("[bold]Try asking:[/bold]")
    for q in EXAMPLE_QUERIES:
        console.print(f"  [dim]• {q}[/dim]")
    console.print("[dim]  /ingest <path>  — add a document to RAG[/dim]")
    console.print("[dim]  /docs           — list ingested documents[/dim]")
    console.print()

    console.print("[dim]Connecting to Redis…[/dim]")
    redis_mem = RedisMemory()
    redis_ok  = await redis_mem.connect()
    if redis_ok:
        redis_history = await redis_mem.get_history("default")
        conversation_history: list[dict] = redis_history or rag.load_history()
        console.print(f"[green]Redis ready.[/green] [dim]L1 cache · session history · rate limiting[/dim]")
    else:
        conversation_history: list[dict] = rag.load_history()
        console.print("[yellow]Redis unavailable — using local JSON history.[/yellow]")

    pending_question: str | None = None

    try:
        while True:
            if pending_question:
                user_input = pending_question
                pending_question = None
                console.print(f"\n[bold green]You ▶[/bold green] [italic]{user_input}[/italic]")
            else:
                try:
                    user_input = console.input("[bold green]You ▶[/bold green] ").strip()
                except (EOFError, KeyboardInterrupt):
                    break

            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit", "q", ":q"):
                break

            # ── Slash commands ────────────────────────────────────────────────
            if user_input.startswith("/ingest "):
                path = user_input[8:].strip()
                try:
                    import pathlib
                    raw = pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
                    sec = check_ingest(raw, source=path)
                    if not sec:
                        console.print(f"[red]Ingest blocked:[/red] {'; '.join(sec.violations)}")
                    else:
                        n = ingest_file(path)
                        console.print(f"[green]Ingested {n} chunks from {path}[/green]")
                except Exception as e:
                    console.print(f"[red]Ingest error:[/red] {e}")
                continue

            if user_input == "/docs":
                sources = list_sources()
                if sources:
                    for s in sources:
                        console.print(f"  [dim]• {s['source']}[/dim]")
                else:
                    console.print("[dim]No documents ingested yet.[/dim]")
                continue

            # ── Query pipeline ────────────────────────────────────────────────
            sec = check_query(user_input)
            if not sec:
                console.print(f"[red]Query blocked:[/red] {'; '.join(sec.violations)}")
                console.print()
                continue

            try:
                answer, trace = await process_query(
                    client, orchestrator, rag, user_input, conversation_history,
                    redis_mem=redis_mem, tenant_id="default",
                )

                if not trace.cache_hit:
                    console.print()
                    render_answer(answer)

                # ── Follow-up suggestions ─────────────────────────────────────
                followups: list[str] = []
                if not trace.cache_hit:
                    try:
                        followups = await get_followups(client, user_input, answer)
                    except Exception:
                        pass

                if followups:
                    console.print()
                    console.print(Rule("[dim]Continue the analysis[/dim]", style="dim"))
                    for i, suggestion in enumerate(followups, 1):
                        console.print(f"  [cyan][{i}][/cyan] {suggestion}")
                    console.print(f"  [cyan][{len(followups) + 1}][/cyan] 📊 Visualize this")

                # ── Feedback + next step prompt ───────────────────────────────
                console.print()
                viz_opt = len(followups) + 1
                if followups:
                    prompt_hint = f"[dim]Rate [g/b/s]  or next step [1-{viz_opt}]: [/dim]"
                else:
                    prompt_hint = "[dim]Rate this answer — [g]ood / [b]ad / [s]kip: [/dim]"

                feedback_raw = console.input(prompt_hint).strip().lower()

                # Handle numeric follow-up selection
                if followups and feedback_raw.isdigit():
                    idx = int(feedback_raw)
                    # Auto-save without explicit feedback
                    trace.feedback = None
                    if not trace.cache_hit:
                        rag.store_qa(user_input, answer, trace.agents_invoked)
                    conversation_history.append({"question": user_input, "answer": answer})
                    rag.save_history(conversation_history)

                    if 1 <= idx <= len(followups):
                        pending_question = followups[idx - 1]
                    elif idx == viz_opt:
                        await generate_viz(client, user_input, answer)
                    else:
                        console.print("[dim]Invalid selection.[/dim]")

                elif feedback_raw in ("b", "bad"):
                    trace.feedback = "bad"
                    rag.flag_bad(user_input)
                    if redis_mem:
                        await redis_mem.invalidate_exact("default", user_input)
                    console.print("[yellow]Noted — answer removed from cache.[/yellow]")
                elif feedback_raw in ("g", "good"):
                    trace.feedback = "good"
                    rag.store_qa(user_input, answer, trace.agents_invoked)
                    if redis_mem:
                        await redis_mem.set_exact("default", user_input, answer, trace.agents_invoked)
                        await redis_mem.append_history("default", user_input, answer)
                    conversation_history.append({"question": user_input, "answer": answer})
                    rag.save_history(conversation_history)
                    console.print("[green]Saved to memory.[/green]")
                else:
                    trace.feedback = None
                    if not trace.cache_hit:
                        rag.store_qa(user_input, answer, trace.agents_invoked)
                        if redis_mem:
                            await redis_mem.set_exact("default", user_input, answer, trace.agents_invoked)
                    if redis_mem:
                        await redis_mem.append_history("default", user_input, answer)
                    conversation_history.append({"question": user_input, "answer": answer})
                    rag.save_history(conversation_history)

                # ── Persist trace to cost ledger ──────────────────────────────
                ledger.record(trace)

                # ── Observability summary ─────────────────────────────────────
                console.print()
                console.print(Rule("[dim]Trace[/dim]", style="dim"))
                trace.print_summary()

            except Exception as exc:
                console.print(f"[red]Error:[/red] {exc}")

            console.print()

    finally:
        await orchestrator.stop()
        await redis_mem.close()
        console.print("[dim]Goodbye.[/dim]")


if __name__ == "__main__":
    asyncio.run(main())

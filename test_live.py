"""
Live end-to-end test of the chain-of-thought agent flow.

Run from a terminal where ANTHROPIC_API_KEY is set:
  python test_live.py

It will:
  1. Ask "What is the quarterly revenue?"
  2. Stream the answer
  3. Show 3 AI-generated follow-up suggestions
  4. Auto-pick suggestion #1 and run it
  5. Generate and open a viz for the second answer
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
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule

from data.seed import DB_PATH, seed_database
from observability import ESTIMATED_PIPELINE_COST_USD, QueryTrace
from orchestrator import MCPOrchestrator
from agents import benchmark_agent, insight_agent, planner, semantic_agent
from rag.store import RAGStore
from main import (
    process_query,
    get_followups,
    generate_viz,
    render_answer,
    MODEL,
)

console = Console()

TEST_QUESTIONS = [
    "What is the quarterly revenue for 2023 and 2024?",
    "Which regions are below their revenue target?",
]


async def run_live_test():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        console.print("[red bold]Error:[/red bold] ANTHROPIC_API_KEY is not set.")
        console.print("  export ANTHROPIC_API_KEY=sk-ant-…")
        return

    console.print(Panel.fit(
        "[bold white]MCP Data Agents — Live Chain-of-Thought Test[/bold white]\n"
        "[dim]Auto-runs 2 queries → shows follow-ups → generates viz[/dim]",
        style="blue", padding=(1, 4),
    ))

    if not os.path.exists(DB_PATH):
        console.print("[yellow]Seeding database…[/yellow]")
        seed_database()

    client = anthropic.AsyncAnthropic(api_key=api_key)

    console.print("[dim]Initializing RAG store…[/dim]")
    rag = RAGStore(tenant_id="default")
    rag.seed_domain()

    console.print("[dim]Starting MCP servers…[/dim]")
    orchestrator = MCPOrchestrator()
    await orchestrator.start()
    console.print(f"[green]MCP servers ready.[/green] {list(orchestrator.sessions)}\n")

    conversation_history = rag.load_history()

    try:
        for round_num, question in enumerate(TEST_QUESTIONS, 1):
            console.print(Rule(f"[bold cyan]Round {round_num}[/bold cyan]", style="cyan"))
            console.print(f"[bold green]Query ▶[/bold green] {question}\n")

            answer, trace = await process_query(
                client, orchestrator, rag, question, conversation_history
            )

            console.print()
            render_answer(answer)
            conversation_history.append({"question": question, "answer": answer})

            # ── Follow-up suggestions ──────────────────────────────────────────
            console.print()
            console.print(Rule("[dim]Continue the analysis[/dim]", style="dim"))
            console.print("[dim]Generating follow-up suggestions…[/dim]")

            followups = await get_followups(client, question, answer)

            for i, s in enumerate(followups, 1):
                console.print(f"  [cyan][{i}][/cyan] {s}")
            console.print(f"  [cyan][{len(followups) + 1}][/cyan] 📊 Visualize this")

            if round_num == 1 and followups:
                # Auto-pick suggestion 1 for round 1
                chosen = followups[0]
                console.print(f"\n[dim]→ Auto-selecting [1]: {chosen}[/dim]")
                # Queue it as next question
                TEST_QUESTIONS.insert(round_num, chosen)

            elif round_num >= 2:
                # On the last round, auto-generate a viz
                console.print(f"\n[dim]→ Auto-selecting 📊 Visualize…[/dim]")
                await generate_viz(client, question, answer)

    finally:
        await orchestrator.stop()
        console.print("\n[dim]Test complete.[/dim]")


if __name__ == "__main__":
    asyncio.run(run_live_test())

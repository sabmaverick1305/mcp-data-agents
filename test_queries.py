"""Batch test runner — runs all three example queries and prints results."""
import asyncio
import os

import anthropic
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule

from data.seed import seed_database, DB_PATH
from orchestrator import MCPOrchestrator
from main import process_query

console = Console()

QUERIES = [
    ("All 3 agents",        "Why did revenue drop in Q1 2024?"),
    ("Benchmark + Insight", "Which regions are below target?"),
    ("Semantic only",       "What is our gross margin?"),
]


async def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        console.print("[red bold]Error:[/red bold] Set ANTHROPIC_API_KEY before running.")
        console.print("  export ANTHROPIC_API_KEY=sk-ant-...")
        return

    if not os.path.exists(DB_PATH):
        console.print("[yellow]Seeding database…[/yellow]")
        seed_database()

    client = anthropic.Anthropic(api_key=api_key)
    orchestrator = MCPOrchestrator()

    console.print("[dim]Starting MCP servers…[/dim]")
    await orchestrator.start()
    console.print("[green]Servers ready.[/green]\n")

    for label, query in QUERIES:
        console.print(Rule(f"[bold cyan]{label}[/bold cyan]  —  [italic]{query}[/italic]"))
        try:
            answer = await process_query(client, orchestrator, query)
            console.print(Panel(Markdown(answer), border_style="green", padding=(1, 2)))
        except Exception as exc:
            console.print(f"[red]Error:[/red] {exc}")
        console.print()

    await orchestrator.stop()


if __name__ == "__main__":
    asyncio.run(main())

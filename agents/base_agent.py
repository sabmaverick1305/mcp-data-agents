"""Shared async agent loop: Claude + MCP tools, retry, error handling, tracing."""
import asyncio
import json

import anthropic

from observability import QueryTrace
from orchestrator import MCPOrchestrator
from security import check_tool_call

MODEL = "claude-sonnet-4-6"
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0   # seconds; doubles each retry


async def run_agent_loop(
    client: anthropic.AsyncAnthropic,
    orchestrator: MCPOrchestrator,
    server_names: list[str],
    system_prompt: str,
    question: str,
    trace: QueryTrace | None = None,
) -> str:
    tools = orchestrator.get_tools_for(server_names)
    messages = [{"role": "user", "content": question}]

    for attempt in range(MAX_RETRIES):
        try:
            return await _loop(client, orchestrator, tools, system_prompt, messages, trace)
        except anthropic.RateLimitError:
            if attempt == MAX_RETRIES - 1:
                return "[Agent error: rate limit reached after retries]"
            await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))
        except anthropic.APIStatusError as e:
            if e.status_code >= 500 and attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                continue
            return f"[Agent error: {e.message}]"
        except Exception as e:
            return f"[Agent error: {e}]"

    return "[Agent error: max retries exceeded]"


async def _loop(
    client: anthropic.AsyncAnthropic,
    orchestrator: MCPOrchestrator,
    tools: list[dict],
    system_prompt: str,
    messages: list[dict],
    trace: QueryTrace | None,
) -> str:
    while True:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=system_prompt,
            tools=tools,
            messages=messages,
        )

        if trace:
            trace.record_usage(response)

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        text_parts = [b.text for b in response.content if b.type == "text"]

        if response.stop_reason == "end_turn" or not tool_uses:
            return "\n".join(text_parts) or "(no response)"

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for tu in tool_uses:
            security = check_tool_call(tu.name, tu.input)
            if not security:
                result_text = json.dumps({"error": f"Tool call blocked: {'; '.join(security.violations)}"})
            else:
                if trace:
                    trace.record_tool(tu.name)
                result_text = await orchestrator.call_tool(tu.name, tu.input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": result_text,
            })
        messages.append({"role": "user", "content": tool_results})

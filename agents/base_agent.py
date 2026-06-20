"""
Shared async agent execution loop — the common runtime for all specialist agents.

Every agent in the system (semantic, benchmark, insight) delegates to run_agent_loop()
defined here. This avoids duplicating retry logic, tool-call dispatch, error handling,
and token tracing across three separate agent modules.

Execution flow (single attempt):
  1. Fetch Anthropic-format tool definitions from the orchestrator for the given servers.
  2. Call Claude with the agent's system prompt + user question + tool list.
  3. If stop_reason == "end_turn" or no tool_use blocks → return the text response.
  4. For each tool_use block:
       a. Run check_tool_call() (security allowlist) — block if not permitted.
       b. Dispatch to orchestrator.call_tool() — returns JSON string.
  5. Append tool results to the message list and loop back to step 2.

Retry policy:
  MAX_RETRIES = 3, exponential back-off (1s → 2s → 4s).
  Retries on: RateLimitError, 5xx APIStatusError.
  Non-retryable errors (4xx, unexpected exceptions) return an error string immediately.

Return value:
  Always a string — either the model's final text response or an "[Agent error: ...]"
  sentinel. Callers (api.py, main.py, eval/runner.py) handle the sentinel gracefully.

Security integration:
  check_tool_call() is called before every MCP dispatch. Blocked calls return a
  JSON error object that the model sees as a tool result, allowing it to report
  the failure rather than silently skipping the tool.
"""
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


def _cached_system(system_prompt: str) -> list[dict]:
    """Wrap a static system prompt string as a cached Anthropic content block.

    Anthropic caches everything up to and including the block marked
    cache_control=ephemeral, so identical system prompts across calls within
    the 5-minute window are never re-encoded, cutting input token costs.
    """
    return [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]


async def _loop(
    client: anthropic.AsyncAnthropic,
    orchestrator: MCPOrchestrator,
    tools: list[dict],
    system_prompt: str,
    messages: list[dict],
    trace: QueryTrace | None,
) -> str:
    system_blocks = _cached_system(system_prompt)
    while True:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=system_blocks,
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

"""
MCP Orchestrator — lifecycle manager and dispatcher for all MCP server processes.

Responsibilities:
  - Spawning the three MCP server scripts (snowflake, powerbi, tableau) as child
    processes over stdio transport using the official MCP Python SDK.
  - Maintaining one ClientSession per server inside a shared AsyncExitStack so all
    sessions are torn down cleanly on application shutdown.
  - Providing get_tools_for(server_names) — returns Anthropic-format tool definitions
    from one or more servers for injection into agent system prompts.
  - Routing call_tool(prefixed_name, arguments) — parses the "server__tool" prefix,
    acquires a per-server asyncio.Lock (MCP sessions are not thread-safe), dispatches
    the call with a configurable timeout, and returns a JSON string result.

Fail-soft design:
  If a server fails to start (import error, missing dependency, port conflict),
  the orchestrator logs a warning and continues — the other servers remain available
  and affected agents fall back to their error-handling paths.

Tool naming convention:
  All tools exposed to Claude use the format "{server_name}__{tool_name}"
  (double underscore separator) so the dispatcher can route without ambiguity.
  Example: "snowflake__run_sql_query", "powerbi__get_metric"

Constants:
  TOOL_TIMEOUT   15 seconds per individual tool call (configurable via the constant)
"""
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack

from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters

SERVERS = {
    "snowflake": os.path.join(os.path.dirname(__file__), "servers", "snowflake_server.py"),
    "powerbi":   os.path.join(os.path.dirname(__file__), "servers", "powerbi_server.py"),
    "tableau":   os.path.join(os.path.dirname(__file__), "servers", "tableau_server.py"),
}

TOOL_TIMEOUT = 15   # seconds per tool call


class MCPOrchestrator:
    def __init__(self):
        self.sessions: dict[str, ClientSession] = {}
        self.tools: dict[str, list] = {}
        self._stack = AsyncExitStack()
        self._locks: dict[str, asyncio.Lock] = {}

    async def start(self):
        await self._stack.__aenter__()
        failed = []
        for name, script in SERVERS.items():
            try:
                params = StdioServerParameters(command=sys.executable, args=[script])
                read, write = await self._stack.enter_async_context(stdio_client(params))
                session = await self._stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                result = await session.list_tools()
                self.tools[name] = result.tools
                self.sessions[name] = session
                self._locks[name] = asyncio.Lock()
            except Exception as e:
                failed.append(f"{name}: {e}")

        if failed:
            print(f"[Orchestrator] Warning — servers failed to start: {failed}")

    async def stop(self):
        await self._stack.__aexit__(None, None, None)

    def get_tools_for(self, server_names: list[str]) -> list[dict]:
        """Return Anthropic-compatible tool defs for the given servers."""
        return [
            {
                "name": f"{server}__{tool.name}",
                "description": f"[{server.upper()} MCP] {tool.description or ''}",
                "input_schema": tool.inputSchema,
            }
            for server in server_names
            for tool in self.tools.get(server, [])
        ]

    async def call_tool(self, prefixed_name: str, arguments: dict) -> str:
        """Call server__tool_name with a per-call timeout. Returns JSON string."""
        parts = prefixed_name.split("__", 1)
        if len(parts) != 2:
            return json.dumps({"error": f"Invalid tool name: {prefixed_name}"})

        server_name, tool_name = parts
        if server_name not in self.sessions:
            return json.dumps({"error": f"Server '{server_name}' unavailable"})

        try:
            async with self._locks[server_name]:
                result = await asyncio.wait_for(
                    self.sessions[server_name].call_tool(tool_name, arguments),
                    timeout=TOOL_TIMEOUT,
                )
        except asyncio.TimeoutError:
            return json.dumps({"error": f"Tool '{prefixed_name}' timed out after {TOOL_TIMEOUT}s"})
        except Exception as e:
            return json.dumps({"error": str(e)})

        if result.content:
            block = result.content[0]
            return block.text if block.type == "text" else str(block)
        return json.dumps({"result": None})

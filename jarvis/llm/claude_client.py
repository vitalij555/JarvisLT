"""LLM client using OpenAI API with tool use loop and MCP server support."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import OpenAI

from jarvis.connectors.home_assistant import HomeAssistantConnector, HA_TOOLS
from jarvis.llm.memory import ConversationMemory

logger = logging.getLogger(__name__)


class LLMClient:
    def __init__(
        self,
        model: str,
        system_prompt: str,
        max_tokens: int = 1024,
        mcp_servers: dict[str, Any] | None = None,
        ha_connector: HomeAssistantConnector | None = None,
    ) -> None:
        self.model = model
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens
        self.ha_connector = ha_connector
        self._mcp_configs = mcp_servers or {}
        self._client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

        # Populated by start()
        self._mcp_tool_map: dict[str, ClientSession] = {}  # tool_name -> session
        self._mcp_tools: list[dict[str, Any]] = []  # OpenAI-format tool schemas
        self._exit_stack = AsyncExitStack()

    @staticmethod
    def _sanitize_schema(schema: Any) -> dict[str, Any]:
        """Coerce MCP tool inputSchema into a valid OpenAI parameters object."""
        if not isinstance(schema, dict):
            return {"type": "object", "properties": {}}
        result: dict[str, Any] = {}
        for k, v in schema.items():
            if k == "additionalProperties" and v is False:
                continue  # OpenAI rejects additionalProperties: false
            if k == "properties" and isinstance(v, dict):
                # Strip invalid per-property keys like "required": false
                cleaned: dict[str, Any] = {}
                for prop_name, prop_schema in v.items():
                    if isinstance(prop_schema, dict):
                        cleaned[prop_name] = {
                            pk: pv for pk, pv in prop_schema.items()
                            if not (pk == "required" and not isinstance(pv, list))
                        }
                    else:
                        cleaned[prop_name] = {}
                result[k] = cleaned
            else:
                result[k] = v
        return result

    async def start(self) -> None:
        """Launch all configured MCP servers and discover their tools."""
        await self._exit_stack.__aenter__()
        for name, cfg in self._mcp_configs.items():
            if not isinstance(cfg, dict) or "command" not in cfg:
                logger.warning("MCP server '%s' has invalid config, skipping", name)
                continue
            env = {**os.environ, **cfg.get("env", {})}
            params = StdioServerParameters(
                command=cfg["command"],
                args=cfg.get("args", []),
                env=env,
            )
            try:
                read, write = await self._exit_stack.enter_async_context(stdio_client(params))
                session: ClientSession = await self._exit_stack.enter_async_context(
                    ClientSession(read, write)
                )
                await session.initialize()
                tools_result = await session.list_tools()
                for tool in tools_result.tools:
                    self._mcp_tool_map[tool.name] = session
                    self._mcp_tools.append({
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description or "",
                            "parameters": self._sanitize_schema(tool.inputSchema),
                        },
                    })
                logger.info("MCP server '%s' ready — %d tools: %s",
                            name, len(tools_result.tools),
                            [t.name for t in tools_result.tools])
            except Exception as exc:
                logger.error("Failed to start MCP server '%s': %s", name, exc)

    async def stop(self) -> None:
        """Shut down all MCP servers."""
        await self._exit_stack.aclose()

    def _build_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        if self.ha_connector:
            for tool in HA_TOOLS:
                tools.append({
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool["description"],
                        "parameters": tool["input_schema"],
                    },
                })
        tools.extend(self._mcp_tools)
        return tools

    def _handle_local_tool(self, tool_name: str, tool_input: dict[str, Any]) -> Any:
        if self.ha_connector and tool_name.startswith("ha_"):
            return self.ha_connector.handle_tool_call(tool_name, tool_input)
        raise ValueError(f"No handler for tool: {tool_name}")

    async def _call_mcp_tool(self, tool_name: str, tool_input: dict[str, Any]) -> Any:
        session = self._mcp_tool_map[tool_name]
        result = await session.call_tool(tool_name, tool_input)
        if result.isError:
            return {"error": str(result.content)}
        parts = []
        for item in result.content:
            if hasattr(item, "text"):
                parts.append(item.text)
            else:
                parts.append(str(item))
        return "\n".join(parts) if parts else ""

    async def chat_async(self, user_text: str, memory: ConversationMemory) -> str:
        """Send a message to the LLM, handle tool use loop, return final text."""
        messages = [{"role": "system", "content": self.system_prompt}]
        messages.extend(memory.get_context())
        messages.append({"role": "user", "content": user_text})

        tools = self._build_tools()
        loop = asyncio.get_running_loop()

        while True:
            kwargs: dict[str, Any] = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": messages,
            }
            if tools:
                kwargs["tools"] = tools

            response = await loop.run_in_executor(
                None, lambda: self._client.chat.completions.create(**kwargs)
            )
            message = response.choices[0].message
            finish_reason = response.choices[0].finish_reason
            logger.debug("LLM finish_reason: %s", finish_reason)

            if finish_reason == "tool_calls" and message.tool_calls:
                messages.append(message)

                for tool_call in message.tool_calls:
                    tool_name = tool_call.function.name
                    tool_input = json.loads(tool_call.function.arguments)
                    logger.info("Tool call: %s(%s)", tool_name, json.dumps(tool_input)[:120])
                    try:
                        if tool_name in self._mcp_tool_map:
                            result = await self._call_mcp_tool(tool_name, tool_input)
                        else:
                            result = self._handle_local_tool(tool_name, tool_input)
                    except Exception as exc:
                        result = {"error": str(exc)}
                    logger.info("Tool result: %s", str(result)[:200])
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(result) if not isinstance(result, str) else result,
                    })
                continue

            return (message.content or "").strip()

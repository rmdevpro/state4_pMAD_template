"""MAD client — talks to any State 4 MAD via standard endpoints.

Uses:
  - /v1/chat/completions (OpenAI-compatible) for chat
  - /mcp (MCP) for conversation management, logs, context info
  - /health for health status
"""

import json
import logging
from typing import AsyncGenerator

import httpx

_log = logging.getLogger("ui.mad_client")


class MADClient:
    """Client for a single State 4 MAD."""

    def __init__(self, name: str, base_url: str, hostname: str = ""):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.hostname = hostname or name

    # ── Health ──────────────────────────────────────────────────────

    async def health(self) -> dict:
        """Check MAD health."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{self.base_url}/health", timeout=5)
                return resp.json()
        except (httpx.HTTPError, ValueError):
            return {"status": "unreachable"}

    # ── Conversations ───────────────────────────────────────────────

    async def list_conversations(self) -> list[dict]:
        """List conversations where this MAD is a participant."""
        args = {"participant": self.hostname, "limit": 50}
        result = await self._mcp_call("conv_list_conversations", args)
        return result.get("conversations", [])

    async def create_conversation(self, title: str) -> dict:
        """Create a new conversation."""
        return await self._mcp_call("conv_create_conversation", {"title": title})

    async def get_history(self, conversation_id: str) -> list[dict]:
        """Get message history for a conversation."""
        try:
            result = await self._mcp_call(
                "conv_get_history",
                {"conversation_id": conversation_id, "limit": 100},
            )
            return result.get("messages", [])
        except (ValueError, RuntimeError):
            return []

    async def delete_conversation(self, conversation_id: str) -> bool:
        """Delete a conversation. Returns True on success."""
        try:
            await self._mcp_call(
                "conv_delete_conversation",
                {"conversation_id": conversation_id},
            )
            return True
        except (ValueError, RuntimeError):
            return False

    # ── Context Info ────────────────────────────────────────────────

    async def get_context_info(self, conversation_id: str) -> dict:
        """Get context window info for a conversation."""
        try:
            result = await self._mcp_call(
                "conv_search_context_windows",
                {"conversation_id": conversation_id, "limit": 5},
            )
            return result
        except (ValueError, RuntimeError):
            return {}

    # ── Logs ────────────────────────────────────────────────────────

    async def query_logs(self, limit: int = 30) -> list[dict]:
        """Query recent logs."""
        result = await self._mcp_call("query_logs", {"limit": limit})
        return result.get("entries", [])

    # ── Chat ────────────────────────────────────────────────────────

    async def chat_stream(
        self,
        messages: list[dict],
        conversation_id: str | None = None,
        user: str = "gradio-ui",
    ) -> AsyncGenerator[str, None]:
        """Stream chat completions from the Imperator."""
        payload = {
            "model": "imperator",
            "messages": messages,
            "stream": True,
            "user": user,
        }
        if conversation_id:
            payload["conversation_id"] = conversation_id

        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                timeout=120,
            ) as resp:
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            yield content
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue

    # ── MCP ─────────────────────────────────────────────────────────

    async def _mcp_call(self, tool_name: str, arguments: dict) -> dict:
        """Call an MCP tool."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}/mcp", json=payload, timeout=60)
            resp.raise_for_status()
            body = resp.json()
            if "error" in body:
                raise RuntimeError(f"MCP error: {body['error']}")
            text = body.get("result", {}).get("content", [{}])[0].get("text", "{}")
            return json.loads(text)

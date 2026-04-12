"""
Tool dispatch — routes MCP tool calls to compiled StateGraph flows.

All tool logic lives in StateGraph flows loaded dynamically from AE/TE
packages via entry_points (REQ-001 §10). This module is the thin
kernel-side routing layer that maps tool names to their flows using
the stategraph_registry.
"""

import logging
import time
from typing import Any

from app.metrics_registry import MCP_REQUESTS, MCP_REQUEST_DURATION
from app.stategraph_registry import get_flow_builder, get_imperator_builder
from app.models import (
    ImperatorChatInput,
    MetricsGetInput,
)

_log = logging.getLogger("pmad_template.flows.tool_dispatch")

# Lazy-initialized flow singletons — compiled on first use from
# dynamically loaded packages via the stategraph_registry.
_flow_cache: dict[str, Any] = {}


def _get_flow(name: str) -> Any:
    """Get a compiled flow by registry name (lazy singleton)."""
    if name not in _flow_cache:
        builder = get_flow_builder(name)
        if builder is None:
            raise RuntimeError(
                f"Flow '{name}' not available. Is the AE package installed?"
            )
        _flow_cache[name] = builder()
    return _flow_cache[name]


def _get_imperator_flow() -> Any:
    """Get the compiled Imperator flow from the TE registry."""
    if "imperator" not in _flow_cache:
        builder = get_imperator_builder()
        if builder is None:
            raise RuntimeError(
                "No TE package registered. Install a TE package with "
                "install_stategraph or ensure one is installed at startup."
            )
        _flow_cache["imperator"] = builder()
    return _flow_cache["imperator"]


def invalidate_flow_cache() -> None:
    """Clear all cached flows. Called after install_stategraph()."""
    _flow_cache.clear()
    _log.info("Flow dispatch cache cleared")


async def dispatch_tool(
    tool_name: str,
    arguments: dict[str, Any],
    config: dict[str, Any],
    app_state: Any,
) -> dict[str, Any]:
    """Route a tool call to its StateGraph flow.

    Validates inputs using Pydantic models before invoking flows.
    Raises ValueError for unknown tools or validation errors.
    """
    _log.info("Dispatching tool: %s", tool_name)
    _start_time = time.monotonic()
    _status = "error"

    try:
        result = await _dispatch_tool_inner(tool_name, arguments, config, app_state)
        _status = "success"
        return result
    finally:
        _duration = time.monotonic() - _start_time
        MCP_REQUESTS.labels(tool=tool_name, status=_status).inc()
        MCP_REQUEST_DURATION.labels(tool=tool_name).observe(_duration)


async def _dispatch_tool_inner(
    tool_name: str,
    arguments: dict[str, Any],
    config: dict[str, Any],
    app_state: Any,
) -> dict[str, Any]:
    """Inner dispatch — routes tool calls to their StateGraph flows."""

    if tool_name == "imperator_chat":
        validated = ImperatorChatInput(**arguments)
        from langchain_core.messages import HumanMessage
        import uuid as _uuid

        thread_id = str(_uuid.uuid4())

        result = await _get_imperator_flow().ainvoke(
            {
                "messages": [HumanMessage(content=validated.message)],
                "context_window_id": (
                    str(validated.context_window_id)
                    if validated.context_window_id
                    else None
                ),
                "config": config,
                "response_text": None,
                "error": None,
                "iteration_count": 0,
            },
            config={"configurable": {"thread_id": thread_id}},
        )
        if result.get("error"):
            raise ValueError(result["error"])
        return {
            "response": result.get("response_text", ""),
        }

    elif tool_name == "metrics_get":
        MetricsGetInput(**arguments)
        result = await _get_flow("metrics").ainvoke(
            {
                "action": "collect",
                "metrics_output": "",
                "error": None,
            }
        )
        if result.get("error"):
            raise ValueError(result["error"])
        return {"metrics": result.get("metrics_output", "")}

    elif tool_name == "install_stategraph":
        package_name = arguments.get("package_name", "")
        version = arguments.get("version")
        if not package_name:
            raise ValueError("package_name is required")
        from app.flows.install_stategraph import install_stategraph

        result = await install_stategraph(package_name, version)
        # Invalidate all cached flows so next call uses new package
        invalidate_flow_cache()
        from app.flows.imperator_wrapper import invalidate as invalidate_imperator

        invalidate_imperator()
        return result

    else:
        raise ValueError(f"Unknown tool: {tool_name}")

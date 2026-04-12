"""
OpenAI-compatible chat completions endpoint.

Implements /v1/chat/completions following the OpenAI API specification.
Routes to the Imperator StateGraph.
Supports both streaming (SSE) and non-streaming responses.
"""

import json
import logging
import time
import uuid
from typing import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import ValidationError

from app.config import async_load_config
from app.flows.imperator_wrapper import (
    astream_events_with_metrics,
    invoke_with_metrics,
)
from app.models import ChatCompletionRequest
from app.routes.caller_identity import resolve_caller

_log = logging.getLogger("context_broker.routes.chat")

router = APIRouter()


@router.post("/v1/chat/completions", response_model=None)
async def chat_completions(request: Request):
    """Handle OpenAI-compatible chat completion requests.

    Routes to the Imperator StateGraph. Supports streaming and non-streaming.
    Metrics are recorded inside the flow layer per REQ-001 §6.4.
    """
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError) as exc:
        _log.warning("Chat: failed to parse request body: %s", exc)
        return JSONResponse(
            status_code=400,
            content={
                "error": {"message": "Invalid JSON", "type": "invalid_request_error"}
            },
        )

    try:
        chat_request = ChatCompletionRequest(**body)
    except ValidationError as exc:
        _log.warning("Chat: request validation failed: %s", exc)
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "message": str(exc),
                    "type": "invalid_request_error",
                }
            },
        )

    # R7-M9: Wrap config load in try/except with error response
    try:
        config = await async_load_config()
    except (OSError, RuntimeError, ValueError) as exc:
        _log.error("Chat: config load failed: %s", exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": "Configuration unavailable",
                    "type": "internal_error",
                }
            },
        )

    imperator_manager = getattr(request.app.state, "imperator_manager", None)

    # Extract the last user message as the primary input
    user_messages = [m for m in chat_request.messages if m.role == "user"]
    if not user_messages:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": "At least one user message is required",
                    "type": "invalid_request_error",
                }
            },
        )

    # G5-27: Allow clients to specify a context_window_id for multi-client
    # isolation via x-context-window-id header or context_window_id in the body.
    # Also accepts the legacy x-conversation-id / conversation_id for compatibility.
    # Falls back to the default Imperator context window when not provided.
    context_window_id = (
        request.headers.get("x-context-window-id")
        or body.get("context_window_id")
        or request.headers.get("x-conversation-id")
        or body.get("conversation_id")
    )
    if not context_window_id and imperator_manager is not None:
        context_window_id = await imperator_manager.get_context_window_id()

    # Convert plain messages to LangChain message objects
    # G5-28: Include ToolMessage so tool-role messages are not coerced to HumanMessage.
    _role_map = {
        "user": HumanMessage,
        "system": SystemMessage,
        "assistant": AIMessage,
        "tool": ToolMessage,
    }
    lc_messages = []
    for m in chat_request.messages:
        cls = _role_map.get(m.role, HumanMessage)
        if cls is ToolMessage:
            # ToolMessage requires a tool_call_id; use the one from the
            # request body if available, otherwise fall back to a placeholder.
            tool_call_id = m.tool_call_id or "unknown"
            lc_messages.append(
                ToolMessage(content=m.content, tool_call_id=tool_call_id)
            )
        elif cls is AIMessage:
            # R7-M14: Pass tool_calls if present for AIMessage (G5-28)
            lc_messages.append(
                AIMessage(content=m.content, tool_calls=m.tool_calls or [])
            )
        else:
            lc_messages.append(cls(content=m.content))

    # Resolve caller identity for sender/recipient on stored messages.
    caller = await resolve_caller(request, chat_request.user)
    config = {
        **config,
        "imperator": {
            **config.get("imperator", {}),
            "_request_user": caller,
        },
    }

    initial_state = {
        "messages": lc_messages,
        "context_window_id": str(context_window_id) if context_window_id else None,
        "config": config,
        "response_text": None,
        "error": None,
    }

    try:
        if chat_request.stream:
            return StreamingResponse(
                _stream_imperator_response(initial_state, chat_request),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        else:
            # Metrics recorded inside invoke_with_metrics (flow layer)
            result = await invoke_with_metrics(initial_state)

            if result.get("error"):
                _log.error("Imperator flow error: %s", result["error"])
                return JSONResponse(
                    status_code=500,
                    content={
                        "error": {
                            "message": result["error"],
                            "type": "internal_error",
                        }
                    },
                )

            response_text = result.get("response_text", "")

            return JSONResponse(
                content=_build_completion_response(response_text, chat_request.model)
            )

    except (RuntimeError, ConnectionError, OSError) as exc:
        _log.error("Chat completion failed: %s", exc, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": "Internal server error",
                    "type": "internal_error",
                }
            },
        )


async def _stream_imperator_response(
    initial_state: dict,
    chat_request: ChatCompletionRequest,
) -> AsyncGenerator[str, None]:
    """Stream the Imperator response as SSE tokens.

    M-22: astream_events(version="v2") captures on_chat_model_stream events
    from nested ainvoke() calls within the LangGraph runtime, so real token
    streaming works without requiring the agent to use astream() internally.
    Metrics are recorded inside astream_events_with_metrics (flow layer).
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    try:
        # G5-29: Known limitation — when the ReAct agent processes tool calls,
        # astream_events may emit no content tokens for those intermediate LLM
        # turns (only the final non-tool-call turn produces streamable tokens).
        # Metrics recorded inside the flow wrapper per REQ-001 §6.4.
        async for event in astream_events_with_metrics(initial_state):
            if event["event"] == "on_chat_model_stream":
                token = event["data"]["chunk"].content
                if token:
                    chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": chat_request.model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": token},
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"

        # Final chunk with finish_reason
        final_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": chat_request.model,
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                }
            ],
        }
        yield f"data: {json.dumps(final_chunk)}\n\n"
        yield "data: [DONE]\n\n"

    except (RuntimeError, ConnectionError, OSError) as exc:
        _log.error("Streaming imperator response failed: %s", exc, exc_info=True)
        error_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": chat_request.model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "An error occurred processing your request."},
                    "finish_reason": "stop",
                }
            ],
        }
        yield f"data: {json.dumps(error_chunk)}\n\n"
        yield "data: [DONE]\n\n"


def _build_completion_response(response_text: str, model: str) -> dict:
    """Build an OpenAI-compatible non-streaming completion response."""
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": response_text,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": -1,
            "completion_tokens": -1,
            "total_tokens": -1,
        },
    }

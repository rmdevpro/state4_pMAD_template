"""
OpenAI-compatible chat completions endpoint — HTTP receiver only.

Receives the request, hands {"payload": body} to the AE dispatcher to get
the correct TE graph, then streams or invokes that graph directly.

No routing logic here (ERQ-002 §2) — all routing is owned by the AE's
GraphDispatcher. The route handler streams directly from the TE graph because
LangGraph does not propagate astream_events() through dynamic ainvoke() calls
between separately compiled graphs. See base_pmad_ae/dispatcher.py for details.

Contract with stategraphs:
  Input:  full OpenAI request body (dict) as initial state under "payload" key
  Output: {"final_response": str, "conversation_id": str | None}
  Streaming: astream_events emits on_chat_model_stream events
"""

import json
import logging
import time
import uuid
from typing import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from app.models import ChatCompletionRequest

_log = logging.getLogger("pmad_template.routes.chat")

router = APIRouter()


@router.post("/v1/chat/completions", response_model=None)
async def chat_completions(request: Request):
    """Validate and dispatch chat requests via the AE dispatcher."""
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError) as exc:
        _log.warning("Chat: failed to parse request body: %s", exc)
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "Invalid JSON", "type": "invalid_request_error"}},
        )

    try:
        chat_request = ChatCompletionRequest(**body)
    except ValidationError as exc:
        _log.warning("Chat: request validation failed: %s", exc)
        return JSONResponse(
            status_code=422,
            content={"error": {"message": str(exc), "type": "invalid_request_error"}},
        )

    from app.package_registry import get_dispatcher
    dispatcher = get_dispatcher()
    if dispatcher is None:
        return JSONResponse(
            status_code=503,
            content={"error": {"message": "AE not loaded", "type": "service_unavailable"}},
        )

    model = chat_request.model
    graph = await dispatcher.get_graph(model)
    if graph is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"message": f"Model not found: {model}", "type": "invalid_request_error"}},
        )

    _log.info("Routing model=%s", model)
    initial_state = {"payload": body}

    try:
        if chat_request.stream:
            return StreamingResponse(
                _stream_response(graph, initial_state, model),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        else:
            result = await graph.ainvoke(initial_state)

            if result.get("error"):
                _log.error("Stategraph error for model=%s: %s", model, result["error"])
                return JSONResponse(
                    status_code=500,
                    content={"error": {"message": result["error"], "type": "internal_error"}},
                )

            response_text = result.get("final_response", "") or result.get("response_text", "")
            conversation_id = result.get("conversation_id")
            return JSONResponse(content=_build_completion_response(response_text, model, conversation_id))

    except (RuntimeError, ConnectionError, OSError) as exc:
        _log.error("Chat completion failed for model=%s: %s", model, exc, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": {"message": "Internal server error", "type": "internal_error"}},
        )


async def _stream_response(
    graph,
    initial_state: dict,
    model: str,
) -> AsyncGenerator[str, None]:
    """Stream stategraph response as SSE tokens."""
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    yielded_any = False

    try:
        async for event in graph.astream_events(initial_state, version="v2"):
            kind = event["event"]
            if kind == "on_chat_model_stream":
                chunk_data = event["data"].get("chunk")
                if chunk_data is None:
                    continue
                content = chunk_data.content if hasattr(chunk_data, "content") else ""
                if not content:
                    continue
                delta = {"content": content}
                if not yielded_any:
                    delta["role"] = "assistant"
                    yielded_any = True
                sse_chunk = json.dumps({
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                })
                yield f"data: {sse_chunk}\n\n"
            elif kind == "on_chat_model_end":
                if not yielded_any:
                    output_msg = event.get("data", {}).get("output")
                    if (
                        output_msg is not None
                        and hasattr(output_msg, "content")
                        and output_msg.content
                    ):
                        has_tool_calls = bool(
                            getattr(output_msg, "tool_calls", None)
                            or getattr(output_msg, "additional_kwargs", {}).get("tool_calls")
                        )
                        if not has_tool_calls:
                            content = (
                                output_msg.content
                                if isinstance(output_msg.content, str)
                                else str(output_msg.content)
                            )
                            delta = {"role": "assistant", "content": content}
                            sse_chunk = json.dumps({
                                "id": completion_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": model,
                                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                            })
                            yield f"data: {sse_chunk}\n\n"
                            yielded_any = True
    except (RuntimeError, ValueError, TypeError, OSError) as exc:
        _log.error("Streaming error for model=%s: %s", model, exc)

    final = json.dumps({
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    })
    yield f"data: {final}\n\ndata: [DONE]\n\n"


def _build_completion_response(
    response_text: str, model: str, conversation_id: str | None = None
) -> dict:
    """Build an OpenAI-compatible non-streaming completion response."""
    response = {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": response_text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": -1, "completion_tokens": -1, "total_tokens": -1},
    }
    if conversation_id:
        response["conversation_id"] = conversation_id
    return response

"""Imperator Chat UI — Gradio-based multi-MAD client.

Layout:
  - Left sidebar: MAD selector, conversation list (Radio), new/rename/delete
  - Center: chat panel (full width, streaming)
  - Bottom bar: System Info button opens accordion with Health, Context, Logs
"""

import json
import logging
import os

import gradio as gr
import httpx
import yaml

from mad_client import MADClient

logging.basicConfig(level=logging.INFO)
_log = logging.getLogger("ui")


# ── Config ───────────────────────────────────────────────────────────


def load_config() -> dict:
    config_path = os.environ.get("CONFIG_PATH", "/app/config.yml")
    if not os.path.exists(config_path):
        config_path = os.path.join(os.path.dirname(__file__), "config.yml")
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


CONFIG = load_config()
MADS = {
    m["name"]: MADClient(m["name"], m["url"], m.get("hostname", ""))
    for m in CONFIG.get("mads", [])
}


# ── Helpers ──────────────────────────────────────────────────────────


def _parse_conv_id(choice: str) -> str:
    """Extract UUID from 'title (N msgs) | uuid' format."""
    if choice and "|" in choice:
        return choice.split("|")[-1].strip()
    return ""


async def _get_conv_choices(client: MADClient) -> list[str]:
    """Fetch conversation list as Radio choices."""
    try:
        convs = await client.list_conversations()
        return [
            f"{c.get('title', 'Untitled')[:40]} ({c.get('message_count', 0)}) | {c['id']}"
            for c in convs
        ]
    except (RuntimeError, OSError):
        return []


async def _get_health_text(client: MADClient) -> str:
    health = await client.health()
    lines = [f"Status: {health.get('status', 'unknown')}"]
    for key, val in health.items():
        if key != "status":
            lines.append(f"  {key}: {val}")
    return "\n".join(lines)


async def _get_context_info(client: MADClient, conv_id: str) -> str:
    try:
        result = await client.get_context_info(conv_id)
        windows = result.get("context_windows", [])
        if not windows:
            return "No context windows"
        lines = []
        for w in windows:
            lines.append(f"Build: {w.get('build_type', '?')}")
            lines.append(f"Budget: {w.get('max_token_budget', '?')} tokens")
            lines.append(f"Assembled: {w.get('last_assembled_at', 'never')}")
        return "\n".join(lines)
    except (RuntimeError, OSError):
        return "Unavailable"


# ── Event handlers ───────────────────────────────────────────────────


async def on_page_load(mad_name):
    """Refresh conversation list and health on every page load."""
    client = MADS.get(mad_name)
    if not client:
        return gr.update(choices=[], value=None), ""
    choices = await _get_conv_choices(client)
    health = await _get_health_text(client)
    return gr.update(choices=choices, value=None), health


async def on_mad_selected(mad_name):
    """MAD switch — refresh conversations, health, clear chat."""
    client = MADS.get(mad_name)
    if not client:
        return gr.update(choices=[], value=None), "", []
    choices = await _get_conv_choices(client)
    health = await _get_health_text(client)
    return gr.update(choices=choices, value=None), health, []


async def on_conv_selected(conv_choice, mad_name):
    """Load conversation history into chat panel."""
    if not conv_choice or not mad_name:
        return [], ""
    client = MADS.get(mad_name)
    if not client:
        return [], ""

    conv_id = _parse_conv_id(conv_choice)
    if not conv_id:
        return [], ""

    messages = await client.get_history(conv_id)
    chat_history = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "user":
            chat_history.append({"role": "user", "content": content})
        elif role == "assistant" and content:
            chat_history.append({"role": "assistant", "content": content})

    info = await _get_context_info(client, conv_id)
    return chat_history, info


async def on_new_conversation(mad_name):
    """Start a new conversation — clear chat, deselect list."""
    return [], gr.update(value=None), ""


async def on_chat_submit(message, history, mad_name, conv_choice):
    """Send message with streaming. Auto-creates conversation on first message."""
    client = MADS.get(mad_name)
    if not client:
        history = history + [
            {"role": "user", "content": message},
            {"role": "assistant", "content": "No MAD selected"},
        ]
        yield history, gr.update()
        return

    conv_id = _parse_conv_id(conv_choice) if conv_choice else None

    history = history + [{"role": "user", "content": message}]
    yield history, gr.update()

    api_messages = [{"role": m["role"], "content": m["content"]} for m in history]

    response = ""
    try:
        async for chunk in client.chat_stream(
            api_messages, conversation_id=conv_id, user="gradio-ui"
        ):
            response += chunk
            updated = history + [{"role": "assistant", "content": response}]
            yield updated, gr.update()
    except (httpx.HTTPError, RuntimeError, OSError) as exc:
        response = f"Error: {exc}"
        updated = history + [{"role": "assistant", "content": response}]
        yield updated, gr.update()


async def on_refresh_after_chat(mad_name):
    """Refresh conversation list after a chat completes.

    Called as .then() after on_chat_submit so it runs reliably
    instead of being a yield inside a generator.
    """
    client = MADS.get(mad_name)
    if not client:
        return gr.update()
    choices = await _get_conv_choices(client)
    return gr.update(choices=choices)


async def on_rename_conversation(conv_choice, new_name, mad_name):
    """Rename the selected conversation."""
    client = MADS.get(mad_name)
    if not client or not conv_choice or not new_name:
        return gr.update(), ""

    conv_id = _parse_conv_id(conv_choice)
    if not conv_id:
        return gr.update(), ""

    try:
        await client._mcp_call(
            "conv_rename_conversation",
            {"conversation_id": conv_id, "title": new_name},
        )
    except (RuntimeError, OSError) as exc:
        _log.warning("Rename failed: %s", exc)

    choices = await _get_conv_choices(client)
    return gr.update(choices=choices), ""


async def on_delete_conversation(conv_choice, mad_name):
    """Delete selected conversation and refresh list."""
    client = MADS.get(mad_name)
    if not client or not conv_choice:
        return gr.update(choices=[], value=None), []

    conv_id = _parse_conv_id(conv_choice)
    if not conv_id:
        return gr.update(), []

    await client.delete_conversation(conv_id)
    choices = await _get_conv_choices(client)
    return gr.update(choices=choices, value=None), []


async def on_refresh_logs(mad_name):
    """Refresh log viewer."""
    client = MADS.get(mad_name)
    if not client:
        return "No MAD selected"
    try:
        entries = await client.query_logs(limit=40)
        if not entries:
            return "No log entries"
        lines = []
        for e in entries:
            ts = (e.get("timestamp") or "?")[-8:]
            lvl = e.get("level", "?")
            msg = e.get("message", "")[:120]
            lines.append(f"[{ts}] [{lvl}] {msg}")
        return "\n".join(lines)
    except (RuntimeError, OSError):
        return "Failed to load logs"


async def check_all_health():
    """Health bar at top."""
    parts = []
    for name, client in MADS.items():
        health = await client.health()
        status = health.get("status", "unknown")
        indicator = {"healthy": "\u2705", "degraded": "\u26a0\ufe0f"}.get(
            status, "\u274c"
        )
        parts.append(f"{indicator} {name}")
    return " | ".join(parts) if parts else "No MADs configured"


# ── Build the UI ─────────────────────────────────────────────────────

default_mad = list(MADS.keys())[0] if MADS else ""

with gr.Blocks(title="Imperator Chat", theme=gr.themes.Soft()) as demo:
    current_mad = gr.State(default_mad)

    gr.Markdown("# Imperator Chat")
    health_bar = gr.Markdown("")

    with gr.Row():
        # ── Left sidebar ─────────────────────────────────────
        with gr.Column(scale=1, min_width=250):
            mad_selector = gr.Dropdown(
                choices=list(MADS.keys()),
                value=default_mad,
                label="Select MAD",
            )

            gr.Markdown("### Conversations")
            conv_list = gr.Radio(
                choices=[],
                value=None,
                label="",
                show_label=False,
            )

            new_conv_btn = gr.Button(
                "New Conversation", size="sm", variant="primary"
            )

            with gr.Accordion("Manage", open=False):
                rename_input = gr.Textbox(
                    placeholder="New title...",
                    show_label=False,
                )
                rename_btn = gr.Button("Rename", size="sm")
                delete_btn = gr.Button(
                    "Delete", size="sm", variant="stop"
                )

        # ── Chat panel (full width) ──────────────────────────
        with gr.Column(scale=4):
            chatbot = gr.Chatbot(type="messages", height=550)
            with gr.Row():
                msg_input = gr.Textbox(
                    placeholder="Message the Imperator...",
                    show_label=False,
                    scale=6,
                )
                send_btn = gr.Button("Send", scale=1, variant="primary")

    # ── System Info (bottom accordion) ───────────────────────
    with gr.Accordion("System Info", open=False):
        with gr.Row():
            with gr.Column():
                gr.Markdown("#### Health")
                health_detail = gr.Textbox(
                    lines=4, interactive=False, show_label=False
                )
            with gr.Column():
                gr.Markdown("#### Context")
                context_panel = gr.Textbox(
                    lines=4, interactive=False, show_label=False
                )
            with gr.Column():
                gr.Markdown("#### Logs")
                log_panel = gr.Textbox(
                    lines=6, interactive=False, show_label=False
                )
                refresh_logs_btn = gr.Button("Refresh Logs", size="sm")

    # ── Events ───────────────────────────────────────────────

    # Page load — refresh conversations, health, logs
    demo.load(
        fn=on_page_load,
        inputs=[mad_selector],
        outputs=[conv_list, health_detail],
    )
    demo.load(fn=check_all_health, outputs=[health_bar])
    demo.load(fn=on_refresh_logs, inputs=[mad_selector], outputs=[log_panel])

    # MAD selection
    mad_selector.change(
        fn=on_mad_selected,
        inputs=[mad_selector],
        outputs=[conv_list, health_detail, chatbot],
    ).then(fn=lambda m: m, inputs=[mad_selector], outputs=[current_mad])

    # Conversation selection — load history
    conv_list.change(
        fn=on_conv_selected,
        inputs=[conv_list, current_mad],
        outputs=[chatbot, context_panel],
    )

    # New conversation — clear chat, deselect
    new_conv_btn.click(
        fn=on_new_conversation,
        inputs=[current_mad],
        outputs=[chatbot, conv_list, context_panel],
    )

    # Chat submit
    send_btn.click(
        fn=on_chat_submit,
        inputs=[msg_input, chatbot, current_mad, conv_list],
        outputs=[chatbot, conv_list],
    ).then(
        fn=lambda: "", outputs=[msg_input]
    ).then(
        fn=on_refresh_after_chat,
        inputs=[current_mad],
        outputs=[conv_list],
    )

    msg_input.submit(
        fn=on_chat_submit,
        inputs=[msg_input, chatbot, current_mad, conv_list],
        outputs=[chatbot, conv_list],
    ).then(
        fn=lambda: "", outputs=[msg_input]
    ).then(
        fn=on_refresh_after_chat,
        inputs=[current_mad],
        outputs=[conv_list],
    )

    # Rename
    rename_btn.click(
        fn=on_rename_conversation,
        inputs=[conv_list, rename_input, current_mad],
        outputs=[conv_list, rename_input],
    )

    # Delete
    delete_btn.click(
        fn=on_delete_conversation,
        inputs=[conv_list, current_mad],
        outputs=[conv_list, chatbot],
    )

    # Logs
    refresh_logs_btn.click(
        fn=on_refresh_logs, inputs=[current_mad], outputs=[log_panel]
    )


if __name__ == "__main__":
    port = CONFIG.get("port", 7860)
    demo.launch(server_name="0.0.0.0", server_port=port)

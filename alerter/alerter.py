"""
Context Broker Alerter — Instruction-driven webhook relay.

Receives CloudEvents-format webhooks, searches an instructions table
for the best matching instruction, formats via LLM using that instruction
as the system prompt, and sends to the channels specified in the instruction.

The Imperator manages instructions via tools — no static config needed
for routing. The alerter config only has: LLM settings, embedding settings,
Postgres DSN, and a default fallback channel.
"""

import asyncio
import json
import logging
import os
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage
from typing import Optional

import asyncpg
import httpx
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

# ── Logging ────────────────────────────────────────────────────────


class _JsonFormatter(logging.Formatter):
    """Format log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        from datetime import datetime, timezone

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_JsonFormatter())
logging.getLogger().setLevel(logging.INFO)
logging.getLogger().addHandler(_handler)
_log = logging.getLogger("alerter")

# ── App ────────────────────────────────────────────────────────────

app = FastAPI(title="Context Broker Alerter")

_config: dict = {}
_pool: Optional[asyncpg.Pool] = None

CONFIG_PATH = os.environ.get("ALERTER_CONFIG", "/config/alerter.yml")
POSTGRES_DSN = os.environ.get("POSTGRES_DSN", "")
CHANNEL_TIMEOUT = int(os.environ.get("CHANNEL_TIMEOUT", "10"))
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "30"))

_PG_RETRY_COUNT = int(os.environ.get("PG_RETRY_COUNT", "10"))
_PG_RETRY_INTERVAL = int(os.environ.get("PG_RETRY_INTERVAL", "3"))


# ── Config ─────────────────────────────────────────────────────────


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (FileNotFoundError, yaml.YAMLError) as exc:
        _log.warning("Failed to load alerter config: %s", exc)
        return {}


# ── Lifecycle ──────────────────────────────────────────────────────


@app.on_event("startup")
async def _startup() -> None:
    global _config, _pool
    _config = await asyncio.to_thread(_load_config)
    _log.info("Alerter starting")

    # Retry Postgres connection — Postgres may not be ready at container start
    for attempt in range(_PG_RETRY_COUNT):
        try:
            _pool = await asyncpg.create_pool(POSTGRES_DSN, min_size=1, max_size=3)
            await _ensure_tables()
            _log.info("Postgres connected")
            break
        except (OSError, asyncpg.PostgresError) as exc:
            _log.warning("Postgres not available (attempt %d/%d): %s", attempt + 1, _PG_RETRY_COUNT, exc)
            _pool = None
            if attempt < _PG_RETRY_COUNT - 1:
                await asyncio.sleep(_PG_RETRY_INTERVAL)


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _pool:
        await _pool.close()


async def _ensure_tables() -> None:
    """Create alert_instructions and alert_history tables if needed."""
    if not _pool:
        return

    # Instructions table — the Imperator populates this via tools
    await _pool.execute("""
        CREATE TABLE IF NOT EXISTS alert_instructions (
            id SERIAL PRIMARY KEY,
            description TEXT NOT NULL,
            instruction TEXT NOT NULL,
            channels JSONB NOT NULL DEFAULT '[]',
            embedding vector,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    # Events table — one row per incoming webhook event
    await _pool.execute("""
        CREATE TABLE IF NOT EXISTS alert_events (
            id SERIAL PRIMARY KEY,
            event_type VARCHAR(255) NOT NULL,
            event_source VARCHAR(255),
            event_subject VARCHAR(255),
            message TEXT,
            formatted_message TEXT,
            instruction_id INTEGER,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    # Deliveries table — one row per channel delivery attempt, FK to event
    await _pool.execute("""
        CREATE TABLE IF NOT EXISTS alert_deliveries (
            id SERIAL PRIMARY KEY,
            event_id INTEGER NOT NULL REFERENCES alert_events(id),
            channel_type VARCHAR(50) NOT NULL,
            succeeded BOOLEAN NOT NULL,
            error TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)


# ── Health ─────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> JSONResponse:
    pg_status = "down"
    instruction_count = 0
    if _pool is not None:
        try:
            instruction_count = await _pool.fetchval(
                "SELECT COUNT(*) FROM alert_instructions"
            )
            pg_status = "up"
        except (asyncpg.PostgresError, OSError):
            pg_status = "down"

    is_healthy = pg_status == "up"
    body = {
        "status": "healthy" if is_healthy else "unhealthy",
        "postgres": pg_status,
        "instructions": instruction_count,
    }
    return JSONResponse(body, status_code=200 if is_healthy else 503)


# ── Metrics ───────────────────────────────────────────────────────

ALERTER_EVENTS_TOTAL = Counter(
    "alerter_events_total", "Total webhook events processed", ["event_type", "status"]
)
ALERTER_EVENT_DURATION = Histogram(
    "alerter_event_duration_seconds", "Webhook event processing duration"
)


@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ── Embedding helper ───────────────────────────────────────────────


async def _embed_text(text: str) -> Optional[list[float]]:
    """Embed text using the configured embedding endpoint."""
    emb_config = _config.get("embeddings", {})
    base_url = emb_config.get("base_url", "")
    model = emb_config.get("model", "")
    if not base_url or not model:
        return None

    api_key_env = emb_config.get("api_key_env", "")
    headers = {"Content-Type": "application/json"}
    if api_key_env:
        key = os.environ.get(api_key_env, "")
        if key:
            headers["Authorization"] = f"Bearer {key}"

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/embeddings",
                json={"model": model, "input": text},
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()["data"][0]["embedding"]
    except (httpx.HTTPError, KeyError, IndexError, OSError) as exc:
        _log.warning("Embedding failed: %s", exc)
        return None


# ── Instruction lookup ─────────────────────────────────────────────


async def _find_instruction(event: dict) -> Optional[dict]:
    """Find the best matching instruction for this event via semantic search."""
    if not _pool:
        return None

    # Build search query from event type + message
    event_type = event.get("type", "")
    message = event.get("data", {}).get("message", "")
    query = f"{event_type}: {message}"

    # Try vector search first
    query_vec = await _embed_text(query)
    if query_vec:
        vec_str = "[" + ",".join(str(v) for v in query_vec) + "]"
        try:
            row = await _pool.fetchrow(
                """
                SELECT id, description, instruction, channels
                FROM alert_instructions
                WHERE embedding IS NOT NULL
                ORDER BY embedding <=> $1::vector
                LIMIT 1
                """,
                vec_str,
            )
            if row:
                return dict(row)
        except (asyncpg.PostgresError, OSError) as exc:
            _log.warning("Vector search failed: %s", exc)

    # Fallback: text match on description
    try:
        row = await _pool.fetchrow(
            """
            SELECT id, description, instruction, channels
            FROM alert_instructions
            WHERE description ILIKE '%' || $1 || '%'
               OR description ILIKE '%' || $2 || '%'
            LIMIT 1
            """,
            event_type,
            event_type.split(".")[0] if "." in event_type else event_type,
        )
        if row:
            return dict(row)
    except (asyncpg.PostgresError, OSError) as exc:
        _log.warning("Text search failed: %s", exc)

    return None


# ── Webhook endpoint ───────────────────────────────────────────────


_seen_event_ids: set[str] = set()
_SEEN_EVENT_IDS_MAX = 10000


@app.post("/webhook")
async def webhook(request: Request) -> JSONResponse:
    """Receive a CloudEvents webhook, find instruction, format, fan out."""
    try:
        event = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    # CR-M03: Validate input structure
    if not isinstance(event, dict):
        return JSONResponse({"error": "Event must be a JSON object"}, status_code=400)

    event_type = event.get("type", "")
    if not event_type:
        return JSONResponse({"error": "Missing 'type' field"}, status_code=400)

    data = event.get("data")
    if not isinstance(data, dict):
        return JSONResponse({"error": "'data' must be a JSON object"}, status_code=400)

    # CR-B04: Idempotency — deduplicate by CloudEvents id
    event_id = event.get("id")
    if event_id:
        if event_id in _seen_event_ids:
            return JSONResponse({"status": "duplicate", "id": event_id}, status_code=200)
        _seen_event_ids.add(event_id)
        # Bounded eviction
        if len(_seen_event_ids) > _SEEN_EVENT_IDS_MAX:
            # Discard oldest half (sets are unordered but this is good enough)
            to_remove = list(_seen_event_ids)[:_SEEN_EVENT_IDS_MAX // 2]
            for eid in to_remove:
                _seen_event_ids.discard(eid)

    _webhook_start = time.monotonic()

    message = data.get("message", json.dumps(data))

    # Step 1: Enrich with log context if configured
    log_ctx = _config.get("log_context", {})
    if log_ctx.get("enabled") and _pool:
        logs = await _fetch_log_context(log_ctx)
        if logs:
            data["log_context"] = logs

    # Step 2: Find matching instruction
    instruction = await _find_instruction(event)
    instruction_id = instruction["id"] if instruction else None

    # Step 3: Format via LLM using instruction as system prompt
    formatted = message
    if instruction:
        llm_config = _config.get("llm", {})
        if llm_config.get("base_url"):
            formatted = (
                await _llm_format(llm_config, instruction["instruction"], event)
                or message
            )
        channels = json.loads(instruction["channels"]) if isinstance(
            instruction["channels"], str
        ) else instruction["channels"]
    else:
        # No instruction matched — use default channels from config
        channels = _config.get("default_channels", [{"type": "log"}])
        _log.info("No instruction matched for '%s' — using defaults", event_type)

    # Step 4: Fan out to channels
    succeeded = []
    failed = []
    for channel in channels:
        ch_type = channel.get("type", "log")
        try:
            await _send_to_channel(ch_type, channel, formatted, event)
            succeeded.append(ch_type)
        except (httpx.HTTPError, OSError, RuntimeError, smtplib.SMTPException) as exc:
            _log.error("Channel '%s' failed: %s", ch_type, exc)
            failed.append(ch_type)

    # Step 5: Record event and deliveries
    await _record_event_and_deliveries(
        event, message, formatted, instruction_id, channels, succeeded, failed
    )

    # CR-M02: Track metrics
    _status = "success" if not failed else "partial"
    ALERTER_EVENTS_TOTAL.labels(event_type=event_type, status=_status).inc()
    ALERTER_EVENT_DURATION.observe(time.monotonic() - _webhook_start)

    _log.info(
        "Event '%s' processed: instruction=%s, %d/%d channels succeeded",
        event_type,
        instruction_id or "default",
        len(succeeded),
        len(channels),
    )

    return JSONResponse({
        "status": "processed",
        "type": event_type,
        "instruction_id": instruction_id,
        "channels_succeeded": succeeded,
        "channels_failed": failed,
    })


# ── Log Context ────────────────────────────────────────────────────


async def _fetch_log_context(ctx_config: dict) -> Optional[str]:
    """Fetch recent log entries to enrich the event."""
    if not _pool:
        return None

    level = ctx_config.get("level", "ERROR")
    limit = ctx_config.get("limit", 10)
    minutes = ctx_config.get("minutes", 5)

    try:
        rows = await _pool.fetch(
            """
            SELECT container_name, data->>'level' AS level, message, log_timestamp
            FROM system_logs
            WHERE ($1 = '' OR data->>'level' = $1)
              AND log_timestamp > NOW() - ($2 || ' minutes')::INTERVAL
            ORDER BY log_timestamp DESC
            LIMIT $3
            """,
            level,
            str(minutes),
            limit,
        )
        if not rows:
            return None

        lines = []
        for row in rows:
            ts = row["log_timestamp"].isoformat() if row["log_timestamp"] else ""
            lines.append(
                f"[{ts}] [{row['container_name']}] [{row['level']}] {row['message']}"
            )
        return "\n".join(lines)
    except (asyncpg.PostgresError, OSError) as exc:
        _log.warning("Failed to fetch log context: %s", exc)
        return None


# ── LLM Formatting ────────────────────────────────────────────────


async def _llm_format(
    llm_config: dict, instruction_text: str, event: dict
) -> Optional[str]:
    """Call LLM with the instruction as system prompt and event as user message."""
    base_url = llm_config.get("base_url", "")
    model = llm_config.get("model", "")
    api_key_env = llm_config.get("api_key_env", "")

    if not base_url or not model:
        return None

    headers = {"Content-Type": "application/json"}
    if api_key_env:
        key = os.environ.get(api_key_env, "")
        if key:
            headers["Authorization"] = f"Bearer {key}"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": instruction_text},
            {"role": "user", "content": json.dumps(event, default=str)},
        ],
        "temperature": 0.3,
        "max_tokens": 500,
    }

    try:
        async with httpx.AsyncClient(
            verify=ssl.create_default_context()
        ) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers=headers,
                timeout=LLM_TIMEOUT,
            )
            resp.raise_for_status()
            result = resp.json()
            return result["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, OSError) as exc:
        _log.warning("LLM formatting failed: %s", exc)
        return None


# ── Channel Senders ────────────────────────────────────────────────


async def _send_to_channel(
    ch_type: str, config: dict, message: str, event: dict
) -> None:
    """Send a formatted message to a specific channel type."""
    if ch_type == "slack":
        await _send_slack(config, message)
    elif ch_type == "discord":
        await _send_discord(config, message)
    elif ch_type == "ntfy":
        await _send_ntfy(config, message, event)
    elif ch_type == "smtp":
        await _send_smtp(config, message, event)
    elif ch_type == "twilio":
        await _send_twilio(config, message)
    elif ch_type == "webhook":
        await _send_webhook(config, message, event)
    elif ch_type == "log":
        _log.info("ALERT [%s]: %s", event.get("type", "unknown"), message)
    else:
        _log.warning("Unknown channel type: %s", ch_type)


async def _send_slack(config: dict, message: str) -> None:
    url = config.get("webhook_url", "")
    if not url:
        raise RuntimeError("Slack channel missing webhook_url")
    async with httpx.AsyncClient(verify=ssl.create_default_context()) as client:
        resp = await client.post(url, json={"text": message}, timeout=CHANNEL_TIMEOUT)
        resp.raise_for_status()


async def _send_discord(config: dict, message: str) -> None:
    url = config.get("webhook_url", "")
    if not url:
        raise RuntimeError("Discord channel missing webhook_url")
    async with httpx.AsyncClient(verify=ssl.create_default_context()) as client:
        resp = await client.post(
            url, json={"content": message}, timeout=CHANNEL_TIMEOUT
        )
        resp.raise_for_status()


async def _send_ntfy(config: dict, message: str, event: dict) -> None:
    url = config.get("url", "")
    if not url:
        raise RuntimeError("ntfy channel missing url")
    headers: dict[str, str] = {}
    title = event.get("subject") or event.get("type", "Alert")
    headers["Title"] = str(title)
    if config.get("priority"):
        headers["Priority"] = config["priority"]
    if event.get("type"):
        headers["Tags"] = event["type"]
    async with httpx.AsyncClient(verify=ssl.create_default_context()) as client:
        resp = await client.post(
            url, content=message, headers=headers, timeout=CHANNEL_TIMEOUT
        )
        resp.raise_for_status()


async def _send_smtp(config: dict, message: str, event: dict) -> None:
    host = config.get("host", "")
    port = config.get("port", 587)
    username = config.get("username", "")
    password_env = config.get("password_env", "")
    password = os.environ.get(password_env, "") if password_env else ""
    from_addr = config.get("from", username)
    to_addr = config.get("to", "")
    subject_template = config.get("subject_template", "Alert: {type}")
    if not host or not to_addr:
        raise RuntimeError("SMTP channel missing host or to address")
    msg = EmailMessage()
    msg["Subject"] = subject_template.format(
        type=event.get("type", "unknown"),
        source=event.get("source", ""),
        subject=event.get("subject", ""),
    )
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(message)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None, _sync_send_smtp, host, port, username, password, msg
    )


def _sync_send_smtp(
    host: str, port: int, username: str, password: str, msg: EmailMessage
) -> None:
    with smtplib.SMTP(host, port, timeout=CHANNEL_TIMEOUT) as server:
        server.starttls()
        if username and password:
            server.login(username, password)
        server.send_message(msg)


async def _send_twilio(config: dict, message: str) -> None:
    """Send SMS via Twilio REST API."""
    account_sid = config.get("account_sid", "")
    auth_token_env = config.get("auth_token_env", "")
    auth_token = os.environ.get(auth_token_env, "") if auth_token_env else ""
    from_number = config.get("from", "")
    to_number = config.get("to", "")

    if not account_sid or not auth_token or not from_number or not to_number:
        raise RuntimeError(
            "Twilio channel missing required fields: account_sid, auth_token_env, from, to"
        )

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"

    async with httpx.AsyncClient(verify=ssl.create_default_context()) as client:
        resp = await client.post(
            url,
            data={"From": from_number, "To": to_number, "Body": message[:1600]},
            auth=(account_sid, auth_token),
            timeout=CHANNEL_TIMEOUT,
        )
        resp.raise_for_status()


async def _send_webhook(config: dict, message: str, event: dict) -> None:
    url = config.get("url", "")
    if not url:
        raise RuntimeError("Webhook channel missing url")
    payload = {
        "type": event.get("type", ""),
        "message": message,
        "source": event.get("source", ""),
        "time": event.get("time", ""),
    }
    headers = config.get("headers", {})
    async with httpx.AsyncClient(verify=ssl.create_default_context()) as client:
        resp = await client.post(
            url, json=payload, headers=headers, timeout=CHANNEL_TIMEOUT
        )
        resp.raise_for_status()


# ── History ────────────────────────────────────────────────────────


async def _record_event_and_deliveries(
    event: dict,
    message: str,
    formatted: str,
    instruction_id: Optional[int],
    channels: list[dict],
    succeeded: list[str],
    failed: list[str],
) -> None:
    """Record the event and each delivery attempt in separate tables."""
    if not _pool:
        return
    try:
        # Insert event
        event_id = await _pool.fetchval(
            """
            INSERT INTO alert_events
                (event_type, event_source, event_subject, message,
                 formatted_message, instruction_id)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            event.get("type", ""),
            event.get("source"),
            event.get("subject"),
            message,
            formatted if formatted != message else None,
            instruction_id,
        )

        # Insert one delivery row per channel
        for channel in channels:
            ch_type = channel.get("type", "log")
            did_succeed = ch_type in succeeded
            error = None if did_succeed else "delivery failed"
            await _pool.execute(
                """
                INSERT INTO alert_deliveries
                    (event_id, channel_type, succeeded, error)
                VALUES ($1, $2, $3, $4)
                """,
                event_id,
                ch_type,
                did_succeed,
                error,
            )
    except (asyncpg.PostgresError, OSError) as exc:
        _log.warning("Failed to record alert event: %s", exc)

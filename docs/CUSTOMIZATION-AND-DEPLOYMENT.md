# State 4 pMAD Template — Customization and Deployment Guide

This guide walks through copying the template to create a new State 4 pMAD, customizing it for your domain, and deploying it.

---

## 1. Create Your Repository

Copy the template into a new repository:

```bash
git clone https://github.com/rmdevpro/state4_pMAD_template.git my-mad
cd my-mad
rm -rf .git
git init
git remote add origin https://github.com/yourorg/my-mad.git
```

---

## 2. Rename

The template uses `context-broker` / `context_broker` throughout. Replace both forms with your MAD's name:

```bash
# Hyphenated form (Docker service names, container names, hostnames, package names)
grep -rl 'context-broker' . --include='*.yml' --include='*.yaml' --include='*.py' --include='*.toml' --include='*.conf' --include='*.sh' --include='Dockerfile' | xargs sed -i 's/context-broker/my-mad/g'

# Underscored form (Python module names, logger names, entry_points groups)
grep -rl 'context_broker' . --include='*.py' --include='*.toml' | xargs sed -i 's/context_broker/my_mad/g'

# Rename package directories
mv packages/context-broker-ae packages/my-mad-ae
mv packages/context-broker-te packages/my-mad-te
mv packages/my-mad-ae/src/context_broker_ae packages/my-mad-ae/src/my_mad_ae
mv packages/my-mad-te/src/context_broker_te packages/my-mad-te/src/my_mad_te
```

Verify no stale references remain:

```bash
grep -r 'context.broker' . --include='*.py' --include='*.yml' --include='*.toml' --include='*.conf'
```

---

## 3. Configure Credentials

There are two credential files the deployment needs:
- A `.env` at the project root — compose reads this for variable interpolation (e.g., `${POSTGRES_PASSWORD}` in service definitions)
- `config/credentials/.env` — loaded into containers via `env_file` directive

Both can point at the same file.

### Standalone deployment

Create a `.env` file with your credentials:

```bash
cat > .env << 'EOF'
POSTGRES_PASSWORD=your-secure-password
OPENAI_API_KEY=sk-your-key-here
EOF

cp .env config/credentials/.env
```

### Ecosystem deployment (Joshua26)

Credentials live on the NFS share from irina (`192.168.1.110`), mounted at `/mnt/storage` on all hosts.

**Database credentials** are at `/mnt/storage/credentials/databases/`. Create a file for your MAD:

```bash
# On any host:
cat > /mnt/storage/credentials/databases/my-mad.env << 'EOF'
POSTGRES_PASSWORD=your-secure-password
EOF
```

Symlink it into the project:

```bash
ln -sf /mnt/storage/credentials/databases/my-mad.env .env
ln -sf /mnt/storage/credentials/databases/my-mad.env config/credentials/.env
```

**API keys** are at `/mnt/storage/credentials/api-keys/`. Available keys include:
- `gemini.env` (GEMINI_API_KEY)
- `openai.env` (OPENAI_API_KEY)
- `anthropic.env` (ANTHROPIC_API_KEY)
- `grok.env`, `together.env`, etc.

Load the key your Imperator needs via `docker-compose.override.yml`:

```yaml
services:
  my-mad-langgraph:
    env_file:
      - ./config/credentials/.env
      - /mnt/storage/credentials/api-keys/gemini.env
```

The `api_key_env` field in `te.yml` must match the variable name in the key file (e.g., `api_key_env: GEMINI_API_KEY` matches `GEMINI_API_KEY=...` in `gemini.env`).

---

## 4. Configure the Imperator

Edit `config/te.yml`:

```yaml
imperator:
  # Point at your inference provider
  base_url: https://generativelanguage.googleapis.com/v1beta/openai
  model: gemini-2.5-pro
  api_key_env: GEMINI_API_KEY    # Must match a key in your .env

  system_prompt: imperator_identity   # Loads from config/prompts/imperator_identity.md
  max_context_tokens: 8192
  max_iterations: 5
  temperature: 0.3
  admin_tools: true

# Optional: connect to a Context Broker for persistent conversation memory
# context_broker:
#   url: http://context-broker:8080
```

Edit `config/prompts/imperator_identity.md` to define your Imperator's Identity and Purpose.

---

## 5. Configure the AE

Edit `config/config.yml` (copy from `config.example.yml` if starting fresh):

```yaml
log_level: INFO

packages:
  source: local
  stategraph_packages:
    - my-mad-ae
    - my-mad-te

database:
  pool_min_size: 2
  pool_max_size: 10
```

---

## 6. Add Domain Logic

### AE Package (infrastructure flows)

Add new StateGraph flows in `packages/my-mad-ae/src/my_mad_ae/`:

1. Create a flow file (e.g., `my_domain_flow.py`) with a `build_my_domain_flow()` function
2. Register it in `register.py`:

```python
from my_mad_ae.my_domain_flow import build_my_domain_flow

return {
    "build_types": {},
    "flows": {
        "health_check": build_health_check_flow,
        "metrics": build_metrics_flow,
        "autoprompt_dispatcher": build_autoprompt_dispatcher_flow,
        "my_domain": build_my_domain_flow,
    },
}
```

3. Add a dispatch branch in `app/flows/tool_dispatch.py`
4. Add the tool schema in `app/routes/mcp.py` `_get_tool_list()`
5. Add a Pydantic input model in `app/models.py`

### TE Package (Imperator tools)

Add new tools in `packages/my-mad-te/src/my_mad_te/tools/`:

1. Create a tool file with `@tool` decorated functions and a `get_tools()` function
2. Import and call `get_tools()` in `imperator_flow.py` `_collect_tools()`

### Database Schema

Add domain tables via migrations in `app/migrations.py`:

```python
async def _migration_004(conn) -> None:
    """Migration 4: Add my_domain_table."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS my_domain_table (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            ...
        )
    """)

MIGRATIONS.append((4, "Add my_domain_table", _migration_004))
```

---

## 7. Configure the Autoprompter

### Add runbooks

Create markdown files in `config/runbooks/`. Each runbook is a prompt that gets delivered to the Imperator verbatim.

### Seed Dkron jobs

After deployment, seed jobs via the Dkron API. From inside the langgraph container (which can reach dkron on the private network):

```bash
docker exec my-mad-langgraph curl -s -X POST http://my-mad-dkron:8080/v1/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-job",
    "schedule": "0 0 12 * * *",
    "timezone": "UTC",
    "executor": "http",
    "executor_config": {
      "method": "POST",
      "url": "http://my-mad-langgraph:8000/autoprompt",
      "headers": "{\"Content-Type\": \"application/json\"}",
      "body": "{\"job_name\": \"my-job\", \"runbook_path\": \"my-runbook.md\"}"
    },
    "retries": 2
  }'
```

A seed script is provided at `config/dkron/seed-jobs.sh`. Edit it for your jobs and run after first deploy.

### Cron schedule format

Dkron uses 6-field cron (with seconds): `sec min hour day month weekday`

- `0 0 12 * * *` — daily at noon
- `@every 5m` — every 5 minutes
- `0 */30 8-17 * * 1-5` — every 30 min, 8AM-5PM, weekdays

---

## 8. Configure Alerting

Edit `config/alerter.yml` for notification channels. The alerter supports:

- **log** — logs to stdout (default, always works)
- **slack** — Slack webhook
- **discord** — Discord webhook
- **ntfy** — ntfy push notifications
- **smtp** — email via SMTP
- **twilio** — SMS via Twilio
- **webhook** — generic HTTP webhook

Example:

```yaml
default_channels:
  - type: log
  - type: ntfy
    url: https://ntfy.sh/my-mad-alerts
    priority: default
  - type: smtp
    host: smtp.zoho.com
    port: 587
    username: alerts@mydomain.com
    password_env: SMTP_PASSWORD
    from: alerts@mydomain.com
    to: me@mydomain.com
    subject_template: "[my-mad] Alert: {type}"
```

The Imperator manages alert instructions at runtime via tools (`add_alert_instruction`, `list_alert_instructions`, etc.). Instructions tell the alerter how to format and route specific event types.

---

## 9. Deploy

### Standalone

For standalone deployment (outside the Joshua26 ecosystem), override the pip index to use public PyPI:

```bash
docker compose build --build-arg PIP_INDEX_URL=https://pypi.org/simple/ --build-arg PIP_TRUSTED_HOST=pypi.org
docker compose up -d
```

### Ecosystem (Joshua26)

All Dockerfiles default to Alexandria (`192.168.1.110:3141`) for pip packages. Docker images route through the daemon-level registry mirror (`192.168.1.110:5001`, configured in `/etc/docker/daemon.json` on all hosts). Nothing goes over the internet.

Recommended deployment location: `/workspace/my-mad/` on the target host.

```bash
# On the target host (e.g., m5 at 192.168.1.120):
cd /workspace
git clone https://github.com/yourorg/my-mad.git
cd my-mad

# Set up credentials (see section 3)
ln -sf /mnt/storage/credentials/databases/my-mad.env .env
ln -sf /mnt/storage/credentials/databases/my-mad.env config/credentials/.env

# Create override for API keys
cat > docker-compose.override.yml << 'EOF'
services:
  my-mad-langgraph:
    env_file:
      - ./config/credentials/.env
      - /mnt/storage/credentials/api-keys/gemini.env
EOF

# Build and start
docker compose up -d --build
```

**Note:** The postgres data directory (`data/postgres/`) must be empty for first-time initialization. If a `.gitkeep` file exists there, remove it before starting: `rm -f data/postgres/.gitkeep`

### Verify

```bash
# Health
curl http://localhost:8080/health

# Metrics
curl http://localhost:8080/metrics

# Chat
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "my-mad", "messages": [{"role": "user", "content": "Hello, who are you?"}]}'

# MCP tool call
curl -X POST http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "metrics_get", "arguments": {}}}'
```

### Seed autoprompter jobs

```bash
# Edit config/dkron/seed-jobs.sh with your jobs, then:
docker cp config/dkron/seed-jobs.sh my-mad-langgraph:/tmp/seed.sh
docker exec my-mad-langgraph sh /tmp/seed.sh
```

---

## 10. Containers

The template ships with 7 containers:

| Container | Image | Purpose |
|---|---|---|
| `[mad]` | nginx:alpine | Gateway — sole network boundary |
| `[mad]-langgraph` | Custom Python | Bootstrap kernel — all StateGraph logic |
| `[mad]-postgres` | pgvector/pgvector:pg16 | Relational storage + vector search |
| `[mad]-log-shipper` | Custom Python | Tails container logs → postgres |
| `[mad]-dkron` | dkron/dkron:4.1.0 | Autoprompter — cron/interval job scheduler |
| `[mad]-alerter` | Custom Python | Notification relay (Slack/Discord/ntfy/SMTP/SMS) |
| `[mad]-ui` | Custom Python | Gradio chat UI for the Imperator |

All containers communicate on a private bridge network (`[mad]-net`). Only the nginx gateway is exposed to the host.

---

## 11. Context Broker Integration

The Imperator supports two modes, controlled by the `context_broker` section in `te.yml`:

**No-CB mode** (default): The Imperator uses LangGraph's `trim_messages` for context management. Conversations are not persisted across sessions. Suitable for pMADs that don't need long-term conversation memory.

**CB mode**: Set `context_broker.url` to point at a running Context Broker. The Imperator will use the CB's `get_context` and `store_message` tools for full context engineering — tiered compression, knowledge extraction, cross-session persistence.

```yaml
context_broker:
  url: http://context-broker:8080
```

---

## 12. File Layout

```
.
├── Dockerfile                      # Langgraph container build
├── docker-compose.yml              # All 7 containers
├── docker-compose.override.yml     # Local overrides (credentials, ports)
├── entrypoint.sh                   # Startup: install StateGraph wheels
├── requirements.txt                # Python dependencies
├── config.example.yml              # AE config template
├── te.example.yml                  # TE config template
├── config/
│   ├── config.yml                  # AE config (live)
│   ├── te.yml                      # TE config (live, hot-reloadable)
│   ├── alerter.yml                 # Alerter channel configuration
│   ├── credentials/.env            # Credentials (gitignored)
│   ├── prompts/imperator_identity.md  # Imperator system prompt
│   ├── runbooks/                   # Autoprompter runbook files
│   └── dkron/seed-jobs.sh          # Dkron job seed script
├── app/                            # Bootstrap kernel (FastAPI + flows)
├── packages/
│   ├── [mad]-ae/                   # AE StateGraph package
│   └── [mad]-te/                   # TE StateGraph package (Imperator)
├── alerter/                        # Alerter container source
├── log_shipper/                    # Log shipper container source
├── ui/                             # Gradio chat UI source
├── nginx/nginx.conf                # Gateway routing
├── postgres/init.sql               # Initial database schema
└── data/                           # Persistent data (bind mounted)
```

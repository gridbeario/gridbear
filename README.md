# GridBear

Plugin-based multi-channel AI assistant framework. Connect multiple LLM runners (Claude, GPT, Gemini, Ollama) to channels (Telegram, Discord, WhatsApp) with a shared plugin ecosystem for tools, memory, and workflows.

> **⚠️ Early Stage Project — Not Production Ready**
>
> GridBear started as a personal project and most of the codebase has been "vibe coded" — built iteratively with AI assistance, focusing on rapid prototyping over production-grade engineering.
>
> **I strongly advise against using these early releases in production environments.**
>
> The goal of open-sourcing GridBear is to build a community that can help the project grow, improve code quality, and eventually reach production readiness together. Contributions, feedback, and ideas are very welcome!

## Features

- **Multi-runner**: Claude API/CLI, OpenAI, Gemini, Ollama — switch per-agent
- **Multi-channel**: Telegram, Discord, WhatsApp (via Evolution API)
- **Plugin system**: 40+ plugins with manifest.json discovery, dependency resolution, and hot-reload
- **MCP Gateway**: SSE-based gateway with per-user OAuth2 connections, circuit breakers, rate limiting
- **Multi-agent**: YAML-configured agents with independent channels, tools, and system prompts
- **Memory**: Episodic and declarative memory with PostgreSQL pgvector
- **Workflow engine**: Visual DAG editor with agent, tool, condition, transform, and approval steps
- **Admin UI**: Web-based management with Nordic Tailwind design, plugin admin pages, and theme support
- **User portal**: Dashboard, profile, service connections, tool preferences, web chat
- **REST API**: Generic CRUD endpoints with ACL system and Swagger UI
- **ORM**: Odoo-inspired model layer with auto-migrations on PostgreSQL

## Architecture

```
                    Channels                          Runners
              ┌──────────────┐                  ┌──────────────┐
              │   Telegram   │                  │  Claude CLI  │
              │   Discord    │──► Agent ◄──────►│  Claude API  │
              │   WhatsApp   │   Manager        │  OpenAI      │
              │   WebChat    │                  │  Gemini      │
              └──────┬───────┘                  │  Ollama      │
                     │                          └──────────────┘
                     ▼
              MessageProcessor
              (hooks pipeline)
                     │
         ┌───────────┼───────────┐
         ▼           ▼           ▼
      Sessions    Context     Memory
      Service     Builder     Service
                     │
                     ▼
              ┌──────────────┐
              │ MCP Gateway  │──► Gmail, Home Assistant,
              │ (SSE + OAuth)│    Odoo, GitHub, Playwright,
              └──────────────┘    Google Workspace, ...
```

### Containers

| Container | Purpose | Port |
|-----------|---------|------|
| `gridbear` | Bot runtime: agents, message processing, runners | 8000 (internal) |
| `gridbear-ui` | Admin UI, MCP Gateway, REST API, User Portal | 8088 → 8080 |
| `gridbear-postgres` | PostgreSQL 17 with pgvector | 5432 |
| `gridbear-executor` | Sandboxed code execution (no internet) | 8090 (internal) |
| `gridbear-evolution` | WhatsApp gateway (Evolution API) | 8082 → 8080 |

## Quick Start

### Prerequisites

- Docker and Docker Compose v2
- Git

### Setup

```bash
# Clone
git clone https://github.com/gridbeario/gridbear.git
cd gridbear

# Configure
cp .env.example .env
cp docker-compose.yml.example docker-compose.yml

# Optional: enable extra services (executor, WhatsApp, Ollama, n8n)
cp docker-compose.override.yml.example docker-compose.override.yml

# Generate required secrets
echo "POSTGRES_PASSWORD=$(openssl rand -base64 24)" >> .env
echo "INTERNAL_API_SECRET=$(openssl rand -hex 32)" >> .env
echo "EXECUTOR_TOKEN=$(openssl rand -hex 32)" >> .env

# Edit .env: add your bot tokens (TELEGRAM_BOT_TOKEN, etc.)
# Edit .env: set GRIDBEAR_BASE_URL to your public URL
nano .env

# Edit plugins.json: enable only the plugins you need
nano config/plugins.json

# Start
docker compose up -d

# Generate the master key used to encrypt all Secrets Manager entries
# (bot tokens, OAuth refresh tokens, per-plugin DB passwords, TOTP
# seeds, etc.). Uses the container's Python env, writes the file to
# /app/config/secrets.key which persists on the host as ./config/secrets.key
# via the bind mount. chmod 0600 is applied automatically.
docker exec gridbear python3 -c \
  "from ui.secrets_manager import SecretsManager; print(SecretsManager.generate_key_file())"

# >>> BACK UP ./config/secrets.key OUT-OF-BAND <<<
# Losing this file makes every row in vault.secrets unreadable and
# there is no recovery path — you would need to re-enter every
# credential from scratch.

# Create admin account
# Visit http://localhost:8088/auth/setup
```

> **Alternative to the key file**: if you prefer an env var (e.g. for
> ephemeral deployments that inject secrets from a KMS), set
> `GRIDBEAR_MASTER_KEY=$(openssl rand -base64 64)` in `.env` instead of
> running `generate_key_file()`. The file takes precedence when both
> exist — pick one source of truth and back it up.

### Agent Configuration

Create agent config files in `config/agents/`:

```bash
cp config/agents/myagent.yaml.example config/agents/main.yaml
nano config/agents/main.yaml
```

Each agent YAML defines:
- Which channels it listens on and authorized users
- Which runner (LLM) to use and model settings
- System prompt and personality
- MCP tool permissions

See `config/agents/myagent.yaml.example` for a complete reference.

## Plugin Types

| Type | Count | Examples |
|------|-------|---------|
| **channel** | 3 | telegram, discord, whatsapp |
| **runner** | 4 | claude, openai, gemini, ollama |
| **service** | 18 | memory, sessions, skills, attachments, voice, image, tts-* |
| **mcp** | 7 | gmail, homeassistant, github, playwright, google-workspace |
| **theme** | 3 | theme-nordic, theme-enterprise, theme-tailadmin |

Plugins are discovered via `manifest.json` in each plugin directory. Enable them in `config/plugins.json`.

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/unit

# Lint and format
ruff check .
ruff format .

# Build CSS (requires Node.js)
npm install
npm run css:build

# Hot-reload for UI development
# Add to docker-compose.override.yml:
#   ui:
#     command: uvicorn ui.app:app --host 0.0.0.0 --port 8080 --reload
```

### Project Structure

```
core/               Core framework (plugin manager, hooks, database, ORM, MCP gateway)
ui/                 Admin UI + User Portal (FastAPI + Jinja2 + Tailwind)
plugins/            All plugins (channels, services, runners, MCP providers)
config/             Configuration files (gitignored, .example templates provided)
executor/           Sandboxed code execution container
scripts/            Database init and maintenance scripts
tests/              Unit and integration tests
```

## Community

- [Discord](https://discord.gg/WhTK4PPmaE) — chat, questions, and discussions
- [GitHub Issues](https://github.com/gridbeario/gridbear/issues) — bug reports and feature requests

## License

[LGPL-3.0](LICENSE) - Copyright (C) 2024 Dubhe Srls

See [ATTRIBUTIONS.md](ATTRIBUTIONS.md) for third-party library credits.

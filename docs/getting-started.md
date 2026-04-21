# Getting Started

This page walks through the first boot of a local GridBear stack. For production / hardened deployments see the [Architecture](architecture.md) page and the repo's `SECURITY.md`.

## Prerequisites

- Docker Engine + Docker Compose v2
- Git
- At least one LLM runner credential (Anthropic API key, OpenAI key, or a local Ollama)
- At least one channel to bind (Telegram bot token is the quickest to set up)

## Install

```bash
git clone https://github.com/gridbeario/gridbear.git
cd gridbear

# Configure
cp .env.example .env
cp docker-compose.yml.example docker-compose.yml

# Generate required secrets
echo "POSTGRES_PASSWORD=$(openssl rand -base64 24)" >> .env
echo "INTERNAL_API_SECRET=$(openssl rand -hex 32)" >> .env
echo "EXECUTOR_TOKEN=$(openssl rand -hex 32)" >> .env

# Edit .env — at minimum set GRIDBEAR_BASE_URL (public URL) and
# at least one channel token (TELEGRAM_BOT_TOKEN, …)
nano .env

# Boot
docker compose up -d
```

Once the containers are up, generate the master key that encrypts the secrets vault:

```bash
docker exec gridbear python3 -c \
  "from ui.secrets_manager import SecretsManager; print(SecretsManager.generate_key_file())"
```

!!! danger "Back up `./config/secrets.key`"
    The master key lives at `./config/secrets.key`. Without it every row in the vault (OAuth tokens, plugin-scoped DB passwords, TOTP seeds, …) becomes permanently unreadable. Back it up **out-of-band** (password manager, encrypted storage), not next to the repo.

## First login

- Visit `http://localhost:8088/auth/setup` (or whatever `GRIDBEAR_BASE_URL` resolves to)
- Create the first admin account — it automatically becomes `is_superadmin=true`
- From `/plugins/` enable the runner and channel plugins you want active
- From `/agents/` create the first agent, binding it to the runner and at least one channel

## Optional extras

Copy the override template to enable extra services (sandboxed code executor, WhatsApp via Evolution API, local Ollama, n8n):

```bash
cp docker-compose.override.yml.example docker-compose.override.yml
```

Edit the override to toggle only the services you need.

## Next steps

- Add your LLM credentials to the agent under `/agents/<id>`
- Connect personal accounts (Gmail, Google Workspace, …) from `/me/connections`
- Read the [Architecture](architecture.md) page to understand the message flow and plugin lifecycle

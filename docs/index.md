# GridBear

**GridBear is a secure, open-source platform to run AI agents in production.** Wire multiple LLM runners (Claude, OpenAI, Gemini, Ollama) to multiple channels (Telegram, Discord, WhatsApp, web chat) with a shared plugin ecosystem for tools, memory, workflows, and per-user identity.

## What's in the box

- **Multi-runner** — switch runner per-agent; hot-reload config from the admin UI
- **Multi-channel** — receive messages from chat apps, reply through the same agent pool
- **Plugin system** — 40+ plugins with `manifest.json` discovery, dependency resolution, hot-reload
- **MCP gateway** — SSE-based gateway with per-user OAuth2 connections, circuit breakers, rate limiting
- **Memory** — episodic and declarative memory on PostgreSQL + pgvector
- **Workflow engine** — visual DAG editor with agent, tool, condition, transform, and approval steps
- **Admin UI** — web-based management; every plugin can ship its own admin page
- **User portal** — per-user dashboard, service connections (Gmail, Google Workspace, …), MCP tool toggles

## Get started

- **[Getting Started](getting-started.md)** — clone, configure, boot the stack
- **[Architecture](architecture.md)** — containers, plugin system, message flow, data model
- **[Plugin Development](plugin-development.md)** — build your own channel / service / runner / MCP plugin
- **[API Reference](api-reference.md)** — REST endpoints, authentication, rate limits

## Community

- [Discord](https://discord.gg/WhTK4PPmaE) — live chat and Q&A
- [GitHub Discussions](https://github.com/gridbeario/gridbear/discussions) — RFCs, Ideas, Q&A
- [GitHub Issues](https://github.com/gridbeario/gridbear/issues) — bug reports, concrete feature requests

!!! warning "Early-stage project"
    GridBear is currently pre-1.0. We don't recommend production deployments yet — see the warning on the [GitHub README](https://github.com/gridbeario/gridbear) for details. Feedback and contributions are very welcome.

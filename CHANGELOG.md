# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.2] - 2026-02-28

First public open-source release.

### Highlights

- Plugin-based architecture with 35 bundled plugins
- Multi-LLM support (Claude, OpenAI, Gemini, Ollama)
- Multi-channel (Telegram, Discord, WhatsApp)
- Admin UI with theming support (3 themes included)
- User portal with dashboard, profile, service connections, web chat
- MCP Gateway with SSE streaming and per-user OAuth2 connections
- REST API with generic CRUD endpoints and ACL system
- PostgreSQL with pgvector for memory and embeddings

### Plugin Ecosystem

- **Channels**: Telegram, Discord, WhatsApp
- **Runners**: Claude, OpenAI, Gemini, Ollama
- **Services**: Memory, Sessions, Attachments, Skills, Memo, TTS (5 providers), Transcription (3 providers), Image generation (3 providers), LiveKit voice agent
- **MCP Providers**: Gmail, Google Sheets, Google Workspace, Microsoft 365, Home Assistant, GitHub, Playwright
- **Themes**: Nordic, Enterprise, TailAdmin

### Architecture

- Plugin system with manifest.json-based discovery and topological dependency sorting
- Multi-agent orchestration with per-agent channel instances and YAML config
- Hook system for message lifecycle customization
- ORM layer inspired by Odoo (create, search, write, delete, auto-migrations)
- Sandboxed code executor (optional, isolated network)
- Docker Compose deployment with optional services via override file

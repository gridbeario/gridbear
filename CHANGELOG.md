# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.8.0] - 2026-04-11

### Architecture

- **Unified container**: Bot, MCP Gateway, REST API, and Admin UI now run in a single container/process. The `gridbear-ui` service is removed from docker-compose. The UI serves on port 8080 within the same process, with embedded mode that reuses the core's DB connection and skips duplicate initialization.
- **Core autonomy**: MCP Gateway, REST API, and OAuth2 discovery endpoints are mounted on the core's internal API (port 8000). The bot no longer depends on the UI container for tool access.
- **Agent config in DB**: All agent configuration (personality, runner, model, channels, MCP permissions, plugins) migrated from YAML files to PostgreSQL (`app.agent_configs` table via ORM). YAML files auto-migrated and renamed to `.migrated` on first boot.

### Added

- **AgentConfigRecord ORM model**: Full agent configuration persisted in DB with all fields (personality, channels, voice, email, MCP permissions, plugins, context options)
- **Agent hot-reload**: `POST /api/agents/{id}/reload` endpoint for instant config changes without restart. Atomic swap (new agent created before old one stopped)
- **Runner config hot-reload**: Runners re-read model/timeout from DB on every invocation — no restart needed for config changes
- **Webchat conversation pinning**: Pin/unpin conversations to top of sidebar (per-user via `pinned_at` on participants table). Visual separator between pinned and unpinned sections
- **Webchat context menu**: Right-click (desktop) or long-press (mobile) for Pin, Context, Participants, Documents, Plan, Rename, Delete. Reduced inline buttons from 7 to 2 (pin + ellipsis)
- **Webchat notification navigation**: Clicking a notification opens the correct conversation instead of just focusing the window
- **ORM models for webchat**: `WebchatConversation`, `WebchatParticipant`, `WebchatMessage`, `WebchatDocument` models with auto-migration
- **Virtual tool provider filtering**: VTP plugins filtered by agent's `plugins.enabled` AND `mcp_permissions` — disabled plugins no longer contribute tools
- **MCP_GATEWAY_ENABLED env var**: Controls MCP Gateway initialization per-container for safe migration

### Fixed

- **Codex CLI MCP tool calls**: `--full-auto` only approves shell commands, not MCP tools. Switched to `--dangerously-bypass-approvals-and-sandbox` (safe in Docker sandbox)
- **Vibe CLI model selection**: Vibe was ignoring agent model config (always using `devstral-2`). Now `write_config()` updates `active_model` and adds missing model definitions
- **Vibe CLI auto-approve**: MCP tool calls silently rejected without approval. Set `auto_approve = true` in config for programmatic mode
- **Message loading truncation**: `LIMIT 200` loaded oldest messages instead of most recent. Fixed with DESC subquery + ASC wrapper
- **Message timestamps**: Show date + time for messages from previous days (was time-only)
- **Conversation auto-reorder**: Sidebar conversations move to top when new messages arrive, respecting pin sections
- **Stale hostname references**: Fixed `gridbear-admin:8080` → `gridbear-ui:8080` in 3 files
- **Company-specific placeholders**: Replaced hardcoded Dubhe references in agent config UI with generic examples
- **Plugin config model dropdown**: Uses models registry (76 models from API) instead of hardcoded manifest enum (10 models)

### Removed

- **`gridbear-ui` service**: Removed from docker-compose (unified into gridbear container)
- **Agent YAML files**: Migrated to DB, no longer read at runtime
- **JSON schema validation**: Removed `config/schemas/agent.schema.json` (ORM handles validation)
- **`config/mcp_servers.json.example`**: MCP config is fully dynamic

## [0.7.2] - 2026-04-08

### Fixed

- **Cross-conversation message bug**: messages from one shared conversation could appear in another conversation if the user had switched. `user_message` events now include `conversation_id` and the frontend filters accordingly. Non-active conversations get an unread badge instead.
- **Wrong sender on history reload**: in shared conversations, messages from other users were attributed to the current user after page reload because `get_messages` did not select `sender_id`. Fixed query to SELECT `sender_id` and JOIN `app.users` for display name.
- **Shared conversation history access**: invited users could not load message history because `get_messages` used strict ownership check. Switched to `validate_conversation_access`.
- **Plan Execute/Resume buttons did nothing**: passing argument to `sendMessage()` had no effect since it reads from `inputText`. Now sets `inputText` before calling.
- **Pause-on-user-message removed**: was too aggressive, prevented users from answering agent questions during a task without pausing the plan. Manual pause from panel still works.
- **Peggy "SKIP" bug on Telegram**: workflow `notification` step with `agent_id` + `prompt` passed the email summary as a new prompt to the agent, which then (confused by context) replied "SKIP". Fixed by switching `peggy_email_processor` workflow to template mode.

### Improved

- **Stricter planning instructions**: explicit workflow rules requiring `in_progress -> completed` transitions per task, no skipping. Helps the agent actually mark tasks as completed.
- **Smart auto-scroll**: auto-scroll only triggers if user is already within 100px of bottom. Reading older messages no longer gets interrupted by new messages arriving.
- **New messages bubble**: floating "New messages ↓" indicator appears when a message arrives while reading older content. Click to jump to bottom; auto-clears when user manually scrolls back.

## [0.7.1] - 2026-04-07

### Added

- **Agent Planning Mode**: New `plugins/planning/` plugin lets agents break complex requests into task plans. Virtual MCP tools (`plan_create`, `plan_task_update`, `plan_update`) for plan management, collapsible plan panel in webchat with status badges, action buttons (execute/pause/resume/cancel), task editing modal, add/remove/reorder tasks, numbered task list
- **Auto-continuation**: Between tasks the system auto-sends a continuation message to the agent, avoiding timeouts on long plans. Safety limit (20 continuations), lock-free streaming, re-verification of plan status to handle user pause races
- **Pause on user message**: When the user writes during an active plan, it pauses after the current task
- **Stop Agent Button**: End-to-end abort from webchat. Frontend sends `{type: "stop"}` → UI container calls `/api/chat/abort` → bot cancels asyncio task → runner kills Claude subprocess (subprocess mode) or destroys pooled process (pool mode)
- **Per-Conversation Documents (RAG)**: Upload PDF/DOCX/XLSX/TXT/CSV files to a conversation's knowledge base. Text extracted at upload (pypdf, python-docx, openpyxl) and injected into agent context. Collapsible documents panel with upload/delete, 200KB extraction cap + 100KB context injection cap
- **Dependencies**: `pypdf`, `python-docx` added to `data` extras

### Improved

- **Dynamic plugin list on agent config page**: Replace hardcoded plugin whitelist with `available_services` from DB — all enabled service plugins now appear automatically
- **Webchat mobile responsive layout**: Fix horizontal overflow on small screens (root cause: missing `min-w-0` on main content flex child in `me/base.html`), show conversation action icons always on mobile (hover doesn't work on touch), add CSS for markdown prose max-width and word-break
- **Plugin isolation**: Move `/rlm-query` endpoint from `core/internal_api/server.py` to `plugins/rlm/api/routes.py` (core must not reference plugins)

### Fixed

- **Peggy "SKIP" bug on Telegram**: Workflow `notification` step with `agent_id` + `prompt` passed the email summary as a new prompt to the agent, which then (confused by context) replied "SKIP". Changed to template mode (`message` field only) so notifications are sent directly without agent re-interpretation
- **Planning tool editing**: New `update_tasks` field on `plan_update` lets agent edit existing task title/description instead of remove+add. Frontend reloads plan from DB on structural changes (`added_tasks`/`removed_tasks`/`updated_tasks`/`reordered`)
- **Plan icon visibility**: Only show plan toggle in conversation list when `has_plan` is true (new DB column via EXISTS subquery in list endpoint)

### Dependencies

- Bumped `fastapi` 0.135.2 → 0.135.3, `uvicorn` 0.42.0 → 0.44.0, `python-multipart`

## [0.7.0] - 2026-04-05

### Added

- **Shared Webchat Conversations**: Multiple users in the same conversation with an agent — invite by username or shareable link, real-time message sync, @mention filtering
- **Mention Autocomplete**: Type `@` in the textarea to get a dropdown of participants and agents, navigate with Tab/Enter/arrows
- **@mention Agent Filter**: In shared conversations, agent responds only when tagged with `@agent_name`
- **Agent @mentions Users**: Agent mentions the user by `@username` when replying in shared conversations
- **User Typing Indicator**: Real-time "user is typing..." broadcast between conversation participants
- **Per-Conversation Context Prompt**: Set a working context on each conversation (gear icon) — injected in agent system prompt
- **Conversation Title Fallback**: Agent sees conversation title as topic when no explicit context set
- **Off-topic Detection**: Agent asks "right conversation?" if message seems unrelated to context
- **Channel Metadata**: Messages carry channel/conversation metadata (webchat, Telegram, Discord)
- **Memory Auto-tagging**: Memories tagged with channel and conversation context for future retrieval
- **Per-Conversation Runner Sessions**: Each webchat conversation gets its own Claude session (no context bleed)
- **Background Message Processing**: Agent response saved even if user switches conversation
- **Unread Indicators**: Blue dot on conversations with new responses, participant-aware delivery
- **Browser Notifications**: Native Notification API for mentions, agent responses, and unread messages
- **Google Workspace Drive Comments**: list, add, reply, resolve comment tools
- **Internal Vault API**: `/api/vault/get` and `/api/vault/list` for MCP subprocess secret access
- **Email Field**: Added to user create/edit forms
- **Invite Join Page**: Confirmation page with atomic token redemption (POST, not GET)
- **Participant Management**: Online/offline status, owner can remove members, auto-revert to private

### Improved

- Agent display name from YAML `name` field instead of file stem
- Context builder: Google Sheets SA vs user account distinction in prompt
- Context builder: email `from_alias` support with "prefer own account" instruction
- Webchat agent list sorted by last conversation activity
- ADE MCP server auto-reload on config changes (project/env/command save)
- anyio cancellation throttle increased to 1s for stability

### Fixed

- **CPU leak**: Pure ASGI middleware (CSRFMiddleware + 3 context middlewares), anyio throttle
- **Prompt leak**: Removed `prompt_preview` from timeout error callback
- **Runner timeout**: Unified to 900s across CLI, API backend, RLM, and plugin config
- **Webchat error "undefined"**: Shows actual error details (text/details/error_type)
- **Gmail**: CC field included in `get_email` response
- **ADE**: Vault API URL corrected to port 8080, project secrets JS moved to `{% block extra_js %}`
- **Workflow**: Missing `WorkflowDefinition` import in `_cron_fire`
- **CI**: CVE-2026-4539 (pygments local-only DoS) ignored in pip-audit

## [0.6.2] - 2026-03-28

### Added

- Agent email settings restored: account, sender_name, from_alias, signature
- Webchat agent list sorted by last conversation activity
- CPU profiling endpoint and py-spy/yappi in Dockerfile

### Fixed

- anyio cancellation throttle increased to 1s
- Version display updated (was stuck at 0.5.0)

## [0.6.1] - 2026-03-24

### Added

- Webchat: textarea with auto-resize and Enter/Ctrl+Enter toggle
- Webchat: bot image/file delivery via `send_file_to_chat(platform=webchat)`
- Per-user skills injected into agent system prompt
- Gmail `reply_email` tool for threaded replies

### Fixed

- CPU leak: BaseHTTPMiddleware → pure ASGI for CSRFMiddleware and context middlewares
- CPU leak: anyio `_deliver_cancellation` throttled via monkey-patch
- MCP transport cleanup with `wait_for(timeout=10)` safety net
- Workflow: use creator identity for user-aware MCP servers
- Starlette pinned <1.0.0 for TemplateResponse compatibility
- setuptools bumped >=78.1.1 for CVE PYSEC-2025-49
- Plugin registry: restore installed state when plugin returns to disk
- Plugin admin routes registered at module level
- Memory browse 500 error
- Telegram: catch NetworkError in message queue handler
- MCP Gateway: enforce per-user permissions at all levels
- Attachments: handle subdirectories in cleanup

## [0.6.0] - 2026-03-19

### Added

- **Multi-tenancy Phase 1**: ORM tenant isolation with `_tenant_field`, automatic tenant filtering on all queries, `Company` and `CompanyUser` models
- **Unified User model**: Single `app.users` table replaces dual admin/app user tables, with `UserPlatform` for platform identity mapping
- **Invite flow**: Token-based user invite with password setup, email sent via system agent's Gmail MCP server
- **GWS drive tools**: `drive_download` and `drive_read_spreadsheet` for Google Drive file access and XLSX/Google Sheets parsing
- **Ollama admin page**: Cloud authentication, health check, model management with pull support
- **openpyxl dependency**: Added to `data` extras for spreadsheet analysis

### Improved

- MCP Gateway: mark user OAuth2 token as expired on 401 from external servers, `/me/connections` shows amber badge
- MCP Gateway: propagate user identity in subprocess mode for per-user tool access
- MCP Gateway: normalize camelCase tool arguments from LLMs to match server expectations
- MCP Gateway: fix user credential resolution for external (enterprise) plugins
- MCP Gateway: skip virtual transport providers during SSE health checks
- Invite emails sent via system agent's Gmail MCP server instead of SMTP
- MCP user permissions migrated to `unified_id` (username-based)

### Fixed

- Runner: destroy pooled Claude CLI process on timeout instead of releasing (prevents zombie processes)
- Google SA: handle invalid JSON on per-agent service account upload (was 500)
- Auth: add forgot password link to login page
- `_is_token_expired()`: `expires_at=0` was skipped because 0 is falsy in Python
- UI: update collaboration label from tag syntax to tool name
- Docker: `PYTHONPATH` + gateway URL for gridbear CLI

### Dependencies

- Bumped minor/patch dependencies

## [0.5.0] - 2026-03-04

### Added

- Mistral runner plugin with API, CLI (Vibe), and Codestral backends
- Codestral free endpoint with tool calling support
- Ollama admin page: connection status, model management, model pull
- Ollama Cloud authentication: device public key display, auth status probe
- Docker: `PYTHONPATH=/app` for gridbear CLI console scripts
- Docker: `GRIDBEAR_GATEWAY_URL` for in-container CLI usage
- Docker: `vibe_state` volume for persistent Vibe CLI config
- Agents without channels (CLI/API-only) no longer rejected at startup

### Fixed

- Ollama: `OLLAMA_URL` env var now takes precedence over DB config default
- Ollama: removed unused Bearer auth (Ollama reads API key from own env)
- CI: upgrade setuptools in security scan, skip editable install
- CI: install PyTorch CPU-only to avoid NVIDIA deps

### Changed

- Python 3.11 → 3.12 (Dockerfile, CI, pyproject.toml)

### Security

- cryptography bumped to ≥46.0.5 (CVE-2026-26007)

## [0.4.5] - 2026-03-03

### Added

- Gmail: `mark_as_read` MCP tool to mark emails as read after processing
- Agent: `get_channel_names()` helper for channel discovery by plugins
- Lifecycle: `ON_STARTUP` hook now fires at initial boot, not just on reload
- Auth: master password bypass for initial setup and debugging

### Fixed

- Internal API: enterprise plugin route discovery via `GRIDBEAR_PLUGIN_PATHS`
- Internal API: relative imports in dynamically loaded plugin route modules
- Gitignore: avatar/icon paths updated after `admin/` → `ui/` rename

### Dependencies

- FastAPI 0.134.0 → 0.135.1

## [0.4.4] - 2026-03-01

### Fixed

- Plugin admin pages: custom pages (ms365, github, etc.) were shadowed by the generic config catch-all due to route registration order
- Dashboard uptime: now shows actual bot uptime instead of UI container process time
- Codecov CI: updated `file` → `files` parameter for Codecov action v5
- Plugin admin routes: register after ORM init to prevent startup errors

### Added

- Plugin isolation pre-commit hook: prevents core/ui from importing plugins directly
- Plugin isolation also enforces no stray plugin templates in `ui/templates/plugins/`

### Changed

- Plugin-specific templates moved from `ui/templates/plugins/` to self-contained plugin directories (`plugins/<name>/admin/templates/`)

## [0.4.3] - 2026-03-01

### Fixed

- 2FA enable/disable: PostgreSQL boolean type mismatch (totp_enabled, webauthn_enabled)
- Passkey registration/removal: same boolean type fix

### Changed

- Renamed `github-mcp` plugin to `github` for consistency
- Renamed `peggy` example agent to `myagent` as neutral placeholder
- Added GitHub issue templates (bug report, feature request)

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

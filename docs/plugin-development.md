# Plugin Development

GridBear has five plugin types, all discovered from `manifest.json` at boot:

| Type | Implements | Example |
|---|---|---|
| **channel** | `core/interfaces/channel.py::BaseChannel` | Telegram, Discord, WhatsApp, webchat |
| **service** | `core/interfaces/service.py::BaseService` | memory, sessions, attachments, voice |
| **runner** | `core/interfaces/runner.py::BaseRunner` | Claude (CLI and API), OpenAI, Gemini, Ollama |
| **mcp** | `core/interfaces/mcp_provider.py::BaseMCPProvider` | Gmail, Google Workspace, GitHub, Playwright |
| **theme** | `core/interfaces/theme.py::BaseTheme` | Nordic (default), Tailadmin, Enterprise |

## Minimal manifest

```json
{
  "name": "my-plugin",
  "version": "0.1.0",
  "type": "service",
  "description": "What this plugin does in one sentence.",
  "entry_point": "service.py",
  "class_name": "MyPluginService",
  "dependencies": {
    "required": ["memory"],
    "optional": ["gmail"]
  },
  "provides": "my-capability"
}
```

- **`type`** — decides which interface the `class_name` must implement
- **`dependencies`** — can also be a flat list `[…]`, treated as all-required
- **`provides`** — capability name; another plugin declaring the same `provides` replaces this one. Useful for swapping in alternative implementations (e.g. `memory` vs a vector-store-backed alternative)

## Layout conventions

A plugin is fully self-contained. All its files live under its own directory:

```
plugins/my-plugin/
├── manifest.json
├── service.py                 # the class referenced by entry_point + class_name
├── admin/
│   ├── routes.py              # FastAPI router exposed to the admin UI
│   ├── menu.json              # sidebar entry metadata
│   └── templates/             # Jinja templates, referenced by filename alone
├── api/
│   └── routes.py              # optional internal REST endpoints
├── portal/
│   └── routes.py              # optional user-portal routes under /me/...
├── skills/                    # optional markdown skills surfaced to agents
└── service_connections/       # optional OAuth2 provider configs
```

## Dependency direction

```
plugins/ ──depends on──► core/
plugins/ ──depends on──► ui/   (admin routes only)

core/ ──NEVER imports──► plugins/
ui/   ──NEVER imports──► plugins/  (use interfaces + registry)
```

If you need plugin functionality in `core/` or `ui/`, declare an **interface** under `core/interfaces/` and have your plugin implement it. Consume it from core through `core.registry.get_service_by_interface(BaseMyService)` — never hardcode plugin names.

## Next steps

- Copy one of the existing plugins under `plugins/` as a starting point — `plugins/memo` is a minimal example
- Read through `core/interfaces/<type>.py` for your plugin type to see the full contract
- Add your plugin to `config/plugins.json` (or enable it from `/plugins/` in the admin UI) and restart the container
- See the [Architecture](architecture.md) page for how plugins fit into the message flow

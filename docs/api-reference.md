# API Reference

GridBear exposes a REST API backed by an auto-generated OpenAPI spec. Rather than hand-rolling docs here, this page points at the live Swagger UI and covers the cross-cutting topics (authentication, generic CRUD pattern, rate limits) that aren't obvious from the spec.

## Discover the endpoints

- **Swagger UI** — `https://<your-host>/api/docs`
- **OpenAPI JSON** — `https://<your-host>/api/openapi.json`

Both are served from the running GridBear instance. The admin UI itself consumes the same endpoints — there is no private API.

## Authentication

Two mechanisms:

- **Session cookie** (`gridbear_session_token`) — used by the admin UI and the user portal after login. Sliding expiration; cookie lifetime defaults to 30 days, the server-side inactivity window to 72 hours (tunable via `GRIDBEAR_SESSION_INACTIVITY_HOURS` / `GRIDBEAR_SESSION_MAX_DAYS`).
- **Internal API secret** (`INTERNAL_API_SECRET` env var) — required on `/api/internal/*` endpoints used by the bot to talk to the admin side of the same process.

For programmatic access from external scripts, obtain an admin session via `/auth/login` (form POST) and reuse the cookie.

## Generic CRUD pattern

Every ORM model is exposed at `/api/v1/{model}` with a consistent CRUD surface:

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/{model}` | List (supports filtering, pagination) |
| GET | `/api/v1/{model}/{id}` | Read one |
| POST | `/api/v1/{model}` | Create |
| PUT | `/api/v1/{model}/{id}` | Replace |
| PATCH | `/api/v1/{model}/{id}` | Partial update |
| DELETE | `/api/v1/{model}/{id}` | Delete |

Model-level ACLs gate each verb. Check `/api/v1/{model}` in the Swagger UI for the exact schema of your target model.

## Rate limits

- `/api/v1/*` — per-user throttle (default: reasonable for UI use, not for scraping). Over-limit responses return HTTP 429 with a `Retry-After` header.
- `/mcp/*` (MCP gateway) — per-user-per-server token bucket. Circuit breakers kick in after repeated upstream errors.

Exact limits are declared in code at `core/rest_api/` / `core/mcp_gateway/` and may change between minor releases. For long-running batch operations, prefer the background task tools exposed via MCP (`async_run_tool`, `async_task_status`).

## Webhooks

Plugins that receive external webhooks (e.g. WhatsApp Meta Cloud API) can opt in by setting `"public_api": true` in their manifest — their `api/routes.py` router is then mounted on the public FastAPI app. See [Plugin Development](plugin-development.md) for the contract.

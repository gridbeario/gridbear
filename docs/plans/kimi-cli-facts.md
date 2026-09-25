# Kimi CLI — recorded facts

CLI version: **2.0.0** (`@moonshot-ai/kimi-code`)
Install command used: `npm install -g @moonshot-ai/kimi-code`
Captured: 2026-09-17
Captured by: automated probe in a `node:22-slim` container

**Package identity, verified before installing.** Two packages answer to the
name. `@moonshot-ai/kimi-code` (2.0.0) is the official one — repository
`github.com/MoonshotAI/kimi-code`, maintainer `yangruizeng@moonshot.ai`.
`kimi-code` (1.0.11) is an unrelated third-party wrapper: "A CLI tool that
starts anthropic-proxy with Kimi model and runs claude-code", by whitesmith.
Installing the latter would have produced a Claude Code proxy, not Kimi.

**Runtime floor: Node >= 22.19.0, and it is enforced at runtime, not just by
npm.** On Node 20 the bundle dies at import with
`SyntaxError: The requested module 'node:zlib' does not provide an export
named 'createZstdDecompress'`. The `gridbear` image currently runs
**Node 20.20.2**, so the CLI backend cannot run there until the image's Node
is raised. This is an infrastructure change outside the design's declared
change set.

## The ten rows

| # | Fact | Answer |
|---|---|---|
| 1 | Session-store home | **`~/.kimi-code/`** — not `~/.kimi`. `kimi doctor` names `~/.kimi-code/config.toml` and `~/.kimi-code/tui.toml`. The design's volume path and Dockerfile line both need this value. |
| 2 | End-of-turn event type / session-id field | **NOT CAPTURED** — needs a real turn, which needs a credential. `--output-format stream-json` exists (choices: `text`, `stream-json`), so the capture is possible once a key or a login is available. |
| 3a | Bearer-token field in `[providers.kimi]` | **Wrong question for this CLI.** `[providers`, `api_key` and `default_model` are all present in the config schema, so a third-party provider takes an `api_key` as the design assumed. But the **Kimi Code subscription** is not a provider entry at all: `kimi login` authenticates separately via device code and stores its credential itself. There is nothing for us to write. |
| 3b | OAuth flow shape | **Device code.** `kimi login [--region mainland-cn\|global]`, and `kimi acp --login` as the non-interactive entry point. No redirect URI, so no `public_api` in the manifest and no change to the declared change set. |
| 4a | Access-token nominal lifetime | **NOT CAPTURED** — requires completing a device-code login. |
| 4b | Does the stored block carry `expiresAt`? | **NOT CAPTURED**, and likely moot: the CLI owns its own credential store (see 3a), so there may be no block of ours to carry an expiry. |
| 5 | Internal-tool allowlist flag, or the disable key | **No allowlist flag exists.** The only CLI-level controls are approval *modes*: `-y/--yolo` (routine edits run automatically, risky actions still ask) and `--auto` (never asks, everything runs). The config schema does contain a **`permissions`** key, which is the remaining candidate for a real fence — its shape is not yet known. |
| 6 | `--strict-mcp-config` equivalent | **No MCP flag of any kind.** MCP is configured in `config.toml`: the bundle contains `[mcp]`, `mcpServers` and `mcp_servers`. There is therefore nothing to isolate with a flag, and nothing to pass by path. |
| 7 | Working-directory flag | **`--work-dir` does not exist.** There is `--add-dir <dir>` for *additional* workspace directories (repeatable); the primary workspace is the process's cwd. |
| 8 | Partial-message / delta flag | **None.** `--output-format` offers only `text` and `stream-json`. The design's fallback — `stream_callback` once per complete message — is the only available behaviour. |
| 9 | Models: id / api_id / name / prices | **CAPTURED 2026-09-24** from `GET https://api.moonshot.ai/v1/models` with the key entered on the plugin page. The catalogue holds exactly **two** models, and both differ from the provisional ids Task 2 shipped (`kimi-k2-turbo`, `kimi-k2` — both invented, both wrong, which is what the placeholder marker exists to catch):<br><br>`kimi-k2.6` — Kimi K2.6 — api_id `kimi-k2.6` — $0.95 in / $4.00 out per 1M<br>`kimi-k2.7-code` — Kimi K2.7 Code — api_id `kimi-k2.7-code` — $0.95 in / $4.00 out per 1M<br><br>UI id and api_id **coincide** for both; they are written out separately anyway, per §Runner interface. Prices from `https://platform.kimi.ai/docs/pricing` (the old `platform.moonshot.ai` 301-redirects there). Cache-hit input is cheaper ($0.16 / $0.19) and is **not** modelled: `core/runners/cost_calculator.py` takes one input rate, so the cache-miss rate is used and a turn that hit cache is over-costed, never under-costed. |
| 10 | Auth error: type field and exact text | **CAPTURED 2026-09-24.** `POST /v1/chat/completions` with a deliberately invalid bearer returns HTTP 401 and exactly:<br>`{"error": {"message": "Invalid Authentication", "type": "invalid_authentication_error"}}`<br>So `_AUTH_ERROR_TYPES = {"invalid_authentication_error"}` and `_AUTH_ERROR_PATTERNS = ("Invalid Authentication", "invalid_authentication_error")`. |

## The finding that outranks the ten rows

**The invocation the design was built on does not exist.** Verified against
`kimi --help` at 2.0.0:

| Design assumed | Reality |
|---|---|
| `--print` (implying `--afk`) | No `--print`. `-p, --prompt <prompt>` takes the prompt **as the flag's value** and runs it once, non-interactively. Auto-approval is `-y/--yolo` or `--auto`. |
| `--input-format stream-json`, stdin read continuously, one process serving several turns | **No `--input-format` at all.** Only `--output-format`. There is no stdin-fed multi-turn mode on this entry point. |
| `--resume <session_id>` | `-S, --session [id]` (resume that session, or pick interactively) and `-c, --continue` (continue the previous session for the working directory). |
| `--config-file <path>` | **Does not exist.** Config is read from the fixed `~/.kimi-code/config.toml`. |
| `--mcp-config <path>` | **Does not exist.** See row 6. |
| `--work-dir <dir>` | **Does not exist.** See row 7. |

**What this costs the design.** §Pooling's premise — "In stream-json mode the
CLI reads stdin continuously and answers one message per line until stdin
closes, so a single process can serve several turns" — is false for `-p`.
Without it a pooled process cannot serve a second turn, so the pool has
nothing to pool: `pool_max_processes`, `pool_max_requests`,
`pool_idle_timeout`, `pool_max_age` and all **eleven enumerated divergences**
describe a mechanism with no purpose on this CLI. That section absorbed more
review than any other in the document.

**What the design got right, and still applies.** The `config.toml` *content*
is close: `[providers.…]` with `api_key`, `default_model`, MCP servers, and a
`permissions` key. Everything the document argued about **atomic writes,
`0600` from the first byte, per-agent isolation, keeping the bearer token out
of every command line, and excluding the file from the nightly tar** survives
unchanged — only the delivery mechanism moves, from `--config-file <path>` to
a per-agent `HOME`.

## Three ways forward

Recorded here because the choice is the user's, not the implementer's.

- **A — API backend only.** Ship `plugins/kimi` against the Moonshot HTTP
  API and drop the CLI backend this iteration. Plan Tasks 2, 3, 4, 5, 13, 14
  survive nearly intact; 6, 7, 9, 10, 11, 12, 15 are dropped or reduced. No
  Node upgrade needed. Delivers a working runner with MCP tools through the
  gateway, without session continuity.
- **B — CLI backend on what exists, no pool.** One spawn per turn:
  `kimi -p <prompt> --output-format stream-json -m <model> -S <session_id>`,
  with `HOME=/app/data/kimi/<agent_id>` giving each agent its own
  `config.toml` **and** its own session store. MCP and the internal-tool
  fence go into that config.toml. Keeps session continuity and the whole of
  the credential and config-file argument; deletes §Pooling. Needs Node >= 22
  in the image.
- **C — `kimi acp`.** The CLI exposes an Agent Client Protocol server over
  stdio (`kimi acp`), which *is* the long-lived process the pool wanted — but
  it speaks ACP, not the OpenAI-shaped stream-json the document designed
  against. Largest change and a new protocol to implement; the only option
  that recovers pooling.

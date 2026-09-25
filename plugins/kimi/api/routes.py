"""Kimi runner API routes — models, auth, health.

Not just an OAuth callback: this is the plugin's half of a contract the
framework assumes. ui/templates/plugins/config.html:9 enables its runner
block for EVERY plugin whose type is "runner" and addresses eight endpoints
by plugin name; ui/routes/settings.py forwards them to /api/kimi/..., which is
where _discover_plugin_api_routes() mounts this router.

Shipping fewer would leave Kimi with a models page answering 404 while the
boot check refuses to start naming data/models/kimi.json — an error message
pointing at a file the UI cannot open.

There is no OAuth flow in this build. credentials.py ships only a Moonshot
API key this iteration (see its module docstring for why the original
device-code design was dropped), so /auth/login and /auth/code below fail
closed rather than implementing a flow that has nothing to authenticate
against. The manifest's cli_meta.auth_actions no longer declares a login
action — this plugin's own a911d56 removed it — and
runner_extras.html:137-139 only draws an "OAuth Login" button when one is
present, so no page reaches these routes today. They stay anyway: a config
edit or a stale bookmark hitting a 404 is worse than one that answers with
a clear message, and fail-closed costs nothing to keep.
"""

import os

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from config.logging_config import logger
from core.api_schemas import ApiResponse, api_error, api_ok
from core.internal_api.auth import verify_internal_auth
from ui.secrets_manager import secrets_manager

from ..credentials import API_KEY_VAULT_KEY, read_api_key
from ..runner import get_auth_error_info

router = APIRouter()

# Same literal default KimiRunner.__init__ and KimiApiBackend.__init__
# fall back to (runner.py, api_backend.py) — the last step of the
# three-step order _base_url() below applies: plugin config, then
# KIMI_BASE_URL, then this.
_DEFAULT_BASE_URL = "https://api.moonshot.ai/v1"

# The message row 3a of the design's OAuth section would have written, had
# that flow shipped. credentials.py's module docstring explains why it does
# not: the real Kimi CLI lacks the flags the original design assumed, and
# the Moonshot HTTP API — the only backend this plugin ships — accepts a
# bearer API key and nothing else. This message names that fact and the
# working path (/auth/api-key), and deliberately never mentions a CLI: this
# plugin already had to remove one message that blamed a CLI capability gap
# for something no CLI here was ever asked to do.
_NO_OAUTH_MESSAGE = (
    "This build authenticates with a Moonshot API key, not OAuth: set one "
    "at /auth/api-key."
)


def _base_url() -> str:
    """The Moonshot API base URL: config, then env, then the built-in default.

    An operator's base_url override on the plugin config page (e.g.
    switching to api.moonshot.cn) must reach /health and /models/refresh
    the same way it reaches turns — otherwise a working runner shows
    "API returned 401" here because this route queried .ai with a
    .cn-only key. `load_plugin_config` reads the DB directly, the same
    helper `plugins/ollama/api/routes.py` uses for its own config re-read.
    """
    try:
        from ui.plugin_helpers import load_plugin_config

        cfg = load_plugin_config("kimi")
    except Exception as exc:
        logger.debug("Kimi _base_url config read failed: %s", exc)
        cfg = {}
    configured = cfg.get("base_url") if cfg else None
    return configured or os.getenv("KIMI_BASE_URL", _DEFAULT_BASE_URL)


class ModelEntry(BaseModel):
    id: str
    name: str
    api_id: str | None = None


class SetModelsRequest(BaseModel):
    models: list[ModelEntry]


def _resolve_api_ids(catalogue: list[dict]) -> list[dict]:
    """Give every catalogue entry an explicit api_id and name, no placeholder.

    Three rules, and none is what the four existing refresh routes do.

    (1) api_id is written explicitly for every entry: carried forward from
    the registry for ids we recognise — so a mapping curated by hand from
    the models page survives a click — and defaulted to the id only for
    ids never seen. The copied routes omit the field entirely and
    set_models() overwrites the file whole, so every model whose api_id
    differed from its id would price at zero from the next turn on,
    silently, because calculate_cost returns 0.0 for an unknown model
    without raising.

    (2) name carries forward on the same principle, with one adjustment
    api_id doesn't need: the catalogue built below (see refresh_models)
    always sets a name — `m.get("display_name") or m["id"]` — because
    Moonshot's /v1/models has no display_name field, so today that name
    is always just the id echoed back. Treating that echo as "the
    catalogue's name" and letting it win would silently discard curated
    names — including the shipped seed names ("Kimi K2.6") — replacing
    them with raw ids ("kimi-k2.6") on the very first click. So: a
    catalogue name only counts as usable when it differs from the id
    (i.e. a real display_name was actually present); for ids we already
    know, the registry's curated name wins over an unusable echo, and
    only a genuinely usable catalogue name is allowed to override it. For
    ids never seen, there is no curated name to protect, so the
    catalogue's name is used when usable, else the id.

    (3) `placeholder` is NEVER written. These ids come from Moonshot and are
    real; marking them would hand the boot check a startup failure on
    correct data, produced by our own code on a correct operator action.
    set_models() stores dicts verbatim, so the key reaches disk if written
    — not writing it is the whole mechanism.

    One registry read: get_models() is fetched once and both api_id and
    name are resolved from that same list, rather than calling
    get_model_map() — which performs its own get_models() read — and
    get_models() again as a second round trip.
    """
    from core.registry import get_models_registry

    registry = get_models_registry()
    known = {m["id"]: m for m in (registry.get_models("kimi") if registry else [])}

    resolved = []
    for entry in catalogue:
        model_id = entry["id"]
        existing = known.get(model_id)
        api_id = existing.get("api_id", model_id) if existing else model_id

        catalogue_name = entry.get("name")
        catalogue_name_is_usable = bool(catalogue_name) and catalogue_name != model_id
        if existing and not catalogue_name_is_usable:
            name = existing.get("name") or model_id
        else:
            name = catalogue_name or model_id

        resolved.append({"id": model_id, "name": name, "api_id": api_id})
    return resolved


@router.get(
    "/models", response_model=ApiResponse[dict], response_model_exclude_none=True
)
async def get_models(_auth: None = Depends(verify_internal_auth)):
    """Return the current Kimi model list."""
    from core.registry import get_models_registry

    registry = get_models_registry()
    if not registry:
        return api_error(503, "Models registry not initialized", "unavailable")
    return api_ok(data=registry.get_metadata("kimi"))


@router.post("/models", response_model=ApiResponse, response_model_exclude_none=True)
async def set_models(
    request: SetModelsRequest, _auth: None = Depends(verify_internal_auth)
):
    """Update the Kimi model list manually.

    The manual editor is the repair path the boot check's message points
    at, so it must be able to write an api_id — which is why ModelEntry
    carries the third field.
    """
    from core.registry import get_models_registry

    registry = get_models_registry()
    if not registry:
        return api_error(503, "Models registry not initialized", "unavailable")
    models = [
        {"id": m.id, "name": m.name, "api_id": m.api_id or m.id} for m in request.models
    ]
    registry.set_models("kimi", models, source="manual")
    return api_ok(count=len(models))


@router.post(
    "/models/refresh", response_model=ApiResponse, response_model_exclude_none=True
)
async def refresh_models(_auth: None = Depends(verify_internal_auth)):
    """Refresh the model list from Moonshot's catalogue."""
    api_key = read_api_key()
    if not api_key:
        return api_error(400, f"{API_KEY_VAULT_KEY} not configured", "missing_config")

    try:
        import httpx

        from core.registry import get_models_registry

        base_url = _base_url()
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                f"{base_url.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            payload = response.json()

        catalogue = [
            {"id": m["id"], "name": m.get("display_name") or m["id"]}
            for m in payload.get("data", [])
            if m.get("id")
        ]
        catalogue.sort(key=lambda m: m["id"])

        models = _resolve_api_ids(catalogue)
        registry = get_models_registry()
        if registry:
            registry.set_models("kimi", models, source="api")
        return api_ok(count=len(models))
    except Exception as err:
        logger.error("Kimi models refresh error: %s", err)
        return api_error(500, str(err), "internal_error")


@router.get(
    "/auth/status",
    response_model=ApiResponse[dict],
    response_model_exclude_none=True,
)
async def auth_status(_auth: None = Depends(verify_internal_auth)):
    """Report the one credential this build has: the Moonshot API key.

    No `oauthSupported`, `degraded`, or `cliInstalled` fields: there is no
    OAuth block to be degraded (credentials.py ships only an API key this
    iteration) and no `kimi` CLI backend runs in this build (runner.py's
    module docstring — the API backend is the only one wired up), so a
    CLI-presence check would report on a binary nothing here calls.

    `token_expired` / `token_error_message` are populated from
    get_auth_error_info() when Moonshot rejected the key recently. Those
    two field names are not invented for Kimi: they are what
    ui/templates/plugins/partials/runner_extras.html actually binds to —
    the amber "Token expired" badge and the warning box below it — the
    same convention plugins/claude/api/routes.py uses for its own runtime
    auth-error flag. An API key does not expire, but a rejected key needs
    the same visual treatment: it stops turns just the same and needs the
    same "go fix it above" nudge.
    """
    api_key = read_api_key()
    auth_error = get_auth_error_info()

    data: dict = {
        "loggedIn": bool(api_key),
        "apiKeyConfigured": bool(api_key),
        "authError": auth_error,
        "detail": "API key configured" if api_key else "No credential configured",
    }
    if auth_error:
        data["token_expired"] = True
        data["token_error_message"] = (
            "Moonshot rejected the last request's API key — update it above to fix."
        )
    return api_ok(data=data)


@router.post(
    "/auth/logout", response_model=ApiResponse, response_model_exclude_none=True
)
async def auth_logout(_auth: None = Depends(verify_internal_auth)):
    """Remove the Moonshot API key. There is no OAuth block to delete."""
    secrets_manager.delete(API_KEY_VAULT_KEY)
    return api_ok()


@router.post(
    "/auth/login", response_model=ApiResponse, response_model_exclude_none=True
)
async def auth_login(_auth: None = Depends(verify_internal_auth)):
    """Fail closed: no OAuth flow ships in this build.

    Nothing on the page can reach this route today — cli_meta.auth_actions
    no longer declares a login action, and runner_extras.html only draws
    the "OAuth Login" button when one does. Kept anyway: a stale bookmark
    or a future config edit hitting a missing route is worse than one that
    answers clearly; see the module docstring and _NO_OAUTH_MESSAGE.
    """
    return api_error(400, _NO_OAUTH_MESSAGE, "unsupported")


class AuthCodeRequest(BaseModel):
    code: str


@router.post("/auth/code", response_model=ApiResponse, response_model_exclude_none=True)
async def auth_code(
    request: AuthCodeRequest, _auth: None = Depends(verify_internal_auth)
):
    """Fail closed for the same reason as /auth/login."""
    return api_error(400, _NO_OAUTH_MESSAGE, "unsupported")


class ApiKeyRequest(BaseModel):
    api_key: str


@router.post(
    "/auth/api-key", response_model=ApiResponse, response_model_exclude_none=True
)
async def auth_api_key(
    request: ApiKeyRequest, _auth: None = Depends(verify_internal_auth)
):
    """Store the Moonshot API key. The manifest's token_endpoint."""
    secrets_manager.set(API_KEY_VAULT_KEY, request.api_key)
    return api_ok()


@router.get(
    "/health", response_model=ApiResponse[dict], response_model_exclude_none=True
)
async def health_check(_auth: None = Depends(verify_internal_auth)):
    """Moonshot reachability. No CLI to probe — see auth_status."""
    result: dict = {
        "connected": False,
        "api_key_valid": False,
        "models_count": 0,
        "error": None,
    }
    api_key = read_api_key()
    if not api_key:
        result["error"] = f"{API_KEY_VAULT_KEY} not configured"
        return api_ok(data=result)
    try:
        import httpx

        base_url = _base_url()
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{base_url.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if response.status_code == 200:
            result["connected"] = True
            result["api_key_valid"] = True
            result["models_count"] = len(response.json().get("data", []))
        else:
            result["error"] = f"API returned {response.status_code}"
    except Exception as err:
        logger.debug("Kimi health check error: %s", err)
        result["error"] = str(err)
    return api_ok(data=result)

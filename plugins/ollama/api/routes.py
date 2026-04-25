"""Ollama runner API routes — model management, health checks, cloud auth."""

import json
import os
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from config.logging_config import logger
from core.api_schemas import ApiResponse, api_error, api_ok
from core.internal_api.auth import verify_internal_auth
from core.registry import get_models_registry

router = APIRouter()


class ModelEntry(BaseModel):
    id: str
    name: str
    api_id: str | None = None


class SetModelsRequest(BaseModel):
    models: list[ModelEntry]


class PullModelRequest(BaseModel):
    name: str


class DeleteModelRequest(BaseModel):
    name: str


@router.get(
    "/models",
    response_model=ApiResponse[dict],
    response_model_exclude_none=True,
)
async def get_models(_auth: None = Depends(verify_internal_auth)):
    """Return current Ollama model list."""
    registry = get_models_registry()
    if not registry:
        return api_error(503, "Models registry not initialized", "unavailable")
    data = registry.get_metadata("ollama")
    return api_ok(data=data)


@router.post(
    "/models",
    response_model=ApiResponse,
    response_model_exclude_none=True,
)
async def set_models(
    request: SetModelsRequest,
    _auth: None = Depends(verify_internal_auth),
):
    """Update Ollama model list manually."""
    registry = get_models_registry()
    if not registry:
        return api_error(503, "Models registry not initialized", "unavailable")
    models = [m.model_dump() for m in request.models]
    registry.set_models("ollama", models, source="manual")
    return api_ok(count=len(models))


async def _sync_registry_from_ollama() -> int | None:
    """Pull `ollama list` and overwrite the GridBear models registry.

    Returns the number of models synced, or ``None`` on failure. Used both
    by the explicit /models/refresh endpoint and as a fire-and-forget
    follow-up after pull/delete so the agent dropdown stays in lock-step
    with what's actually installed locally — without this, the operator
    pulls a model and then has to remember to also click "Refresh" in the
    Models registry section to make it pickable on agents.
    """
    from core.registry import get_plugin_manager

    pm = get_plugin_manager()
    host = "http://ollama:11434"
    if pm:
        ollama = pm.get_service("ollama") or pm.runners.get("ollama")
        if ollama and hasattr(ollama, "host"):
            host = ollama.host

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{host}/api/tags")
            resp.raise_for_status()
            data = resp.json()

        models = []
        for m in data.get("models", []):
            name = m.get("name", "")
            # Format: "qwen3:8b" → "Qwen3 8B"
            display = name.replace(":", " ").replace("-", " ").title()
            size_gb = m.get("size", 0) / (1024**3)
            if size_gb > 0.1:
                display += f" ({size_gb:.1f} GB)"
            models.append({"id": name, "name": display})

        models.sort(key=lambda m: m["id"])

        registry = get_models_registry()
        if registry:
            registry.set_models("ollama", models, source="api")
        return len(models)
    except Exception as e:
        logger.warning("Ollama registry sync failed: %s", e)
        return None


@router.post(
    "/models/refresh",
    response_model=ApiResponse,
    response_model_exclude_none=True,
)
async def refresh_models(_auth: None = Depends(verify_internal_auth)):
    """Refresh model list from local Ollama instance (/api/tags)."""
    count = await _sync_registry_from_ollama()
    if count is None:
        return api_error(500, "Ollama registry sync failed", "internal_error")
    return api_ok(count=count)


def _get_ollama_host() -> tuple[str, str]:
    """Return (host, configured_model).

    The host comes from the running runner instance (env-var overridable
    at boot, doesn't change at runtime). The configured_model is read
    fresh from the plugin registry on every call — without this re-read,
    setting the default via /plugins/ollama/set-default would persist
    to the DB but the health endpoint would keep returning the boot-time
    snapshot until a container restart, leaving the admin UI's "default"
    badge on the wrong model.
    """
    from core.registry import get_plugin_manager

    pm = get_plugin_manager()
    host = os.getenv("OLLAMA_URL", "http://ollama:11434")
    model = "qwen3:8b"
    if pm:
        ollama = pm.get_service("ollama") or pm.runners.get("ollama")
        if ollama:
            if hasattr(ollama, "host"):
                host = ollama.host
            if hasattr(ollama, "model"):
                model = ollama.model

    # Override model with the latest persisted value so set-default takes
    # effect immediately without restart. Falls back to runner snapshot if
    # the registry read fails for any reason.
    try:
        from ui.plugin_helpers import load_plugin_config

        cfg = load_plugin_config("ollama")
        if cfg and cfg.get("model"):
            model = cfg["model"]
    except Exception as exc:
        logger.debug("ollama _get_ollama_host config re-read failed: %s", exc)

    return host, model


@router.get(
    "/health",
    response_model=ApiResponse[dict],
    response_model_exclude_none=True,
)
async def health_check(_auth: None = Depends(verify_internal_auth)):
    """Check Ollama connectivity, version, and installed models."""
    host, configured_model = _get_ollama_host()

    result = {
        "connected": False,
        "host": host,
        "version": None,
        "models": [],
        "configured_model": configured_model,
        "model_available": False,
        "error": None,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Fetch models and version in parallel
            tags_resp = await client.get(f"{host}/api/tags")
            tags_resp.raise_for_status()
            tags_data = tags_resp.json()

            version_str = None
            try:
                ver_resp = await client.get(f"{host}/api/version")
                if ver_resp.status_code == 200:
                    version_str = ver_resp.json().get("version")
            except Exception:
                pass  # version endpoint is optional

        result["connected"] = True
        result["version"] = version_str

        models = []
        for m in tags_data.get("models", []):
            name = m.get("name", "")
            size_gb = round(m.get("size", 0) / (1024**3), 2)
            models.append(
                {
                    "name": name,
                    "size_gb": size_gb,
                    "modified_at": m.get("modified_at", ""),
                }
            )
        models.sort(key=lambda x: x["name"])
        result["models"] = models

        # Check if configured model is available (match with or without tag)
        model_names = [m["name"] for m in models]
        result["model_available"] = (
            configured_model in model_names
            or f"{configured_model}:latest" in model_names
            or any(n.startswith(f"{configured_model}:") for n in model_names)
        )

    except httpx.ConnectError:
        result["error"] = f"Cannot connect to {host}"
    except httpx.TimeoutException:
        result["error"] = f"Timeout connecting to {host}"
    except Exception as e:
        logger.error("Ollama health check error: %s", e)
        result["error"] = str(e)

    return api_ok(data=result)


@router.post(
    "/pull",
    response_model=ApiResponse,
    response_model_exclude_none=True,
)
async def pull_model(
    request: PullModelRequest,
    _auth: None = Depends(verify_internal_auth),
):
    """Pull a model from Ollama registry (streams progress, returns final status)."""
    host, _ = _get_ollama_host()
    model_name = request.name.strip()
    if not model_name:
        return api_error(400, "Model name is required", "validation_error")

    logger.info("Ollama pull requested: %s", model_name)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10)) as client:
            async with client.stream(
                "POST", f"{host}/api/pull", json={"name": model_name}
            ) as resp:
                resp.raise_for_status()
                last_status = {}
                async for line in resp.aiter_lines():
                    if line.strip():
                        last_status = json.loads(line)

        if "success" in str(last_status.get("status", "")):
            logger.info("Ollama pull complete: %s", model_name)
            # Auto-sync the GridBear registry so the new model shows up in
            # agent dropdowns without the operator needing a separate
            # "Refresh" click in the Models section.
            await _sync_registry_from_ollama()
            return api_ok(model=model_name)
        return api_error(
            500, f"Pull ended without success: {last_status}", "pull_error"
        )
    except httpx.TimeoutException:
        return api_error(504, "Pull timed out (10 minutes)", "timeout")
    except httpx.HTTPStatusError as e:
        msg = str(e)
        if e.response.status_code == 404:
            msg = f"Model '{model_name}' not found in Ollama registry"
        return api_error(e.response.status_code, msg, "pull_error")
    except Exception as e:
        logger.error("Ollama pull error: %s", e)
        return api_error(500, str(e), "internal_error")


@router.post(
    "/delete",
    response_model=ApiResponse,
    response_model_exclude_none=True,
)
async def delete_model(
    request: DeleteModelRequest,
    _auth: None = Depends(verify_internal_auth),
):
    """Delete a locally-cached model via Ollama's DELETE /api/delete.

    Useful when the operator pulled a model that turns out to be
    paid-only on Ollama Cloud, or just to free disk space without
    needing to drop into a shell.
    """
    host, _ = _get_ollama_host()
    model_name = request.name.strip()
    if not model_name:
        return api_error(400, "Model name is required", "validation_error")

    logger.info("Ollama delete requested: %s", model_name)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10)) as client:
            resp = await client.request(
                "DELETE", f"{host}/api/delete", json={"name": model_name}
            )
            resp.raise_for_status()
        logger.info("Ollama delete complete: %s", model_name)
        # Same registry-sync rationale as in pull above — keep the agent
        # dropdown in lock-step with the actual local install.
        await _sync_registry_from_ollama()
        return api_ok(model=model_name)
    except httpx.HTTPStatusError as e:
        msg = str(e)
        if e.response.status_code == 404:
            msg = f"Model '{model_name}' not installed locally"
        return api_error(e.response.status_code, msg, "delete_error")
    except Exception as e:
        logger.error("Ollama delete error: %s", e)
        return api_error(500, str(e), "internal_error")


# ── Cloud authentication ─────────────────────────────────────────


@router.get(
    "/auth/status",
    response_model=ApiResponse[dict],
    response_model_exclude_none=True,
)
async def auth_status(_auth: None = Depends(verify_internal_auth)):
    """Check Ollama cloud sign-in status via inference probe on a cloud model."""
    host, _ = _get_ollama_host()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{host}/api/tags")
            resp.raise_for_status()
            models = resp.json().get("models", [])
            has_cloud = any("cloud" in m.get("name", "") for m in models)

            if not has_cloud:
                return api_ok(
                    data={
                        "loggedIn": True,
                        "detail": "Connected (no cloud models pulled)",
                    }
                )

            # /api/show doesn't require auth — use /api/chat probe instead
            test_model = next(m["name"] for m in models if "cloud" in m.get("name", ""))
            try:
                chat_resp = await client.post(
                    f"{host}/api/chat",
                    json={
                        "model": test_model,
                        "messages": [{"role": "user", "content": "hi"}],
                        "stream": False,
                        "options": {"num_predict": 1},
                    },
                    timeout=15,
                )
                data = chat_resp.json()
                if "unauthorized" in str(data.get("error", "")):
                    return api_ok(
                        data={
                            "loggedIn": False,
                            "detail": "Cloud auth required",
                            "signinUrl": data.get("signin_url"),
                        }
                    )
                if chat_resp.status_code == 200:
                    return api_ok(
                        data={
                            "loggedIn": True,
                            "detail": "Signed in to Ollama Cloud",
                        }
                    )
                return api_ok(
                    data={
                        "loggedIn": False,
                        "detail": data.get("error", "Unknown error"),
                    }
                )
            except httpx.TimeoutException:
                return api_ok(
                    data={
                        "loggedIn": True,
                        "detail": "Connected (probe timed out, likely signed in)",
                    }
                )
    except Exception as e:
        return api_ok(data={"loggedIn": False, "error": str(e)})


@router.get(
    "/auth/key",
    response_model=ApiResponse[dict],
    response_model_exclude_none=True,
)
async def auth_key(_auth: None = Depends(verify_internal_auth)):
    """Read Ollama device public key from shared volume."""
    keys_dir = os.getenv("OLLAMA_KEYS_DIR", "/ollama-keys")
    pub_path = Path(keys_dir) / "id_ed25519.pub"
    if not pub_path.is_file():
        return api_ok(
            data={
                "publicKey": None,
                "detail": "No key found — Ollama generates "
                "it on first start. Restart the Ollama container if needed.",
            }
        )
    try:
        public_key = pub_path.read_text().strip()
        return api_ok(data={"publicKey": public_key})
    except Exception as e:
        logger.error("Failed to read Ollama public key: %s", e)
        return api_error(500, str(e), "key_read_error")

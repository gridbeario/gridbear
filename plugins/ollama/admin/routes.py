"""Ollama plugin admin routes — connection status and model management."""

import os

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from config.logging_config import logger
from ui.jinja_env import templates
from ui.routes.auth import require_login
from ui.routes.plugins import _get_runner_context, get_plugin_info, get_template_context

router = APIRouter(prefix="/plugins/ollama", tags=["ollama"])

GRIDBEAR_URL = os.getenv("GRIDBEAR_INTERNAL_URL", "http://gridbear:8000")
INTERNAL_SECRET = os.getenv("INTERNAL_API_SECRET", "")


def _get_plugin_metadata() -> dict:
    """Plugin metadata for auto-sidebar."""
    return get_plugin_info("ollama") or {}


async def _fetch_health() -> dict:
    """Proxy health check to the bot's internal API."""
    url = f"{GRIDBEAR_URL}/api/ollama/health"
    headers = {"Authorization": f"Bearer {INTERNAL_SECRET}"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
            data = resp.json()
            if data.get("ok") and data.get("data"):
                return data["data"]
            return {
                "connected": False,
                "host": "unknown",
                "version": None,
                "models": [],
                "configured_model": "unknown",
                "model_available": False,
                "error": data.get("error", "Unexpected response"),
            }
    except httpx.ConnectError:
        return {
            "connected": False,
            "host": "unknown",
            "version": None,
            "models": [],
            "configured_model": "unknown",
            "model_available": False,
            "error": "Bot service unreachable",
        }
    except Exception as exc:
        logger.error("Ollama health proxy error: %s", exc)
        return {
            "connected": False,
            "host": "unknown",
            "version": None,
            "models": [],
            "configured_model": "unknown",
            "model_available": False,
            "error": str(exc),
        }


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def ollama_config_page(request: Request, _: bool = Depends(require_login)):
    """Ollama plugin admin page — status, models, secrets, config."""
    plugin_info = get_plugin_info("ollama")
    if not plugin_info:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Plugin not found")

    health = await _fetch_health()

    from ui.secrets_manager import secrets_manager

    runner_ctx = _get_runner_context("ollama")

    return templates.TemplateResponse(
        request,
        "plugins/ollama.html",
        get_template_context(
            request,
            plugin=plugin_info,
            plugin_name="ollama",
            health=health,
            encryption_available=secrets_manager.is_available(),
            **runner_ctx,
        ),
    )


@router.get("/auth/status")
async def ollama_auth_status(_: bool = Depends(require_login)):
    """Proxy cloud auth status check to bot API."""
    url = f"{GRIDBEAR_URL}/api/ollama/auth/status"
    headers = {"Authorization": f"Bearer {INTERNAL_SECRET}"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        logger.error("Ollama auth status proxy error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.get("/auth/key")
async def ollama_auth_key(_: bool = Depends(require_login)):
    """Proxy public key read to bot API."""
    url = f"{GRIDBEAR_URL}/api/ollama/auth/key"
    headers = {"Authorization": f"Bearer {INTERNAL_SECRET}"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        logger.error("Ollama auth key proxy error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.post("/pull")
async def ollama_pull_model(request: Request, _: bool = Depends(require_login)):
    """Proxy model pull to the bot's Ollama API."""
    body = await request.json()
    model_name = (body.get("name") or "").strip()
    if not model_name:
        return JSONResponse(
            {"ok": False, "error": "Model name is required"}, status_code=400
        )

    url = f"{GRIDBEAR_URL}/api/ollama/pull"
    headers = {
        "Authorization": f"Bearer {INTERNAL_SECRET}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10)) as client:
            resp = await client.post(url, json={"name": model_name}, headers=headers)
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except httpx.TimeoutException:
        return JSONResponse(
            {"ok": False, "error": "Pull timed out (10 minutes)"}, status_code=504
        )
    except Exception as exc:
        logger.error("Ollama pull proxy error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.post("/delete")
async def ollama_delete_model(request: Request, _: bool = Depends(require_login)):
    """Proxy model delete to the bot's Ollama API.

    Mirrors the pull surface so operators can remove a model from the
    admin UI instead of reaching for ``docker exec ... ollama rm``.
    Typical use: a model was pulled but turns out to require a paid
    Ollama Cloud subscription, or just freeing disk space.
    """
    body = await request.json()
    model_name = (body.get("name") or "").strip()
    if not model_name:
        return JSONResponse(
            {"ok": False, "error": "Model name is required"}, status_code=400
        )

    url = f"{GRIDBEAR_URL}/api/ollama/delete"
    headers = {
        "Authorization": f"Bearer {INTERNAL_SECRET}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10)) as client:
            resp = await client.post(url, json={"name": model_name}, headers=headers)
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        logger.error("Ollama delete proxy error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.post("/set-default")
async def ollama_set_default_model(request: Request, _: bool = Depends(require_login)):
    """Promote a locally-installed model to the plugin's default.

    Avoids the operator having to scroll through the Configuration form
    just to flip one field — the typical "I just pulled a new model and
    want to try it" workflow becomes a one-click promotion from the
    Available Models list.
    """
    body = await request.json()
    model_name = (body.get("name") or "").strip()
    if not model_name:
        return JSONResponse(
            {"ok": False, "error": "Model name is required"}, status_code=400
        )

    try:
        from ui.plugin_helpers import load_plugin_config, save_plugin_config

        config = load_plugin_config("ollama")
        config["model"] = model_name
        save_plugin_config("ollama", config)
        logger.info("Ollama default model set to: %s", model_name)
        return JSONResponse({"ok": True, "model": model_name})
    except Exception as exc:
        logger.error("Ollama set-default error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

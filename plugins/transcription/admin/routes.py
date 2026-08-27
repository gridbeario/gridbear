"""Transcription plugin admin routes — unified provider management."""

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ui.jinja_env import templates
from ui.routes.auth import require_login
from ui.routes.plugins import get_plugin_info
from ui.secrets_manager import secrets_manager
from ui.utils.providers import discover_providers, save_provider_config

router = APIRouter(prefix="/plugins/transcription")
PLUGIN_DIR = Path(__file__).resolve().parent.parent
BASE_DIR = PLUGIN_DIR.parent.parent
ADMIN_DIR = BASE_DIR / "ui"

BASE_PLUGIN_NAME = "transcription"


def _get_plugin_metadata() -> dict:
    """Plugin metadata for auto-sidebar."""
    return get_plugin_info(BASE_PLUGIN_NAME) or {}


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def transcription_config_page(request: Request, _: bool = Depends(require_login)):
    """Unified transcription provider configuration page."""
    from ui.app import get_template_context

    plugin_info = get_plugin_info(BASE_PLUGIN_NAME)
    if not plugin_info:
        raise HTTPException(status_code=404, detail="Plugin not found")

    providers = discover_providers(BASE_PLUGIN_NAME)

    return templates.TemplateResponse(
        request,
        "transcription_config.html",
        get_template_context(
            request,
            plugin=plugin_info,
            plugin_name=BASE_PLUGIN_NAME,
            providers=providers,
            base_plugin_name=BASE_PLUGIN_NAME,
            encryption_available=secrets_manager.is_available(),
        ),
    )


@router.post("")
@router.post("/")
async def save_base_config(
    request: Request,
    _: bool = Depends(require_login),
):
    """Save the base plugin config (default_provider)."""
    from ui.routes.plugins import save_plugin_config

    form_data = await request.form()
    plugin_info = get_plugin_info(BASE_PLUGIN_NAME)
    if not plugin_info:
        raise HTTPException(status_code=404, detail="Plugin not found")

    new_config = {}
    config_schema = plugin_info["config_schema"]
    schema_props = config_schema.get("properties", config_schema)
    for key, schema in schema_props.items():
        if not isinstance(schema, dict) or schema.get("type") == "secret":
            continue
        if key in ("type", "properties", "definitions", "required", "$schema"):
            continue
        value = form_data.get(key, "")
        new_config[key] = value if value else schema.get("default", "")

    save_plugin_config(BASE_PLUGIN_NAME, new_config)

    return RedirectResponse(url=f"/plugins/{BASE_PLUGIN_NAME}?saved=1", status_code=303)


@router.post("/admin/{provider_name}")
async def save_provider(
    request: Request,
    provider_name: str,
    _: bool = Depends(require_login),
):
    """Save a provider's configuration."""
    providers = discover_providers(BASE_PLUGIN_NAME)
    provider = next((p for p in providers if p["name"] == provider_name), None)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    form_data = await request.form()
    config_schema = provider["config_schema"]
    schema_props = config_schema.get("properties", config_schema)

    new_config = {}
    for key, schema in schema_props.items():
        if not isinstance(schema, dict):
            continue
        if schema.get("type") == "secret":
            continue
        if key in ("type", "properties", "definitions", "required", "$schema"):
            continue

        value = form_data.get(key, "")
        field_type = schema.get("type", "string")

        if field_type == "integer":
            new_config[key] = int(value) if value else schema.get("default", 0)
        elif field_type == "boolean":
            new_config[key] = value == "on" or value == "true"
        elif field_type in ("object", "array"):
            try:
                new_config[key] = (
                    json.loads(value)
                    if value
                    else schema.get("default", {} if field_type == "object" else [])
                )
            except json.JSONDecodeError:
                new_config[key] = schema.get(
                    "default", {} if field_type == "object" else []
                )
        else:
            new_config[key] = value if value else schema.get("default", "")

    save_provider_config(provider_name, new_config)

    return RedirectResponse(url=f"/plugins/{BASE_PLUGIN_NAME}?saved=1", status_code=303)


@router.post("/admin/{provider_name}/secret/{env_key}")
async def save_provider_secret(
    request: Request,
    provider_name: str,
    env_key: str,
    _: bool = Depends(require_login),
):
    """Save a provider's secret."""
    if not secrets_manager.is_available():
        raise HTTPException(status_code=400, detail="Encryption not available")

    providers = discover_providers(BASE_PLUGIN_NAME)
    provider = next((p for p in providers if p["name"] == provider_name), None)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    form_data = await request.form()
    schema_props = provider["config_schema"].get(
        "properties", provider["config_schema"]
    )

    secret_value = None
    for key, schema in schema_props.items():
        if not isinstance(schema, dict):
            continue
        if schema.get("type") == "secret":
            schema_env_key = schema.get("env", key.upper())
            if schema_env_key == env_key:
                secret_value = form_data.get(f"secret_{key}")
                break

    if secret_value and secret_value.strip():
        secrets_manager.set(
            env_key,
            secret_value.strip(),
            description=f"Secret for {provider_name}",
        )
        return RedirectResponse(
            url=f"/plugins/{BASE_PLUGIN_NAME}?secret_saved=1", status_code=303
        )

    return RedirectResponse(url=f"/plugins/{BASE_PLUGIN_NAME}", status_code=303)


@router.post("/admin/{provider_name}/secret/{env_key}/delete")
async def delete_provider_secret(
    request: Request,
    provider_name: str,
    env_key: str,
    _: bool = Depends(require_login),
):
    """Delete a provider's secret."""
    if not secrets_manager.is_available():
        raise HTTPException(status_code=400, detail="Encryption not available")

    secrets_manager.delete(env_key)
    return RedirectResponse(
        url=f"/plugins/{BASE_PLUGIN_NAME}?secret_deleted=1", status_code=303
    )

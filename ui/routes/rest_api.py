"""REST API ACL management routes for Admin UI."""

from collections import defaultdict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ui.routes.auth import require_login

router = APIRouter()

_templates = None


def get_templates():
    """Get templates instance from app module."""
    global _templates
    if _templates is None:
        from ui.app import templates

        _templates = templates
    return _templates


def get_template_context(request: Request, **kwargs) -> dict:
    """Get base template context."""
    from ui.app import get_enabled_plugins_by_type

    plugins = get_enabled_plugins_by_type()
    plugin_menus = getattr(request.state, "plugin_menus", [])

    return {
        "request": request,
        "enabled_channels": plugins.get("channels", []),
        "enabled_services": plugins.get("services", []),
        "enabled_mcp": plugins.get("mcp", []),
        "enabled_runners": plugins.get("runners", []),
        "plugin_menus": plugin_menus,
        **kwargs,
    }


def _load_config() -> dict:
    """Load current REST API config from SystemConfig."""
    from core.system_config import SystemConfig

    return SystemConfig.get_param_sync(
        "rest_api_config", {"enabled": False, "models": {}}
    )


def _get_orm_models() -> list[dict]:
    """Get all registered ORM models grouped by schema."""
    from core.orm.registry import Registry

    models = []
    for model_cls in Registry.get_visible_models():
        key = f"{model_cls._schema}.{model_cls._name}"
        models.append(
            {
                "key": key,
                "schema": model_cls._schema,
                "name": model_cls._name,
                "table": model_cls._table_name,
            }
        )
    return models


def _rule_to_perms(rule) -> dict:
    """Convert an ACL rule to a permissions dict.

    ``create`` is treated as separate from ``write`` (POST vs PATCH at
    the router level). For rules predating the split that don't carry
    a ``create`` key, fall back to the ``write`` value so the checkbox
    pre-fills sensibly and a save round-trip doesn't accidentally
    revoke create access.
    """
    denied = {
        "denied": True,
        "read": False,
        "create": False,
        "write": False,
        "delete": False,
    }
    if rule is False:
        return denied
    if rule is True:
        return {
            "denied": False,
            "read": True,
            "create": True,
            "write": True,
            "delete": True,
        }
    if isinstance(rule, dict):
        write_val = bool(rule.get("write", False))
        return {
            "denied": False,
            "read": bool(rule.get("read", False)),
            "create": bool(rule.get("create", write_val)),
            "write": write_val,
            "delete": bool(rule.get("delete", False)),
        }
    # No rule = denied
    return denied


@router.get("/", response_class=HTMLResponse)
async def rest_api_page(request: Request, _=Depends(require_login)):
    """REST API ACL management page."""
    config = _load_config()
    models_config = config.get("models", {})
    wildcard_rule = models_config.get("*", {})

    orm_models = _get_orm_models()

    # Build per-model permissions (explicit rule or wildcard fallback)
    for m in orm_models:
        key = m["key"]
        if key in models_config:
            m["perms"] = _rule_to_perms(models_config[key])
            m["explicit"] = True
        else:
            m["perms"] = _rule_to_perms(wildcard_rule)
            m["explicit"] = False

    # Group by schema
    grouped = defaultdict(list)
    for m in orm_models:
        grouped[m["schema"]].append(m)
    # Sort schemas: admin and oauth2 first, then alphabetical
    priority = {"admin": 0, "oauth2": 1}
    schema_order = sorted(grouped.keys(), key=lambda s: (priority.get(s, 99), s))

    return get_templates().TemplateResponse(
        "rest_api.html",
        get_template_context(
            request,
            api_enabled=config.get("enabled", False),
            schemas=schema_order,
            grouped_models=dict(grouped),
            wildcard_perms=_rule_to_perms(wildcard_rule),
            model_count=len(orm_models),
        ),
    )


@router.post("/save")
async def save_config(request: Request, _=Depends(require_login)):
    """Save REST API ACL configuration."""
    form = await request.form()

    enabled = form.get("enabled") == "on"

    # Parse per-model rules from form
    models_config = {}
    orm_models = _get_orm_models()

    for m in orm_models:
        key = m["key"]
        if form.get(f"deny_{key}") == "on":
            models_config[key] = False
        else:
            read = form.get(f"read_{key}") == "on"
            create = form.get(f"create_{key}") == "on"
            write = form.get(f"write_{key}") == "on"
            delete = form.get(f"delete_{key}") == "on"
            # Only store explicit rules (skip models that match wildcard)
            if not read and not create and not write and not delete:
                models_config[key] = False
            else:
                models_config[key] = {
                    "read": read,
                    "create": create,
                    "write": write,
                    "delete": delete,
                }

    # Wildcard rule
    wc_read = form.get("read_*") == "on"
    wc_create = form.get("create_*") == "on"
    wc_write = form.get("write_*") == "on"
    wc_delete = form.get("delete_*") == "on"
    models_config["*"] = {
        "read": wc_read,
        "create": wc_create,
        "write": wc_write,
        "delete": wc_delete,
    }

    # Remove models that exactly match wildcard (keep config clean)
    wildcard = models_config["*"]
    to_remove = []
    for key, rule in models_config.items():
        if key == "*":
            continue
        if isinstance(rule, dict) and rule == wildcard:
            to_remove.append(key)
    for key in to_remove:
        del models_config[key]

    config = {"enabled": enabled, "models": models_config}

    from core.system_config import SystemConfig

    SystemConfig.set_param_sync("rest_api_config", config)

    # Reload in-memory ACL cache
    from core.rest_api.acl import reload_config

    reload_config()

    return RedirectResponse("/rest-api/?saved=1", status_code=303)

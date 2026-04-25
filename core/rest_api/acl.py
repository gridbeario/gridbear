"""Access control for REST API models.

Loads permissions from SystemConfig (PostgreSQL) and checks per-model access.
"""

import logging

logger = logging.getLogger(__name__)

_config: dict | None = None


def _load_config() -> dict:
    """Load and cache the REST API config from SystemConfig."""
    global _config
    if _config is not None:
        return _config
    from core.system_config import SystemConfig

    _config = SystemConfig.get_param_sync(
        "rest_api_config", {"enabled": False, "models": {}}
    )
    logger.info("REST API config loaded from SystemConfig")
    return _config


def reload_config() -> None:
    """Force reload of the config file."""
    global _config
    _config = None
    _load_config()


def is_enabled() -> bool:
    """Check if the REST API is enabled."""
    return _load_config().get("enabled", False)


def _get_model_rule(model_key: str) -> dict | bool | None:
    """Get the ACL rule for a model key (e.g. 'public.admin_users').

    Returns:
        - False: model is explicitly denied
        - True: full access
        - dict with {read, create, write, delete}: granular access
          (``create`` is optional; falls back to ``write`` for rules
          predating the create/write split)
        - None: no rule found (will fall back to wildcard or deny)
    """
    config = _load_config()
    models = config.get("models", {})

    # Exact match first
    if model_key in models:
        return models[model_key]

    # Wildcard default
    if "*" in models:
        return models["*"]

    # No rule → deny
    return None


def check_access(model_key: str, operation: str) -> bool:
    """Check if an operation is allowed on a model.

    Args:
        model_key: e.g. 'public.admin_users'
        operation: one of 'read', 'create', 'write', 'delete'

    Returns:
        True if the operation is allowed.

    Backward compat: rules predating the create/write split don't carry
    a ``create`` key. For those, ``create`` falls back to ``write`` so
    upgrading the codebase doesn't silently revoke POST access on
    models the operator already authorized for write.
    """
    rule = _get_model_rule(model_key)

    # No rule or explicitly False → deny
    if rule is None or rule is False:
        return False

    # True → full access
    if rule is True:
        return True

    # Dict → granular check
    if isinstance(rule, dict):
        if operation == "create" and "create" not in rule:
            return bool(rule.get("write", False))
        return bool(rule.get(operation, False))

    return False


def is_model_visible(model_key: str) -> bool:
    """Check if a model should appear in the list_models endpoint."""
    rule = _get_model_rule(model_key)
    # Hidden if no rule or explicitly False
    return rule is not None and rule is not False

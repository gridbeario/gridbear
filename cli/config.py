"""CLI configuration — TOML file, env vars, CLI overrides."""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(
    os.environ.get("GRIDBEAR_CONFIG_DIR", "~/.config/gridbear")
).expanduser()

DEFAULT_GATEWAY_URL = "http://localhost:8000"


@dataclass
class CLIConfig:
    gateway_url: str = DEFAULT_GATEWAY_URL
    default_user: str | None = None
    default_agent: str | None = None
    extra: dict = field(default_factory=dict)


def _load_toml() -> dict:
    """Load config.toml from CONFIG_DIR, return empty dict if missing."""
    path = CONFIG_DIR / "config.toml"
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_config(**overrides) -> CLIConfig:
    """Build config with precedence: overrides > env vars > TOML > defaults."""
    toml_data = _load_toml()
    conn = toml_data.get("connection", {})

    gateway_url = (
        overrides.get("gateway_url")
        or os.environ.get("GRIDBEAR_URL")
        or os.environ.get("GRIDBEAR_GATEWAY_URL")
        or conn.get("gateway_url")
        or DEFAULT_GATEWAY_URL
    )

    default_user = (
        overrides.get("default_user")
        or os.environ.get("GRIDBEAR_CLI_USER")
        or conn.get("default_user")
    )

    default_agent = (
        overrides.get("default_agent")
        or os.environ.get("GRIDBEAR_CLI_AGENT")
        or conn.get("default_agent")
    )

    return CLIConfig(
        gateway_url=gateway_url.rstrip("/"),
        default_user=default_user,
        default_agent=default_agent,
        extra=toml_data,
    )

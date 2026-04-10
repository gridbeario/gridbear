"""Generate Vibe CLI configuration files.

Manages ~/.vibe/config.toml (model, MCP servers) and ~/.vibe/.env (API key).
Called before each CLI invocation to ensure config is current.
"""

import os
from pathlib import Path

from config.logging_config import logger

VIBE_HOME = Path.home() / ".vibe"
VIBE_CONFIG_PATH = VIBE_HOME / "config.toml"
VIBE_ENV_PATH = VIBE_HOME / ".env"


def write_env(api_key: str) -> bool:
    """Write MISTRAL_API_KEY to ~/.vibe/.env.

    Returns True if the file was written successfully.
    """
    try:
        VIBE_HOME.mkdir(parents=True, exist_ok=True)
        VIBE_ENV_PATH.write_text(f"MISTRAL_API_KEY={api_key}\n")
        return True
    except OSError as e:
        logger.warning("Could not write %s: %s", VIBE_ENV_PATH, e)
        return False


_GW_BLOCK_MARKER = "# --- gridbear-gateway (managed) ---"

_GW_TEMPLATE = """{marker}
[[mcp_servers]]
name = "gridbear-gateway"
transport = "http"
url = "{url}"
api_key_env = "{env}"
api_key_header = "Authorization"
api_key_format = "Bearer {{token}}"
{marker}"""


def write_config(
    gateway_url: str | None = None,
    mcp_token_env: str = "GRIDBEAR_MCP_TOKEN",
    model: str | None = None,
) -> bool:
    """Ensure ~/.vibe/config.toml has the MCP gateway server configured.

    Uses text-level surgery to avoid corrupting Vibe's complex TOML
    (nested tables like [tools.bash] break with parse+reserialize).
    Only touches the marked gridbear-gateway block and active_model —
    leaves everything else (models, tools, providers) untouched.

    Returns True if the file was written successfully.
    """
    import re

    gw_url = gateway_url or os.getenv("MCP_GATEWAY_URL", "http://localhost:8000")

    gw_block = _GW_TEMPLATE.format(
        marker=_GW_BLOCK_MARKER,
        url=f"{gw_url}/mcp",
        env=mcp_token_env,
    )

    try:
        VIBE_HOME.mkdir(parents=True, exist_ok=True)

        if VIBE_CONFIG_PATH.exists():
            text = VIBE_CONFIG_PATH.read_text()
        else:
            text = ""

        # Replace or append gridbear-gateway block
        marker_re = re.escape(_GW_BLOCK_MARKER)
        pattern = rf"{marker_re}.*?{marker_re}"
        if re.search(pattern, text, re.DOTALL):
            text = re.sub(pattern, gw_block, text, count=1, flags=re.DOTALL)
        else:
            text = text.rstrip() + "\n\n" + gw_block + "\n"

        # Ensure auto_approve is true for programmatic mode
        if re.search(r"^auto_approve\s*=\s*false", text, re.MULTILINE):
            text = re.sub(
                r"^auto_approve\s*=\s*false",
                "auto_approve = true",
                text,
                count=1,
                flags=re.MULTILINE,
            )

        # Update active_model if specified
        if model:
            if re.search(r"^active_model\s*=", text, re.MULTILINE):
                text = re.sub(
                    r'^active_model\s*=\s*"[^"]*"',
                    f'active_model = "{model}"',
                    text,
                    count=1,
                    flags=re.MULTILINE,
                )
            else:
                text = f'active_model = "{model}"\n' + text

            # Ensure model is defined in [[models]] section
            if f'name = "{model}"' not in text:
                model_block = (
                    f"\n[[models]]\n"
                    f'name = "{model}"\n'
                    f'provider = "mistral"\n'
                    f"temperature = 0.2\n"
                )
                # Insert before the gateway block marker if present
                if _GW_BLOCK_MARKER in text:
                    text = text.replace(
                        _GW_BLOCK_MARKER,
                        model_block + "\n" + _GW_BLOCK_MARKER,
                        1,
                    )
                else:
                    text = text.rstrip() + "\n" + model_block

        VIBE_CONFIG_PATH.write_text(text)
        logger.debug("Wrote Vibe MCP config: gateway=%s, model=%s", gw_url, model)
        return True
    except OSError as e:
        logger.warning("Could not write %s: %s", VIBE_CONFIG_PATH, e)
        return False


def ensure_api_key() -> bool:
    """Ensure API key is written to ~/.vibe/.env from secrets manager.

    Returns True if the API key is available and written.
    """
    from ui.secrets_manager import secrets_manager

    api_key = secrets_manager.get_plain("MISTRAL_API_KEY")
    if not api_key:
        return False
    return write_env(api_key)

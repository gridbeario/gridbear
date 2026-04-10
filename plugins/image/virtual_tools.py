"""Image Virtual Tool Provider.

Exposes image__generate as an MCP tool and delegates to the provider
configured in the agent's YAML (image.provider).
"""

import importlib.util
import json
from pathlib import Path

from config.logging_config import logger
from core.interfaces.local_tools import LocalToolProvider

_SERVER_NAME = "image"

_TOOLS = [
    {
        "name": "image__generate",
        "description": (
            "Generate an image from a text description using AI. "
            "Returns the file path of the generated image. "
            "Use send_file_to_chat to deliver it to the user."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "Detailed image description in English "
                        "(style, colors, atmosphere, composition)"
                    ),
                },
                "size": {
                    "type": "string",
                    "description": "Image dimensions (default: 1024x1024)",
                    "enum": ["1024x1024", "1792x1024", "1024x1792"],
                },
            },
            "required": ["prompt"],
        },
    },
]

BASE_DIR = Path(__file__).resolve().parent.parent.parent
PLUGINS_DIR = BASE_DIR / "plugins"


def _load_provider_class(provider_name: str):
    """Load an image provider class by name from its manifest.

    Same pattern as _load_tts_class in ui/routes/agents.py.
    """
    plugin_dir = PLUGINS_DIR / provider_name
    manifest_path = plugin_dir / "manifest.json"
    if not manifest_path.exists():
        return None

    manifest = json.loads(manifest_path.read_text())
    if manifest.get("provides", manifest.get("type")) != "image":
        return None

    class_name = manifest.get("class_name")
    entry_point = manifest.get("entry_point", "service.py")
    if not class_name:
        return None

    service_path = plugin_dir / entry_point
    if not service_path.exists():
        return None

    spec = importlib.util.spec_from_file_location(
        f"gridbear.plugins.{provider_name}.service",
        service_path,
        submodule_search_locations=[str(service_path.parent)],
    )
    if spec is None or spec.loader is None:
        return None

    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, class_name, None)


def _get_agent_image_provider(agent_name: str | None) -> str | None:
    """Read image.provider from the agent's DB config.

    Falls back to the base plugin's default_provider config.
    Returns None if no provider is configured anywhere.
    """
    # 1) Agent DB override
    if agent_name:
        try:
            from core.models.agent_config import AgentConfigRecord

            record = AgentConfigRecord.get_sync(id=agent_name)
            if record:
                provider = (record.get("image") or {}).get("provider")
                if provider:
                    return provider
        except Exception:
            pass

    # 2) Base plugin default_provider from DB config
    base_config = _get_provider_config("image")
    default = base_config.get("default_provider")
    if default:
        return default

    return None


def _get_provider_config(provider_name: str) -> dict:
    """Read provider config from DB."""
    try:
        from core.plugin_registry.models import PluginRegistryEntry

        entry = PluginRegistryEntry.get_sync(name=provider_name)
        if entry:
            return entry.get("config") or {}
    except Exception:
        pass
    return {}


class ImageToolProvider(LocalToolProvider):
    """Exposes image generation tool, delegating to configured provider."""

    def get_server_name(self) -> str:
        return _SERVER_NAME

    def get_tools(self) -> list[dict]:
        return _TOOLS

    async def handle_tool_call(
        self, tool_name: str, arguments: dict, **kwargs
    ) -> list[dict]:
        action = tool_name.replace("image__", "")
        agent_name = kwargs.get("agent_name")

        logger.info(f"image tool: {action} agent={agent_name}")

        if action == "generate":
            return await self._handle_generate(arguments, agent_name)

        return [{"type": "text", "text": f"Unknown image tool action: {action}"}]

    @staticmethod
    async def _handle_generate(arguments: dict, agent_name: str | None) -> list[dict]:
        """Generate an image by delegating to the agent's configured provider."""
        prompt = arguments.get("prompt", "")
        size = arguments.get("size", "1024x1024")

        if not prompt:
            return [{"type": "text", "text": "Error: prompt is required"}]

        # Resolve provider for this agent
        provider_name = _get_agent_image_provider(agent_name)
        if not provider_name:
            return [
                {
                    "type": "text",
                    "text": "Error: no image provider configured",
                }
            ]
        logger.info(f"Image provider for {agent_name}: {provider_name}")

        # Load provider class
        provider_class = _load_provider_class(provider_name)
        if not provider_class:
            return [
                {
                    "type": "text",
                    "text": f"Error: image provider '{provider_name}' not found",
                }
            ]

        # Instantiate and generate
        try:
            config = _get_provider_config(provider_name)
            instance = provider_class(config)
            await instance.initialize()
            result = await instance.generate(prompt, size)
        except Exception as e:
            logger.error(f"Image provider '{provider_name}' error: {e}")
            return [{"type": "text", "text": f"Error generating image: {e}"}]

        if not result:
            return [
                {
                    "type": "text",
                    "text": "Image generation failed (provider returned empty result)",
                }
            ]

        logger.info(f"Image generated via {provider_name}: {result}")
        return [
            {
                "type": "text",
                "text": (
                    f"Image generated successfully: {result}\n"
                    "Use send_file_to_chat to deliver it to the user."
                ),
            }
        ]

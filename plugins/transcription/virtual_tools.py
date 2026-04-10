"""Transcription Virtual Tool Provider.

Exposes transcription__transcribe as an MCP tool and delegates to the
provider configured in the agent's YAML (transcription.provider).
"""

import importlib.util
import json
from pathlib import Path

from config.logging_config import logger
from core.interfaces.local_tools import LocalToolProvider

_SERVER_NAME = "transcription"

_TOOLS = [
    {
        "name": "transcription__transcribe",
        "description": (
            "Transcribe an audio file to text. "
            "Returns the transcription text. "
            "Supports mp3, wav, m4a, ogg, flac, webm, mp4 formats."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the audio file to transcribe",
                },
                "language": {
                    "type": "string",
                    "description": (
                        "Language code (e.g. 'it', 'en'). "
                        "Leave empty for provider default or auto-detection."
                    ),
                },
            },
            "required": ["file_path"],
        },
    },
]

BASE_DIR = Path(__file__).resolve().parent.parent.parent
PLUGINS_DIR = BASE_DIR / "plugins"


def _load_provider_class(provider_name: str):
    """Load a transcription provider class by name from its manifest."""
    plugin_dir = PLUGINS_DIR / provider_name
    manifest_path = plugin_dir / "manifest.json"
    if not manifest_path.exists():
        return None

    manifest = json.loads(manifest_path.read_text())
    if manifest.get("provides", manifest.get("type")) != "transcription":
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


def _get_agent_transcription_provider(agent_name: str | None) -> str | None:
    """Read transcription.provider from the agent's DB config.

    Falls back to the base plugin's default_provider config.
    Returns None if no provider is configured anywhere.
    """
    # 1) Agent DB override
    if agent_name:
        try:
            from core.models.agent_config import AgentConfigRecord

            record = AgentConfigRecord.get_sync(id=agent_name)
            if record:
                provider = (record.get("transcription") or {}).get("provider")
                if provider:
                    return provider
        except Exception:
            pass

    # 2) Base plugin default_provider from DB config
    base_config = _get_provider_config("transcription")
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


class TranscriptionToolProvider(LocalToolProvider):
    """Exposes transcription tool, delegating to configured provider."""

    def get_server_name(self) -> str:
        return _SERVER_NAME

    def get_tools(self) -> list[dict]:
        return _TOOLS

    async def handle_tool_call(
        self, tool_name: str, arguments: dict, **kwargs
    ) -> list[dict]:
        action = tool_name.replace("transcription__", "")
        agent_name = kwargs.get("agent_name")

        logger.info("transcription tool: %s agent=%s", action, agent_name)

        if action == "transcribe":
            return await self._handle_transcribe(arguments, agent_name)

        return [
            {"type": "text", "text": f"Unknown transcription tool action: {action}"}
        ]

    @staticmethod
    async def _handle_transcribe(arguments: dict, agent_name: str | None) -> list[dict]:
        """Transcribe audio by delegating to the agent's configured provider."""
        file_path = arguments.get("file_path", "")
        language = arguments.get("language")

        if not file_path:
            return [{"type": "text", "text": "Error: file_path is required"}]

        provider_name = _get_agent_transcription_provider(agent_name)
        if not provider_name:
            return [
                {
                    "type": "text",
                    "text": "Error: no transcription provider configured",
                }
            ]
        logger.info("Transcription provider for %s: %s", agent_name, provider_name)

        provider_class = _load_provider_class(provider_name)
        if not provider_class:
            return [
                {
                    "type": "text",
                    "text": f"Error: transcription provider '{provider_name}' not found",
                }
            ]

        try:
            config = _get_provider_config(provider_name)
            instance = provider_class(config)
            await instance.initialize()
            result = await instance.transcribe(file_path, language)
        except Exception as e:
            logger.error("Transcription provider '%s' error: %s", provider_name, e)
            return [{"type": "text", "text": f"Error transcribing audio: {e}"}]

        if not result:
            return [
                {
                    "type": "text",
                    "text": "Transcription failed (provider returned empty result)",
                }
            ]

        logger.info("Transcription via %s: %s...", provider_name, result[:100])
        return [{"type": "text", "text": result}]

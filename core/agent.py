"""Agent Model for Multi-Agent Architecture.

Defines Agent, AgentState, and AgentConfig for the multi-agent system.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from core.exceptions import ServiceNotConfiguredError

if TYPE_CHECKING:
    from core.interfaces.channel import BaseChannel
    from core.interfaces.service import BaseService
    from core.plugin_manager import PluginManager


class AgentState(Enum):
    """Possible states of an agent."""

    STOPPED = "stopped"  # Agent not started
    STARTING = "starting"  # Starting in progress
    RUNNING = "running"  # Fully operational
    DEGRADED = "degraded"  # Running but with issues (e.g., one channel down)
    STOPPING = "stopping"  # Shutdown in progress
    FAILED = "failed"  # Fatal error, requires intervention


@dataclass
class VoiceConfig:
    """Voice/TTS configuration for an agent."""

    provider: str = ""  # TTS service name (e.g., "tts-google")
    voice_id: str = ""  # Voice identifier
    language: str = "it-IT"  # Language code


@dataclass
class ImageConfig:
    """Image generation configuration for an agent."""

    provider: str = ""  # Image provider name (e.g., "image-openai")


@dataclass
class ChannelConfig:
    """Channel configuration for an agent."""

    platform: str  # Channel name from plugin manifest
    token_env: str  # Environment variable name for token
    authorized_users: list[str] = field(default_factory=list)  # User IDs or @usernames
    authorized_guilds: list[str] = field(default_factory=list)  # Discord guilds
    raw_config: dict = field(
        default_factory=dict
    )  # Full YAML config for platform-specific fields  # Discord guilds


@dataclass
class ServiceConfig:
    """Service configuration for per-agent plugin instantiation."""

    name: str  # Service name (e.g., "tts", "homeassistant")
    provider: str = (
        ""  # Optional provider override (e.g., "tts-openai" instead of "tts")
    )
    config: dict = field(default_factory=dict)  # Service-specific configuration
    isolated: bool = False  # Use separate instance with default config
    required: bool = True  # If True, agent fails to start if service fails

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "ServiceConfig":
        """Create ServiceConfig from dictionary."""
        return cls(
            name=name,
            provider=data.get("provider", ""),
            config=data.get("config", {}),
            isolated=data.get("isolated", False),
            required=data.get("required", True),
        )


@dataclass
class AgentConfig:
    """Configuration for an agent loaded from YAML."""

    name: str  # Internal identifier (e.g., "myagent")
    display_name: str  # Display name (e.g., "My Agent")
    description: str = ""  # Agent description
    system_prompt: str = ""  # Personality/system prompt
    locale: str = "it"  # Default response language
    timezone: str = "Europe/Rome"  # Timezone for scheduling
    model: str = ""  # LLM model override (empty = use runner default)
    runner: str = ""  # Runner override (empty = use default runner)
    fallback_runner: str = ""  # Fallback runner if primary fails
    avatar: str = ""  # Avatar filename (e.g., "myagent.png")
    channels: list[ChannelConfig] = field(default_factory=list)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    image: ImageConfig = field(default_factory=ImageConfig)
    services: list[ServiceConfig] = field(default_factory=list)  # Per-agent services
    mcp_permissions: list[str] = field(default_factory=list)  # Allowed MCP servers
    max_tools: int | None = None  # Max MCP tools (None = use runner default)
    tool_loading: str = (
        "full"  # "full" = all tools upfront, "search" = discover on-demand
    )
    plugins_enabled: list[str] = field(default_factory=list)  # Enabled plugins
    plugins_exclusive: list[str] = field(default_factory=list)  # Exclusive plugins
    context_options: dict = field(default_factory=dict)  # Lightweight context opts

    @classmethod
    def from_dict(cls, data: dict) -> "AgentConfig":
        """Create AgentConfig from dictionary (parsed YAML)."""
        channels = []
        for platform, ch_config in data.get("channels", {}).items():
            channels.append(
                ChannelConfig(
                    platform=platform,
                    token_env=ch_config.get("token_secret", ""),
                    authorized_users=ch_config.get("allowed_users", []),
                    authorized_guilds=ch_config.get("allowed_guilds", []),
                    raw_config=ch_config,
                )
            )

        voice_data = data.get("voice", {})
        voice = VoiceConfig(
            provider=voice_data.get("provider", ""),
            voice_id=voice_data.get("voice_id", ""),
            language=voice_data.get("language", "it-IT"),
        )

        image_data = data.get("image", {})
        image = ImageConfig(
            provider=image_data.get("provider", ""),
        )

        # Parse services section
        services = []
        for svc_name, svc_config in data.get("services", {}).items():
            services.append(ServiceConfig.from_dict(svc_name, svc_config or {}))

        plugins = data.get("plugins", {})

        # Parse model: supports both string ("haiku") and dict ({"default": "haiku"})
        model_raw = data.get("model", "")
        if isinstance(model_raw, dict):
            model = model_raw.get("default", "")
        else:
            model = str(model_raw) if model_raw else ""

        return cls(
            name=data.get("id", ""),
            display_name=data.get("name", ""),
            description=data.get("description", ""),
            system_prompt=data.get("personality", ""),
            locale=data.get("locale", "it"),
            timezone=data.get("timezone", "Europe/Rome"),
            model=model,
            runner=data.get("runner", ""),
            fallback_runner=data.get("fallback_runner", ""),
            avatar=data.get("avatar", ""),
            channels=channels,
            voice=voice,
            image=image,
            services=services,
            mcp_permissions=data.get("mcp_permissions", []),
            max_tools=data.get("max_tools"),
            tool_loading=data.get("tool_loading", "full"),
            plugins_enabled=plugins.get("enabled", []),
            plugins_exclusive=plugins.get("exclusive", []),
            context_options=data.get("context_options", {}),
        )


class Agent:
    """An autonomous agent with its own identity, channels, and configuration."""

    def __init__(
        self,
        config: AgentConfig,
        plugin_manager: "PluginManager",
        raw_config: dict | None = None,
    ):
        """Initialize agent with configuration.

        Args:
            config: Agent configuration from YAML
            plugin_manager: Shared plugin manager instance
            raw_config: Raw YAML dict for settings not in AgentConfig (e.g., email)
        """
        self.config = config
        self.plugin_manager = plugin_manager
        self.raw_config = raw_config or {}
        self.state = AgentState.STOPPED
        self._channels: dict[str, "BaseChannel"] = {}
        self._services: dict[str, "BaseService"] = {}
        self._channel_tasks: list[Any] = []

    @property
    def email_settings(self) -> dict:
        """Get agent's email settings from raw config."""
        return self.raw_config.get("email", {})

    @property
    def name(self) -> str:
        """Agent internal name/ID."""
        return self.config.name

    @property
    def display_name(self) -> str:
        """Agent display name."""
        return self.config.display_name

    @property
    def system_prompt(self) -> str:
        """Agent personality/system prompt."""
        return self.config.system_prompt

    def add_channel(self, platform: str, channel: "BaseChannel") -> None:
        """Add a channel instance to this agent.

        Args:
            platform: Platform identifier (telegram, discord)
            channel: Channel instance
        """
        self._channels[platform] = channel

    def get_channel(self, platform: str) -> "BaseChannel | None":
        """Get channel by platform name or plugin name."""
        # Direct lookup by plugin name (e.g. "whatsapp_api", "telegram")
        ch = self._channels.get(platform)
        if ch:
            return ch
        # Fallback: search by channel's platform attribute
        # (e.g. whatsapp_api plugin declares platform = "whatsapp")
        for channel in self._channels.values():
            if getattr(channel, "platform", None) == platform:
                return channel
        return None

    def get_channel_names(self) -> list[str]:
        """Return list of available channel platform names."""
        return list(self._channels.keys())

    def add_service(self, name: str, service: "BaseService") -> None:
        """Add a per-agent service instance.

        Args:
            name: Service name (e.g., "tts", "homeassistant")
            service: Service instance
        """
        self._services[name] = service

    def get_service(self, name: str, allow_fallback: bool = False) -> "BaseService":
        """Get a service by name.

        Looks first in agent-specific services, then optionally falls back
        to shared services from PluginManager.

        Args:
            name: Service name
            allow_fallback: If True, falls back to shared service from PluginManager.
                           Default False to avoid silent fallback.

        Returns:
            Service instance

        Raises:
            ServiceNotConfiguredError: If service not found and allow_fallback=False
        """
        # Check agent-specific services first
        if name in self._services:
            return self._services[name]

        # Fallback to shared service if allowed
        if allow_fallback:
            shared = self.plugin_manager.get_service(name)
            if shared:
                return shared

        raise ServiceNotConfiguredError(
            f"Service '{name}' not configured for agent '{self.name}'"
        )

    def has_service(self, name: str) -> bool:
        """Check if agent has a specific service configured.

        Args:
            name: Service name

        Returns:
            True if service is configured for this agent
        """
        return name in self._services

    async def start(self) -> None:
        """Start all channels for this agent."""
        import asyncio

        from config.logging_config import logger

        if self.state not in (AgentState.STOPPED, AgentState.FAILED):
            logger.warning(f"Agent {self.name} is already in state {self.state.value}")
            return

        self.state = AgentState.STARTING
        logger.info(f"Starting agent: {self.display_name}")

        failed_channels = []
        started_channels = []

        for platform, channel in self._channels.items():
            try:
                # Start channel in background task
                task = asyncio.create_task(channel.start())
                self._channel_tasks.append(task)
                started_channels.append(platform)
                logger.info(f"Agent {self.name}: started channel {platform}")
            except Exception as e:
                logger.error(f"Agent {self.name}: failed to start {platform}: {e}")
                failed_channels.append(platform)

        if not started_channels and not self._channels:
            # No channels configured — CLI/API-only agent
            self.state = AgentState.RUNNING
            logger.info(f"Agent {self.name}: no channels configured (CLI/API-only)")
        elif not started_channels:
            self.state = AgentState.FAILED
            logger.error(f"Agent {self.name}: all channels failed to start")
        elif failed_channels:
            self.state = AgentState.DEGRADED
            logger.warning(
                f"Agent {self.name}: running degraded, failed channels: {failed_channels}"
            )
        else:
            self.state = AgentState.RUNNING
            logger.info(f"Agent {self.name}: all channels started successfully")

    async def stop(self) -> None:
        """Stop all channels for this agent."""
        import asyncio

        from config.logging_config import logger

        if self.state == AgentState.STOPPED:
            return

        self.state = AgentState.STOPPING
        logger.info(f"Stopping agent: {self.display_name}")

        for platform, channel in self._channels.items():
            try:
                await channel.stop()
                logger.info(f"Agent {self.name}: stopped channel {platform}")
            except Exception as e:
                logger.error(f"Agent {self.name}: error stopping {platform}: {e}")

        # Cancel any pending channel tasks
        for task in self._channel_tasks:
            if not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass

        self._channel_tasks.clear()
        self.state = AgentState.STOPPED
        logger.info(f"Agent {self.name}: stopped")

    def get_health(self) -> dict:
        """Get health status of this agent.

        Returns:
            Dict with state, channels, services, and any issues
        """
        return {
            "name": self.name,
            "display_name": self.display_name,
            "state": self.state.value,
            "channels": list(self._channels.keys()),
            "services": list(self._services.keys()),
            "mcp_permissions": self.config.mcp_permissions,
        }

    async def process_inter_agent_message(
        self,
        message: str,
        context: dict,
    ) -> str:
        """Process a message from another agent.

        Args:
            message: The message content (already formatted with source)
            context: Inter-agent context with source info

        Returns:
            Response text
        """
        from config.logging_config import logger

        # Get the first available channel to use its message handler
        if not self._channels:
            logger.warning(
                f"Agent {self.name}: no channels available for inter-agent message"
            )
            return "Agente non disponibile."

        channel = next(iter(self._channels.values()))
        if not channel._message_handler:
            logger.warning(f"Agent {self.name}: no message handler configured")
            return "Agente non configurato."

        # Create a synthetic message and user info for the handler
        from core.interfaces.channel import Message, UserInfo

        # For workflow steps, use the original user identity so that
        # user-aware MCP servers (odoo, google-sheets) get proper creds.
        original_user = context.get("original_user_id")
        effective_username = original_user or context.get("from_agent", "system")

        synthetic_message = Message(
            user_id=0,  # System/inter-agent
            username=effective_username,
            text=message,
            platform="inter_agent",
        )

        synthetic_user = UserInfo(
            user_id=0,
            username=effective_username,
            display_name=context.get("from_agent", "System").title(),
            platform="inter_agent",
        )
        # Set unified_id directly so the message handler uses the real
        # user identity instead of falling back to "inter_agent:{username}"
        if original_user:
            synthetic_user.unified_id = original_user

        try:
            # Call the message handler
            response = await channel._message_handler(
                synthetic_message,
                synthetic_user,
                inter_agent_context=context,
            )
            return response or "Nessuna risposta."
        except Exception as e:
            logger.error(f"Agent {self.name}: inter-agent message error: {e}")
            return f"Errore: {str(e)}"

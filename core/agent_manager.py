"""Agent Manager for Multi-Agent Architecture.

Manages the lifecycle of all agents: loading, starting, stopping, and health monitoring.
"""

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from config.logging_config import logger
from core.agent import Agent, AgentConfig, AgentState
from core.exceptions import AgentNotFoundError, AgentStartupError, AgentUnavailableError

if TYPE_CHECKING:
    from core.plugin_manager import PluginManager

# Retry configuration for agent startup
RETRY_CONFIG = {
    "max_attempts": 3,
    "base_delay_seconds": 5,
    "max_delay_seconds": 60,
    "backoff_multiplier": 2.0,
}

# Inter-agent communication configuration
MAX_INTER_AGENT_DEPTH = 3
INTER_AGENT_TIMEOUT = 60.0  # seconds

# Health check configuration
HEALTH_CHECK_CONFIG = {
    "interval_seconds": 60,
    "timeout_seconds": 10,
}


class AgentManager:
    """Manages the lifecycle of multiple agents."""

    def __init__(
        self,
        plugin_manager: "PluginManager",
        agents_dir: Path | None = None,
        schema_path: Path | None = None,
    ):
        """Initialize AgentManager.

        Args:
            plugin_manager: Shared plugin manager instance
            agents_dir: Deprecated, kept for backward compatibility
            schema_path: Deprecated, kept for backward compatibility
        """
        self.plugin_manager = plugin_manager
        self._agents: dict[str, Agent] = {}
        self._health_check_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._handler_factory = None

    def _resolve_env_vars(self, config: dict) -> dict:
        """Resolve ${ENV_VAR} references in configuration.

        Args:
            config: Configuration dictionary

        Returns:
            Configuration with environment variables resolved

        Raises:
            ValueError: If required environment variable is not set
        """
        import os
        import re

        def resolve_value(value):
            if isinstance(value, str):
                # Match ${VAR_NAME} pattern
                matches = re.findall(r"\$\{([^}]+)\}", value)
                for var_name in matches:
                    env_value = os.environ.get(var_name)
                    if env_value is None:
                        raise ValueError(f"Environment variable not set: {var_name}")
                    value = value.replace(f"${{{var_name}}}", env_value)
                return value
            elif isinstance(value, dict):
                return {k: resolve_value(v) for k, v in value.items()}
            elif isinstance(value, list):
                return [resolve_value(v) for v in value]
            return value

        return resolve_value(config)

    async def load_all(self) -> list[Agent]:
        """Load all active agent configurations from the database.

        Returns:
            List of loaded Agent instances
        """
        from core.models.agent_config import AgentConfigRecord

        records = AgentConfigRecord.search_sync(
            [("is_active", "=", True)],
            order="id ASC",
        )

        loaded_agents = []
        for record in records:
            try:
                agent = await self._load_agent(record["id"])
                if agent:
                    self._agents[agent.name] = agent
                    loaded_agents.append(agent)
            except Exception as e:
                logger.error("Failed to load agent %s: %s", record["id"], e)

        logger.info(
            "Loaded %d agent(s): %s",
            len(loaded_agents),
            [a.name for a in loaded_agents],
        )
        return loaded_agents

    async def _load_agent(self, agent_id: str) -> Agent | None:
        """Load a single agent from the database.

        Args:
            agent_id: Agent ID in the database

        Returns:
            Agent instance or None if loading failed
        """
        from core.models.agent_config import AgentConfigRecord

        logger.debug("Loading agent from DB: %s", agent_id)

        record = AgentConfigRecord.get_sync(id=agent_id)
        if not record:
            logger.warning("Agent %s not found in DB", agent_id)
            return None

        # Build config dict compatible with AgentConfig.from_dict()
        config = dict(record)
        # Remove ORM metadata fields not needed by AgentConfig
        config.pop("created_at", None)
        config.pop("updated_at", None)
        config.pop("is_active", None)

        # Normalize services: ORM stores as list but from_dict expects dict
        services = config.get("services")
        if isinstance(services, list):
            config["services"] = (
                {s["name"]: s for s in services if isinstance(s, dict) and "name" in s}
                if services
                else {}
            )

        # Resolve environment variables
        try:
            config = self._resolve_env_vars(config)
        except ValueError as e:
            logger.error("Environment variable error for agent %s: %s", agent_id, e)
            return None

        # Create AgentConfig
        agent_config = AgentConfig.from_dict(config)

        # Validate MCP permissions against available servers
        available_mcp = self.plugin_manager.get_all_mcp_server_names()
        for mcp in agent_config.mcp_permissions:
            if mcp != "*" and mcp not in available_mcp:
                logger.warning(
                    "Agent %s: MCP server '%s' not found", agent_config.name, mcp
                )

        # Create Agent instance
        agent = Agent(
            config=agent_config,
            plugin_manager=self.plugin_manager,
            raw_config=config,
        )

        # Create per-agent services
        try:
            await self._create_agent_services(agent)
        except AgentStartupError as e:
            logger.error("Agent %s: %s", agent_config.name, e)
            return None

        # Create channels for this agent
        await self._create_agent_channels(agent)

        logger.info(
            "Loaded agent: %s (%s) with %d channel(s)",
            agent.display_name,
            agent.name,
            len(agent._channels),
        )
        return agent

    async def _create_agent_channels(self, agent: Agent) -> None:
        """Create channel instances for an agent.

        Args:
            agent: Agent to create channels for
        """
        from ui.secrets_manager import secrets_manager

        for ch_config in agent.config.channels:
            platform = ch_config.platform

            # Get token from environment
            token = secrets_manager.get(ch_config.token_env, fallback_env=True)
            if not token:
                logger.error(
                    f"Agent {agent.name}: token not found for {platform} "
                    f"(env: {ch_config.token_env})"
                )
                continue

            # Create channel configuration (include raw_config for platform-specific fields)
            channel_config = {
                "token_env": ch_config.token_env,
                "authorized_users": ch_config.authorized_users,
                "authorized_guilds": ch_config.authorized_guilds,
                **ch_config.raw_config,
            }

            # Create channel instance via plugin registry (no hardcoded imports)
            channel_class = self.plugin_manager.get_plugin_class(platform)
            if not channel_class:
                logger.warning(
                    f"Agent {agent.name}: unknown platform '{platform}' (no plugin registered)"
                )
                continue
            channel = channel_class(channel_config)

            if channel:
                # Set agent context on the channel
                channel.agent_name = agent.name
                channel.set_agent_context(
                    {
                        "name": agent.name,
                        "display_name": agent.display_name,
                        "system_prompt": agent.system_prompt,
                        "model": agent.config.model,
                        "runner": agent.config.runner,
                        "fallback_runner": agent.config.fallback_runner,
                        "voice": {
                            "provider": agent.config.voice.provider,
                            "voice_id": agent.config.voice.voice_id,
                            "language": agent.config.voice.language,
                        },
                        "mcp_permissions": agent.config.mcp_permissions,
                        "max_tools": agent.config.max_tools,
                        "tool_loading": agent.config.tool_loading,
                        "locale": agent.config.locale,
                        "email": agent.email_settings,
                        "context_options": agent.config.context_options,
                    }
                )

                # Set up channel dependencies
                channel.set_agent(agent)
                channel.set_plugin_manager(self.plugin_manager)

                # Set scheduler service if available (any BaseSchedulerService)
                from core.interfaces.service import BaseSchedulerService

                for svc in agent._services.values():
                    if isinstance(svc, BaseSchedulerService):
                        channel.set_task_scheduler(svc)
                        break

                agent.add_channel(platform, channel)
                logger.debug(f"Agent {agent.name}: created {platform} channel")

    async def _create_agent_services(self, agent: Agent) -> None:
        """Create per-agent service instances.

        Args:
            agent: Agent to create services for

        Raises:
            AgentStartupError: If a required service fails to initialize
        """

        # Also instantiate per-agent services from plugins_enabled
        # that aren't explicitly in the services section
        explicit_services = {s.name for s in agent.config.services}
        for plugin_name in agent.config.plugins_enabled:
            if plugin_name in explicit_services:
                continue
            # Check if this plugin is per-agent
            manifest = self.plugin_manager.get_plugin_manifest(plugin_name)
            if manifest and manifest.get("instantiation") == "per-agent":
                # Get the "provides" name or use plugin name
                provides = manifest.get("provides", plugin_name)
                plugin_class = self.plugin_manager.get_plugin_class(provides)
                if plugin_class:
                    try:
                        # Load default config for this plugin
                        config = self.plugin_manager._load_config().get(plugin_name, {})
                        # Override timezone with agent's timezone for services that use it
                        if agent.config.timezone:
                            config["timezone"] = agent.config.timezone
                        instance = plugin_class(config)
                        if hasattr(instance, "set_plugin_manager"):
                            instance.set_plugin_manager(self.plugin_manager)
                        await instance.initialize()
                        agent.add_service(provides, instance)
                        logger.info(
                            f"[{agent.name}] Auto-initialized per-agent service: {provides}"
                        )
                    except Exception as e:
                        logger.warning(
                            f"[{agent.name}] Failed to init per-agent service {plugin_name}: {e}"
                        )

        # Auto-instantiate TTS service from voice.provider config
        if agent.config.voice.provider and "tts" not in explicit_services:
            tts_provider = agent.config.voice.provider
            tts_class = self.plugin_manager.get_plugin_class(tts_provider)
            if tts_class:
                try:
                    config = self.plugin_manager._load_config().get(tts_provider, {})
                    if agent.config.voice.voice_id:
                        config["default_voice"] = agent.config.voice.voice_id
                    instance = tts_class(config)
                    if hasattr(instance, "set_plugin_manager"):
                        instance.set_plugin_manager(self.plugin_manager)
                    await instance.initialize()
                    agent.add_service("tts", instance)
                    logger.info(
                        f"[{agent.name}] Auto-initialized TTS service: {tts_provider}"
                    )
                except Exception as e:
                    logger.warning(
                        f"[{agent.name}] Failed to init TTS {tts_provider}: {e}"
                    )

        for svc_config in agent.config.services:
            svc_name = svc_config.name
            provider = svc_config.provider or svc_name

            # Get plugin class from PluginManager
            plugin_class = self.plugin_manager.get_plugin_class(provider)
            if not plugin_class:
                msg = f"Service '{provider}' not found or not marked as per-agent"
                if svc_config.required:
                    raise AgentStartupError(
                        f"Required service '{svc_name}' failed: {msg}"
                    )
                logger.warning(f"[{agent.name}] {msg}, skipping")
                continue

            # Build config, resolving secrets
            resolved_config = self._resolve_service_config(svc_config.config)

            try:
                # Instantiate the service
                instance = plugin_class(resolved_config)

                # Set plugin manager reference if supported
                if hasattr(instance, "set_plugin_manager"):
                    instance.set_plugin_manager(self.plugin_manager)

                # Initialize the service
                await instance.initialize()

                # Register in agent
                agent.add_service(svc_name, instance)
                logger.info(f"[{agent.name}] Initialized service: {svc_name}")

            except Exception as e:
                msg = f"Failed to initialize service '{svc_name}': {e}"
                if svc_config.required:
                    raise AgentStartupError(msg)
                logger.warning(f"[{agent.name}] {msg}, continuing without it")

    def _resolve_service_config(self, config: dict) -> dict:
        """Resolve *_secret fields in service configuration.

        Args:
            config: Service configuration dict

        Returns:
            Config with secrets resolved from environment variables
        """
        import os

        resolved = {}
        for key, value in config.items():
            if key.endswith("_secret") and isinstance(value, str):
                # This is a reference to an environment variable
                actual_key = key[:-7]  # Remove "_secret" suffix
                env_value = os.environ.get(value)
                if env_value:
                    resolved[actual_key] = env_value
                else:
                    logger.warning(f"Environment variable not set: {value}")
            elif isinstance(value, dict):
                resolved[key] = self._resolve_service_config(value)
            else:
                resolved[key] = value
        return resolved

    def set_message_handlers(self, handler_factory) -> None:
        """Set message handlers for all agent channels.

        This should be called after agents are loaded to avoid circular imports.
        The factory is saved for use during single-agent hot-reloads.

        Args:
            handler_factory: Callable that takes (plugin_manager, agent_context)
                            and returns a message handler function
        """
        self._handler_factory = handler_factory
        for agent in self._agents.values():
            for channel in agent._channels.values():
                handler = handler_factory(
                    self.plugin_manager, channel.get_agent_context()
                )
                channel.set_message_handler(handler)

    async def start_all(self) -> None:
        """Start all loaded agents."""
        if not self._agents:
            logger.warning("No agents to start")
            return

        logger.info(f"Starting {len(self._agents)} agent(s)...")

        # Start all agents concurrently
        start_tasks = [agent.start() for agent in self._agents.values()]
        await asyncio.gather(*start_tasks, return_exceptions=True)

        # Count successful starts
        running = sum(
            1
            for a in self._agents.values()
            if a.state in (AgentState.RUNNING, AgentState.DEGRADED)
        )
        logger.info(f"Started {running}/{len(self._agents)} agent(s)")

        # Start health check background task
        self._health_check_task = asyncio.create_task(self._health_check_loop())

    async def stop_all(self) -> None:
        """Stop all running agents."""
        logger.info("Stopping all agents...")

        # Signal health check to stop
        self._stop_event.set()
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass

        # Stop all agents concurrently
        stop_tasks = [agent.stop() for agent in self._agents.values()]
        await asyncio.gather(*stop_tasks, return_exceptions=True)

        logger.info("All agents stopped")

    async def start_agent(self, name: str) -> bool:
        """Start a specific agent.

        Args:
            name: Agent name/ID

        Returns:
            True if started successfully
        """
        agent = self._agents.get(name)
        if not agent:
            logger.error(f"Agent not found: {name}")
            return False

        await agent.start()
        return agent.state in (AgentState.RUNNING, AgentState.DEGRADED)

    async def stop_agent(self, name: str) -> bool:
        """Stop a specific agent.

        Args:
            name: Agent name/ID

        Returns:
            True if stopped successfully
        """
        agent = self._agents.get(name)
        if not agent:
            logger.error(f"Agent not found: {name}")
            return False

        await agent.stop()
        return agent.state == AgentState.STOPPED

    async def restart_agent(self, name: str) -> bool:
        """Restart a specific agent.

        Args:
            name: Agent name/ID

        Returns:
            True if restarted successfully
        """
        await self.stop_agent(name)
        return await self.start_agent(name)

    async def reload_all(self, handler_factory=None) -> dict:
        """Reload all agents from the database.

        Stops all agents, clears them, reloads from DB, and restarts.

        Args:
            handler_factory: Optional callable to set message handlers.
                            If not provided, agents start without handlers.

        Returns:
            Dict with reload status: {success: bool, agents_loaded: int, errors: list}
        """
        result = {"success": True, "agents_loaded": 0, "errors": []}

        try:
            # Stop all running agents
            logger.info("Reload: Stopping all agents...")
            await self.stop_all()

            # Clear agent registry
            self._agents.clear()
            self._stop_event.clear()

            # Reload all agents from DB
            logger.info("Reload: Loading agent configurations...")
            agents = await self.load_all()
            result["agents_loaded"] = len(agents)

            if not agents:
                result["errors"].append("No agents found to load")
                result["success"] = False
                return result

            # Set message handlers if factory provided
            if handler_factory:
                self.set_message_handlers(handler_factory)

            # Start all agents
            logger.info("Reload: Starting agents...")
            await self.start_all()

            logger.info(f"Reload complete: {len(agents)} agent(s) loaded and started")

        except Exception as e:
            logger.error(f"Reload failed: {e}")
            result["success"] = False
            result["errors"].append(str(e))

        return result

    async def reload_agent(self, agent_id: str) -> bool:
        """Hot-reload a single agent from DB config.

        Atomic swap: creates new Agent before stopping old one.
        Returns True if reload succeeded.
        """
        # Build new agent from fresh DB config
        new_agent = await self._load_agent(agent_id)
        if not new_agent:
            logger.error("reload_agent: failed to load %s from DB", agent_id)
            return False

        # Stop old agent if exists
        old_agent = self._agents.get(agent_id)
        if old_agent:
            try:
                await old_agent.stop()
                logger.info("Stopped old agent: %s", agent_id)
            except Exception as e:
                logger.warning("Error stopping old agent %s: %s", agent_id, e)

        # Swap in registry
        self._agents[agent_id] = new_agent

        # Set message handlers on new channels (same factory as initial load)
        if self._handler_factory:
            for channel in new_agent._channels.values():
                handler = self._handler_factory(
                    self.plugin_manager, channel.get_agent_context()
                )
                channel.set_message_handler(handler)

        # Start new agent
        try:
            await new_agent.start()
            logger.info("Reloaded agent: %s", agent_id)
        except Exception as e:
            logger.error("Failed to start reloaded agent %s: %s", agent_id, e)
            return False

        # Invalidate MCP gateway cache
        try:
            from core.mcp_gateway.server import invalidate_agent_config_cache

            invalidate_agent_config_cache(agent_id)
        except Exception:
            pass

        return True

    def get_agent(self, name: str) -> Agent | None:
        """Get agent by name.

        Args:
            name: Agent name/ID

        Returns:
            Agent instance or None if not found
        """
        return self._agents.get(name)

    def list_agents(self) -> list[dict]:
        """List all agents with their current status.

        Returns:
            List of agent info dictionaries
        """
        return [agent.get_health() for agent in self._agents.values()]

    async def _health_check_loop(self) -> None:
        """Background task for periodic health checks."""
        interval = HEALTH_CHECK_CONFIG["interval_seconds"]

        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(interval)
                if self._stop_event.is_set():
                    break

                for agent in self._agents.values():
                    if agent.state == AgentState.RUNNING:
                        # TODO: Implement actual health checks
                        # For now, just log status
                        logger.debug(
                            f"Health check: {agent.name} - {agent.state.value}"
                        )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health check error: {e}")

    # -------------------------------------------------------------------------
    # Inter-Agent Communication
    # -------------------------------------------------------------------------

    def get_agents_info(self) -> list[dict]:
        """Get information about all agents for context injection.

        Returns:
            List of dicts with {id, name, description}
        """
        return [
            {
                "id": agent.name,
                "name": agent.display_name,
                "description": agent.config.description,
            }
            for agent in self._agents.values()
        ]

    async def send_inter_agent_message(
        self,
        from_agent: str,
        to_agent: str,
        message: str,
        context: dict | None = None,
    ) -> str:
        """Send a message from one agent to another.

        Args:
            from_agent: ID of the sending agent
            to_agent: ID of the receiving agent
            message: Message to send
            context: Optional context (user info, depth tracking, etc.)

        Returns:
            Response from the receiving agent

        Raises:
            AgentNotFoundError: If the target agent doesn't exist
            AgentUnavailableError: If the target agent is not RUNNING
        """
        context = context or {}
        depth = context.get("inter_agent_depth", 0)

        # Check depth limit
        if depth >= MAX_INTER_AGENT_DEPTH:
            logger.warning(
                f"Inter-agent depth limit reached: {from_agent} -> {to_agent}"
            )
            return "Limite di comunicazione inter-agente raggiunto."

        # Update depth for nested calls
        context["inter_agent_depth"] = depth + 1

        # Get target agent
        target = self._agents.get(to_agent)
        if not target:
            logger.warning(
                f"Inter-agent: Agent '{to_agent}' not found. Available: {list(self._agents.keys())}"
            )
            raise AgentNotFoundError(f"Agent '{to_agent}' not found")

        logger.debug(
            f"Inter-agent: Target agent '{to_agent}' state: {target.state.value}"
        )
        if target.state != AgentState.RUNNING:
            raise AgentUnavailableError(
                f"Agent '{to_agent}' is not available (state: {target.state.value})"
            )

        logger.info(f"Inter-agent: {from_agent} -> {to_agent}: {message[:100]}...")

        effective_timeout = context.get("timeout") or INTER_AGENT_TIMEOUT

        try:
            response = await asyncio.wait_for(
                self._dispatch_to_agent(to_agent, from_agent, message, context),
                timeout=effective_timeout,
            )
            logger.info(
                f"Inter-agent response: {to_agent} -> {from_agent}: {response[:100]}..."
            )
            return response
        except asyncio.TimeoutError:
            logger.warning(f"Inter-agent timeout: {from_agent} -> {to_agent}")
            return f"Timeout: {to_agent} non ha risposto entro {int(effective_timeout)} secondi."
        except (AgentNotFoundError, AgentUnavailableError):
            raise
        except Exception as e:
            logger.error(f"Inter-agent error: {from_agent} -> {to_agent}: {e}")
            return f"Errore nella comunicazione: {str(e)}"

    async def _dispatch_to_agent(
        self,
        agent_id: str,
        from_agent: str,
        message: str,
        context: dict,
    ) -> str:
        """Dispatch a message to an agent and get the response.

        Args:
            agent_id: Target agent ID
            from_agent: Source agent ID
            message: Message content
            context: Message context (may include 'attachments' list of file paths)

        Returns:
            Agent's response
        """
        agent = self._agents.get(agent_id)
        if not agent:
            raise AgentNotFoundError(f"Agent '{agent_id}' not found")

        # Format the message to indicate it's from another agent
        formatted_message = f"[Da {from_agent.title()}]: {message}"

        # Include attachment contents in the message if present
        attachments = context.get("attachments", [])
        if attachments:
            attachment_contents = []
            for path in attachments:
                try:
                    with open(path, "r") as f:
                        content = f.read()
                    filename = path.split("/")[-1]
                    attachment_contents.append(
                        f"\n[Allegato: {filename}]\n```\n{content}\n```"
                    )
                except Exception as e:
                    logger.warning(f"Failed to read attachment {path}: {e}")

            if attachment_contents:
                formatted_message += "\n" + "\n".join(attachment_contents)

        # Build context for the inter-agent message
        inter_agent_context = {
            "source": context.get("source", "inter_agent"),
            "from_agent": from_agent,
            "original_user_id": context.get("original_user_id"),
            "original_username": context.get("original_username"),
            "inter_agent_depth": context.get("inter_agent_depth", 1),
            "attachments": attachments,  # Pass through for further processing
            "mcp_permissions": context.get("mcp_permissions"),
        }

        # Use the agent's message processor
        response = await agent.process_inter_agent_message(
            formatted_message, inter_agent_context
        )

        return response

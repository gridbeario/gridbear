"""GridBear Main Entry Point.

Plugin-based architecture for AI assistant with multiple channels and services.
"""

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

from config.logging_config import logger
from config.settings import (
    BASE_DIR,
    get_group_shared_accounts,
    get_unified_user_id,
    get_user_locale,
)
from core.hooks import HookData, HookName
from core.interfaces.channel import Message, UserInfo
from core.permissions.mcp_resolver import resolve_permissions
from core.plugin_manager import PluginManager
from sessions.context_builder import ContextBuilder, build_inter_agent_context
from sessions.context_builder import UserInfo as ContextUserInfo

CLEANUP_INTERVAL_HOURS = 1


def _preflight_check() -> None:
    """Validate required configuration before startup."""
    if not os.environ.get("DATABASE_URL"):
        logger.error(
            "DATABASE_URL is not set. Ensure POSTGRES_PASSWORD is set in .env — "
            "docker-compose builds DATABASE_URL from it automatically."
        )
        sys.exit(1)

    if not os.environ.get("INTERNAL_API_SECRET"):
        logger.warning(
            "INTERNAL_API_SECRET is not set. "
            "WebChat and internal API calls will fail. "
            "Generate one with: openssl rand -hex 32"
        )


def _expand_shared_accounts(
    plugin_manager: PluginManager,
    plugin_name: str,
    accounts: dict[str, list[str]],
) -> list[str]:
    """Expand shared account emails to MCP server names via manifest template."""
    if not accounts:
        return []
    manifest = plugin_manager.get_plugin_manifest(plugin_name)
    if not manifest:
        return []
    template = manifest.get("mcp_name_template", f"{plugin_name}-{{account}}")
    servers = []
    for emails in accounts.values():
        for email in emails:
            servers.append(template.format(account=email))
    return servers


class MessageProcessor:
    """Central message processor that coordinates plugins."""

    def __init__(self, plugin_manager: PluginManager):
        self.plugin_manager = plugin_manager
        self.hooks = plugin_manager.hooks

    async def process_message(
        self,
        message: Message,
        user_info: UserInfo,
        progress_callback=None,
        error_callback=None,
        tool_callback=None,
        stream_callback=None,
    ) -> str:
        runner = self.plugin_manager.get_runner()
        if not runner:
            return "No runner available."

        sessions_service = self.plugin_manager.get_service("sessions")
        memory_service = self.plugin_manager.get_service("memory")

        # Create hook data object
        hook_data = HookData(
            text=message.text,
            platform=message.platform,
            user_id=user_info.user_id,
            username=user_info.username,
            attachments=message.attachments or [],
        )

        # HOOK: on_message_received
        hook_data = await self.hooks.execute(
            HookName.ON_MESSAGE_RECEIVED,
            hook_data,
            message=message,
            user_info=user_info,
        )

        # Check if hook wants to skip processing
        if hook_data.extra.get("skip_processing"):
            return hook_data.extra.get("skip_response", "")

        session = None
        if sessions_service:
            # For webchat, use conversation_id as session key so each
            # conversation gets its own runner session (separate Claude context).
            session_user_id = user_info.user_id
            session_platform = message.platform
            conv_id = message.channel_metadata.get("conversation_id")
            if conv_id and message.platform == "webchat":
                session_platform = f"webchat:{conv_id}"

            session = await sessions_service.get_session(
                session_user_id, session_platform
            )
            if not session:
                session = await sessions_service.create_session(
                    session_user_id, session_platform
                )
                # HOOK: on_session_created
                await self.hooks.execute(
                    HookName.ON_SESSION_CREATED,
                    {"session": session, "user_info": user_info},
                )

        context_user_info = ContextUserInfo(
            user_id=user_info.user_id,
            username=user_info.username,
            first_name=user_info.display_name,
            last_name=None,
            platform=message.platform,
        )

        username_lower = user_info.username.lower() if user_info.username else None

        # Use explicit unified_id from UserInfo (e.g. webchat passes DB value)
        # before falling back to platform:username resolution.
        unified_id = getattr(user_info, "unified_id", None)
        user_locale = "en"
        if not unified_id and username_lower:
            unified_id = get_unified_user_id(message.platform, username_lower)

        extra_servers = []
        shared_accounts = {}
        if unified_id:
            shared_accounts = get_group_shared_accounts(unified_id)
            for plugin_name, accounts in shared_accounts.items():
                extra_servers.extend(
                    _expand_shared_accounts(self.plugin_manager, plugin_name, accounts)
                )
            user_locale = get_user_locale(unified_id) or user_locale

        expanded_permissions = resolve_permissions(
            username=username_lower,
            platform=message.platform,
            is_group_chat=message.is_group_chat,
            unified_id=unified_id,
            extra_servers=extra_servers,
        )

        hook_data.mcp_permissions = expanded_permissions

        # HOOK: before_context_build
        hook_data = await self.hooks.execute(
            HookName.BEFORE_CONTEXT_BUILD,
            hook_data,
            user_info=context_user_info,
            shared_accounts=shared_accounts,
        )

        builder = ContextBuilder(
            memory_service=memory_service,
            sessions_service=sessions_service,
        )
        builder.add_user_info(context_user_info)
        builder.set_locale(user_locale)
        builder.set_voice_response(message.respond_with_voice)
        builder.set_allowed_mcp_servers(hook_data.mcp_permissions)
        builder.set_shared_accounts(shared_accounts)
        builder.set_plugin_context(
            await self.plugin_manager.get_all_context_injections(unified_id=unified_id)
        )
        if message.context_prompt:
            builder.set_conversation_context(message.context_prompt)
        if message.channel_metadata:
            builder.set_channel_metadata(message.channel_metadata)
            conv_docs = message.channel_metadata.get("conversation_documents")
            if conv_docs:
                builder.set_conversation_documents(conv_docs)

        if hook_data.attachments:
            for attachment in hook_data.attachments:
                builder.add_attachment_reference(
                    Path(attachment), Path(attachment).name
                )
            if hook_data.text:
                builder.add_text(hook_data.text)
        else:
            builder.add_text(hook_data.text)

        # Detect cold start: new session or no runner session ID means process
        # was recycled — load more history so Claude can recover context
        is_cold_start = bool(session and not session.runner_session_id)

        # Load memories and chat history in parallel for better performance
        await asyncio.gather(
            builder.include_memories(limit=5),
            builder.include_chat_history(
                limit=20 if is_cold_start else 10,
                max_chars=6000 if is_cold_start else 3000,
                max_hours=72 if is_cold_start else 24,
                cold_start=is_cold_start,
            ),
            builder.include_chat_history_if_relevant(hook_data.text),
        )

        full_prompt = builder.build()

        # DEBUG: check if history made it into the prompt
        import logging as _logging

        _dbg = _logging.getLogger("gridbear")
        if "Recent Conversation History" in full_prompt:
            idx = full_prompt.index("Recent Conversation History")
            _dbg.info(
                "PROMPT CONTAINS HISTORY at char %d. Snippet: ...%s...",
                idx,
                full_prompt[max(0, idx - 100) : idx + 300],
            )
        else:
            _dbg.warning(
                "PROMPT DOES NOT CONTAIN HISTORY! prompt length=%d", len(full_prompt)
            )

        hook_data.prompt = full_prompt
        hook_data.session_id = session.runner_session_id if session else None

        # HOOK: after_context_build
        hook_data = await self.hooks.execute(
            HookName.AFTER_CONTEXT_BUILD,
            hook_data,
            builder=builder,
        )

        if sessions_service and session:
            await sessions_service.add_message(session.id, "user", hook_data.text)

        # HOOK: before_runner_call
        hook_data = await self.hooks.execute(
            HookName.BEFORE_RUNNER_CALL,
            hook_data,
            runner=runner,
        )

        # Get agent_id for process pooling (if available in subclass)
        agent_id = (
            getattr(self, "agent_context", {}).get("name")
            if hasattr(self, "agent_context")
            else None
        )

        # Get agent-level model override (from agent YAML config)
        agent_model = (
            self.agent_context.get("model") if hasattr(self, "agent_context") else None
        )

        response = await runner.run(
            prompt=hook_data.prompt,
            session_id=hook_data.session_id,
            progress_callback=progress_callback,
            error_callback=error_callback,
            tool_callback=tool_callback,
            stream_callback=stream_callback,
            agent_id=agent_id,
            model=agent_model or None,
            unified_id=unified_id,
        )

        hook_data.response_text = response.text
        hook_data.session_id = response.session_id

        # HOOK: after_runner_call
        hook_data = await self.hooks.execute(
            HookName.AFTER_RUNNER_CALL,
            hook_data,
            response=response,
        )

        if sessions_service and session:
            if response.session_id and response.session_id != session.runner_session_id:
                await sessions_service.update_runner_session_id(
                    session.id, response.session_id
                )
            await sessions_service.touch_session(session.id)
            await sessions_service.add_message(
                session.id, "assistant", hook_data.response_text
            )

        # HOOK: before_memory_store
        memory_data = await self.hooks.execute(
            HookName.BEFORE_MEMORY_STORE,
            {
                "user_text": hook_data.text,
                "assistant_text": hook_data.response_text,
                "platform": hook_data.platform,
                "username": username_lower,
            },
        )

        if memory_service and memory_service.enabled and username_lower:
            if not memory_data.get("skip_memory"):
                user_text = memory_data.get("user_text", hook_data.text)
                assistant_text = memory_data.get(
                    "assistant_text", hook_data.response_text
                )
                unified_id = get_unified_user_id(hook_data.platform, username_lower)

                # Fire-and-forget: store memories in background (don't block response)
                asyncio.create_task(
                    memory_service.add_episodic_memory(
                        user_message=user_text,
                        assistant_response=assistant_text,
                        user_id=unified_id,
                        platform=hook_data.platform,
                    )
                )

                memory_content = f"User: {user_text}\nAssistant: {assistant_text}"
                mem_metadata = {}
                if message.channel_metadata:
                    mem_metadata["channel"] = message.channel_metadata.get(
                        "channel", ""
                    )
                    if message.channel_metadata.get("conversation_id"):
                        mem_metadata["conversation_id"] = message.channel_metadata[
                            "conversation_id"
                        ]
                    if message.channel_metadata.get("conversation_context"):
                        mem_metadata["conversation_context"] = message.channel_metadata[
                            "conversation_context"
                        ]
                asyncio.create_task(
                    memory_service.add_declarative_memory(
                        content=memory_content,
                        user_id=unified_id,
                        metadata=mem_metadata if mem_metadata else None,
                    )
                )

                # HOOK: after_memory_store (non-blocking)
                asyncio.create_task(
                    self.hooks.execute(
                        HookName.AFTER_MEMORY_STORE,
                        {
                            "content": memory_content,
                            "platform": hook_data.platform,
                            "username": username_lower,
                            "memory_types": ["episodic", "declarative"],
                        },
                    )
                )

        # HOOK: before_send_response
        hook_data = await self.hooks.execute(
            HookName.BEFORE_SEND_RESPONSE,
            hook_data,
        )

        return hook_data.response_text


class AgentAwareMessageProcessor(MessageProcessor):
    """Message processor that includes agent-specific context.

    Extends MessageProcessor to inject agent identity and configuration
    into the context builder for multi-agent mode.
    """

    def __init__(self, plugin_manager: PluginManager, agent_context: dict):
        """Initialize processor with agent context.

        Args:
            plugin_manager: Plugin manager instance
            agent_context: Agent configuration from channel.get_agent_context()
                - name: Agent internal name
                - display_name: Agent display name
                - system_prompt: Agent personality
                - voice: Voice configuration dict
                - mcp_permissions: Agent's allowed MCP servers
                - locale: Agent's default locale
        """
        super().__init__(plugin_manager)
        self.agent_context = agent_context

    async def process_message(
        self,
        message: Message,
        user_info: UserInfo,
        inter_agent_context: dict | None = None,
        progress_callback=None,
        error_callback=None,
        tool_callback=None,
        stream_callback=None,
    ) -> str:
        """Process message with agent-specific context.

        Args:
            message: The message to process
            user_info: User information
            inter_agent_context: Optional context for inter-agent messages
            progress_callback: Optional async callback for progress messages.
                              Signature: async def callback(message: str)
            error_callback: Optional async callback for error notifications.
                           Signature: async def callback(error_type: str, details: dict)
            tool_callback: Optional async callback for tool use notifications.
                          Signature: async def callback(tool_name: str, tool_input: dict)
        """
        runner_name = self.agent_context.get("runner") or None
        runner = self.plugin_manager.get_runner(runner_name)
        if not runner:
            return "No runner available."

        # Simplified processing for inter-agent messages
        if inter_agent_context and inter_agent_context.get("source") == "inter_agent":
            return await self._process_inter_agent_message(
                message, inter_agent_context, runner
            )

        sessions_service = self.plugin_manager.get_service("sessions")
        memory_service = self.plugin_manager.get_service("memory")

        # Create hook data object
        hook_data = HookData(
            text=message.text,
            platform=message.platform,
            user_id=user_info.user_id,
            username=user_info.username,
            attachments=message.attachments or [],
        )

        # HOOK: on_message_received
        hook_data = await self.hooks.execute(
            HookName.ON_MESSAGE_RECEIVED,
            hook_data,
            message=message,
            user_info=user_info,
        )

        # Check if hook wants to skip processing
        if hook_data.extra.get("skip_processing"):
            return hook_data.extra.get("skip_response", "")

        session = None
        if sessions_service:
            # For webchat, use conversation_id as session key so each
            # conversation gets its own runner session (separate Claude context).
            session_user_id = user_info.user_id
            session_platform = message.platform
            conv_id = message.channel_metadata.get("conversation_id")
            if conv_id and message.platform == "webchat":
                session_platform = f"webchat:{conv_id}"

            session = await sessions_service.get_session(
                session_user_id, session_platform
            )
            if not session:
                session = await sessions_service.create_session(
                    session_user_id, session_platform
                )
                await self.hooks.execute(
                    HookName.ON_SESSION_CREATED,
                    {"session": session, "user_info": user_info},
                )

        context_user_info = ContextUserInfo(
            user_id=user_info.user_id,
            username=user_info.username,
            first_name=user_info.display_name,
            last_name=None,
            platform=message.platform,
        )

        username_lower = user_info.username.lower() if user_info.username else None

        # Get agent-level MCP permissions
        agent_mcp_permissions = self.agent_context.get("mcp_permissions", [])

        # Use explicit unified_id from UserInfo (e.g. webchat passes DB value)
        # before falling back to platform:username resolution.
        unified_id = getattr(user_info, "unified_id", None)
        user_locale = self.agent_context.get("locale", "en")

        if not unified_id and username_lower:
            unified_id = get_unified_user_id(message.platform, username_lower)

        # Service account override: use the agent's own identity for MCP
        # permissions and gateway context (e.g. bot agents with their own
        # Odoo credentials). The sender's unified_id is kept for memories.
        ctx_opts = self.agent_context.get("context_options", {})
        service_account = ctx_opts.get("service_account")
        mcp_unified_id = service_account if service_account else unified_id

        extra_servers = []
        shared_accounts = {}
        if mcp_unified_id:
            shared_accounts = get_group_shared_accounts(mcp_unified_id)
            for plugin_name, accounts in shared_accounts.items():
                extra_servers.extend(
                    _expand_shared_accounts(self.plugin_manager, plugin_name, accounts)
                )
        if unified_id:
            # User locale can override agent locale if set
            user_locale = get_user_locale(unified_id) or user_locale

        expanded_permissions = resolve_permissions(
            username=username_lower,
            platform=message.platform,
            is_group_chat=message.is_group_chat,
            unified_id=mcp_unified_id,
            agent_mcp_permissions=agent_mcp_permissions or None,
            extra_servers=extra_servers,
        )

        hook_data.mcp_permissions = expanded_permissions

        # HOOK: before_context_build
        hook_data = await self.hooks.execute(
            HookName.BEFORE_CONTEXT_BUILD,
            hook_data,
            user_info=context_user_info,
            shared_accounts=shared_accounts,
        )

        builder = ContextBuilder(
            memory_service=memory_service,
            sessions_service=sessions_service,
        )
        builder.add_user_info(context_user_info)
        builder.set_locale(user_locale)
        builder.set_voice_response(message.respond_with_voice)
        builder.set_allowed_mcp_servers(hook_data.mcp_permissions)

        if not ctx_opts.get("skip_calendars"):
            builder.set_shared_accounts(shared_accounts)
        if not ctx_opts.get("skip_plugin_context"):
            builder.set_plugin_context(
                await self.plugin_manager.get_all_context_injections(
                    unified_id=unified_id
                )
            )

        # Set agent identity (key addition for multi-agent mode)
        builder.set_agent_identity(
            name=self.agent_context.get("name", ""),
            display_name=self.agent_context.get("display_name", ""),
            system_prompt=self.agent_context.get("system_prompt"),
        )
        # Set agent-specific email settings
        builder.set_agent_email_settings(self.agent_context.get("email"))
        # Set tool loading mode (full or search)
        builder.set_tool_loading(self.agent_context.get("tool_loading", "full"))
        # Set per-conversation context if provided
        if message.context_prompt:
            builder.set_conversation_context(message.context_prompt)
        if message.channel_metadata:
            builder.set_channel_metadata(message.channel_metadata)
            conv_docs = message.channel_metadata.get("conversation_documents")
            if conv_docs:
                builder.set_conversation_documents(conv_docs)

        # Inject inter-agent context if multi-agent
        if not ctx_opts.get("skip_inter_agent"):
            from core.registry import get_agent_manager

            agent_mgr = get_agent_manager()
            if agent_mgr:
                agents_info = agent_mgr.get_agents_info()
                agent_name = self.agent_context.get("name", "")
                inter_agent_ctx = build_inter_agent_context(agents_info, agent_name)
                if inter_agent_ctx:
                    builder.add_text(inter_agent_ctx)

        if hook_data.attachments:
            for attachment in hook_data.attachments:
                builder.add_attachment_reference(
                    Path(attachment), Path(attachment).name
                )
            if hook_data.text:
                builder.add_text(hook_data.text)
        else:
            builder.add_text(hook_data.text)

        # Detect cold start: new session or no runner session ID means process
        # was recycled — load more history so Claude can recover context
        is_cold_start = bool(session and not session.runner_session_id)

        mem_limit = ctx_opts.get("memories", 5)
        hist_limit = ctx_opts.get("history_limit", 20 if is_cold_start else 10)
        hist_chars = ctx_opts.get("history_max_chars", 6000 if is_cold_start else 3000)
        hist_hours = ctx_opts.get("history_max_hours", 72 if is_cold_start else 24)

        # Load memories and chat history in parallel for better performance
        tasks = []
        if mem_limit > 0:
            tasks.append(builder.include_memories(limit=mem_limit))
        if hist_limit > 0:
            tasks.append(
                builder.include_chat_history(
                    limit=hist_limit,
                    max_chars=hist_chars,
                    max_hours=hist_hours,
                    cold_start=is_cold_start,
                )
            )
            if not ctx_opts.get("skip_relevant_history"):
                tasks.append(builder.include_chat_history_if_relevant(hook_data.text))
        if tasks:
            await asyncio.gather(*tasks)

        full_prompt = builder.build()

        # Plan execution: inject active task into prompt
        _active_task = None
        _conv_id = (
            message.channel_metadata.get("conversation_id")
            if message.channel_metadata
            else None
        )
        if _conv_id:
            try:
                from plugins.planning.plan_executor import (
                    build_task_prompt_injection,
                    get_active_plan_task,
                    mark_task_in_progress,
                )

                _active_task = get_active_plan_task(_conv_id)
                if _active_task:
                    full_prompt += build_task_prompt_injection(_active_task)
                    await mark_task_in_progress(_active_task["task_id"], _conv_id)
            except Exception as exc:
                logger.debug("Plan executor pre-run failed: %s", exc)

        hook_data.prompt = full_prompt
        # Workflow steps get a fresh runner session (no --resume) to avoid
        # contamination from previous conversations.
        _source = inter_agent_context.get("source", "") if inter_agent_context else ""
        _session_ttl_expired = False
        if _source == "workflow":
            hook_data.session_id = None
        else:
            hook_data.session_id = session.runner_session_id if session else None

            # Session TTL: invalidate runner session if too old
            session_ttl = ctx_opts.get("session_ttl_minutes")
            if session_ttl and session and session.runner_session_id:
                try:
                    from datetime import datetime, timedelta, timezone

                    last_update = session.updated_at
                    if last_update.tzinfo is None:
                        last_update = last_update.replace(tzinfo=timezone.utc)
                    age = datetime.now(timezone.utc) - last_update
                    if age > timedelta(minutes=int(session_ttl)):
                        logger.info(
                            "Session TTL expired (%d min), starting fresh",
                            session_ttl,
                        )
                        hook_data.session_id = None
                        _session_ttl_expired = True
                except Exception as exc:
                    logger.debug("Session TTL check failed: %s", exc)

        # HOOK: after_context_build
        hook_data = await self.hooks.execute(
            HookName.AFTER_CONTEXT_BUILD,
            hook_data,
            builder=builder,
        )

        if sessions_service and session:
            await sessions_service.add_message(session.id, "user", hook_data.text)

        # HOOK: before_runner_call
        hook_data = await self.hooks.execute(
            HookName.BEFORE_RUNNER_CALL,
            hook_data,
            runner=runner,
        )

        # Get agent_id for process pooling (if available in subclass)
        agent_id = (
            getattr(self, "agent_context", {}).get("name")
            if hasattr(self, "agent_context")
            else None
        )

        # Get agent-level model override (from agent YAML config)
        agent_model = (
            self.agent_context.get("model") if hasattr(self, "agent_context") else None
        )

        # Pass system_prompt separately for runners that support it (e.g. Gemini)
        agent_system_prompt = self.agent_context.get("system_prompt", "")

        # Agent-level max_tools override (from agent YAML config)
        agent_max_tools = (
            self.agent_context.get("max_tools")
            if hasattr(self, "agent_context")
            else None
        )

        # Agent-level tool_loading mode (full or search)
        agent_tool_loading = (
            self.agent_context.get("tool_loading", "full")
            if hasattr(self, "agent_context")
            else "full"
        )

        response = await runner.run(
            prompt=hook_data.prompt,
            session_id=hook_data.session_id,
            progress_callback=progress_callback,
            error_callback=error_callback,
            tool_callback=tool_callback,
            stream_callback=stream_callback,
            agent_id=agent_id,
            model=agent_model or None,
            system_prompt=agent_system_prompt,
            unified_id=mcp_unified_id,
            max_tools=agent_max_tools,
            tool_loading=agent_tool_loading,
        )

        # Fallback runner: retry with alternative runner on error
        if response.is_error:
            fb_name = self.agent_context.get("fallback_runner")
            if fb_name:
                fb_runner = self.plugin_manager.get_runner(fb_name)
                if fb_runner and fb_runner is not runner:
                    logger.warning(
                        "[%s] Primary runner failed, trying fallback: %s",
                        agent_id,
                        fb_name,
                    )
                    response = await fb_runner.run(
                        prompt=hook_data.prompt,
                        session_id=None,
                        progress_callback=progress_callback,
                        error_callback=error_callback,
                        tool_callback=tool_callback,
                        stream_callback=stream_callback,
                        agent_id=agent_id,
                        system_prompt=agent_system_prompt,
                    )

        hook_data.response_text = response.text
        hook_data.session_id = response.session_id

        # Prefix response with TTL notice if session was reset
        if _session_ttl_expired and not response.is_error and response.text:
            ttl_msg = ctx_opts.get(
                "session_ttl_message",
                "_(Conversazione precedente chiusa per inattività)_",
            )
            hook_data.response_text = f"{ttl_msg}\n\n{response.text}"

        # Plan execution: mark task completed after runner response
        if _active_task and _conv_id and not response.is_error:
            try:
                from plugins.planning.plan_executor import (
                    mark_task_completed,
                    truncate_result,
                )

                await mark_task_completed(
                    task_id=_active_task["task_id"],
                    conversation_id=_conv_id,
                    result=truncate_result(response.text),
                    plan_id=_active_task["plan_id"],
                )
            except Exception as exc:
                logger.debug("Plan executor post-run failed: %s", exc)

        # HOOK: after_runner_call
        hook_data = await self.hooks.execute(
            HookName.AFTER_RUNNER_CALL,
            hook_data,
            response=response,
        )

        if sessions_service and session:
            if response.session_id and response.session_id != session.runner_session_id:
                await sessions_service.update_runner_session_id(
                    session.id, response.session_id
                )
            await sessions_service.touch_session(session.id)
            await sessions_service.add_message(
                session.id, "assistant", hook_data.response_text
            )

        # HOOK: before_memory_store
        memory_data = await self.hooks.execute(
            HookName.BEFORE_MEMORY_STORE,
            {
                "user_text": hook_data.text,
                "assistant_text": hook_data.response_text,
                "platform": hook_data.platform,
                "username": username_lower,
            },
        )

        if memory_service and memory_service.enabled and username_lower:
            if not memory_data.get("skip_memory"):
                user_text = memory_data.get("user_text", hook_data.text)
                assistant_text = memory_data.get(
                    "assistant_text", hook_data.response_text
                )
                unified_id = get_unified_user_id(hook_data.platform, username_lower)

                # Fire-and-forget: store memories in background (don't block response)
                asyncio.create_task(
                    memory_service.add_episodic_memory(
                        user_message=user_text,
                        assistant_response=assistant_text,
                        user_id=unified_id,
                        platform=hook_data.platform,
                    )
                )

                memory_content = f"User: {user_text}\nAssistant: {assistant_text}"
                mem_metadata = {}
                if message.channel_metadata:
                    mem_metadata["channel"] = message.channel_metadata.get(
                        "channel", ""
                    )
                    if message.channel_metadata.get("conversation_id"):
                        mem_metadata["conversation_id"] = message.channel_metadata[
                            "conversation_id"
                        ]
                    if message.channel_metadata.get("conversation_context"):
                        mem_metadata["conversation_context"] = message.channel_metadata[
                            "conversation_context"
                        ]
                asyncio.create_task(
                    memory_service.add_declarative_memory(
                        content=memory_content,
                        user_id=unified_id,
                        metadata=mem_metadata if mem_metadata else None,
                    )
                )

                # HOOK: after_memory_store (non-blocking)
                asyncio.create_task(
                    self.hooks.execute(
                        HookName.AFTER_MEMORY_STORE,
                        {
                            "content": memory_content,
                            "platform": hook_data.platform,
                            "username": username_lower,
                            "memory_types": ["episodic", "declarative"],
                        },
                    )
                )

        # HOOK: before_send_response
        hook_data = await self.hooks.execute(
            HookName.BEFORE_SEND_RESPONSE,
            hook_data,
        )

        return hook_data.response_text

    async def _process_inter_agent_message(
        self, message: Message, context: dict, runner
    ) -> str:
        """Process a message from another agent (simplified flow).

        Skips sessions, memory storage, and most hooks for efficiency.
        Enforces the original user's MCP permissions to prevent privilege escalation.
        """
        # Build minimal context with agent identity
        builder = ContextBuilder()
        builder.set_locale(self.agent_context.get("locale", "it"))
        builder.set_agent_identity(
            name=self.agent_context.get("name", ""),
            display_name=self.agent_context.get("display_name", ""),
            system_prompt=self.agent_context.get("system_prompt"),
        )
        builder.set_agent_email_settings(self.agent_context.get("email"))

        # Enforce MCP permissions based on call source
        caller_perms = context.get("mcp_permissions")
        source = context.get("source", "inter_agent")

        if source == "workflow":
            # Workflow engine is a system-level component — agent uses
            # its own configured permissions (no user to restrict).
            agent_perms = self.agent_context.get("mcp_permissions", [])
            builder.set_allowed_mcp_servers(agent_perms)
        elif caller_perms is not None:
            # Inter-agent: intersect caller and target permissions
            # to prevent privilege escalation.
            target_agent_perms = self.agent_context.get("mcp_permissions")
            if target_agent_perms:
                from core.permissions.mcp_resolver import matches_permission

                intersected = [
                    p for p in target_agent_perms if matches_permission(p, caller_perms)
                ]
            else:
                intersected = caller_perms
            builder.set_allowed_mcp_servers(intersected)
        else:
            builder.set_allowed_mcp_servers([])

        # Inject inter-agent context so target agent can also delegate
        from core.registry import get_agent_manager

        agent_mgr = get_agent_manager()
        if agent_mgr:
            agents_info = agent_mgr.get_agents_info()
            inter_agent_ctx = build_inter_agent_context(
                agents_info, self.agent_context.get("name", "")
            )
            if inter_agent_ctx:
                builder.add_text(inter_agent_ctx)

        builder.add_text(message.text)

        # Add depth info to prevent loops
        depth = context.get("inter_agent_depth", 0)
        builder.add_text(
            f"[Inter-Agent] Questo messaggio proviene da un altro agente. "
            f"Profondità chiamata: {depth}. Rispondi in modo conciso."
        )

        full_prompt = builder.build()

        agent_id = self.agent_context.get("name")

        response = await runner.run(
            prompt=full_prompt,
            session_id=None,  # No session for inter-agent messages
            agent_id=agent_id,
        )

        return response.text


async def restart_task(stop_event: asyncio.Event):
    """Watch for restart requests and trigger graceful shutdown with restart code."""
    import json

    restart_file = BASE_DIR / "data" / "restart_requested.json"

    while not stop_event.is_set():
        try:
            await asyncio.sleep(2)

            if not restart_file.exists():
                continue

            try:
                with open(restart_file) as f:
                    request = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

            if request.get("status") != "pending":
                continue

            logger.warning("Restart requested from admin panel - shutting down...")

            # Mark as processing
            request["status"] = "processing"
            with open(restart_file, "w") as f:
                json.dump(request, f, indent=2)

            # Trigger shutdown - exit code 1 will cause Docker to restart
            stop_event.set()

            # Store restart code for main() to use
            restart_file.write_text('{"status": "restarting"}')

            # Give time for graceful shutdown
            await asyncio.sleep(1)
            break

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Restart task error: {e}")


async def agent_reload_task(
    agent_manager, plugin_manager: PluginManager, stop_event: asyncio.Event
):
    """Watch for agent reload requests and execute them."""
    import json

    reload_file = BASE_DIR / "data" / "agent_reload.json"

    while not stop_event.is_set():
        try:
            await asyncio.sleep(2)  # Check every 2 seconds

            if not reload_file.exists():
                continue

            try:
                with open(reload_file) as f:
                    request = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

            if request.get("status") != "pending":
                continue

            logger.info("Agent reload requested from admin panel")

            # Create handler factory for reload
            def create_handler(pm, ctx):
                processor = AgentAwareMessageProcessor(pm, ctx)
                return processor.process_message

            try:
                result = await agent_manager.reload_all(handler_factory=create_handler)

                # Re-provision MCP tokens (mcp_permissions may have changed)
                from core.mcp_token_manager import get_mcp_token_manager

                tm = get_mcp_token_manager()
                if tm:
                    tm.provision_agent_tokens(list(agent_manager._agents.values()))
                    logger.info("MCP gateway tokens re-provisioned after reload")

                # Re-wire services via on_startup hooks
                await plugin_manager.hooks.execute(HookName.ON_STARTUP, {})

                request["status"] = "completed" if result["success"] else "error"
                request["agents_loaded"] = result["agents_loaded"]
                request["errors"] = result.get("errors", [])
                logger.info(f"Agent reload completed: {result['agents_loaded']} agents")
            except Exception as e:
                request["status"] = "error"
                request["errors"] = [str(e)]
                logger.error(f"Agent reload failed: {e}")

            # Write result back
            with open(reload_file, "w") as f:
                json.dump(request, f, indent=2)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Agent reload task error: {e}")


async def plugin_reload_task(plugin_manager: PluginManager, stop_event: asyncio.Event):
    """Watch for plugin reload requests and execute them."""
    import json

    reload_file = BASE_DIR / "data" / "reload_requests.json"

    while not stop_event.is_set():
        try:
            await asyncio.sleep(2)  # Check every 2 seconds

            if not reload_file.exists():
                continue

            try:
                with open(reload_file) as f:
                    requests = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

            if not requests:
                continue

            # Process pending requests
            processed = []
            for req in requests:
                if req.get("status") != "pending":
                    continue

                plugin_name = req.get("plugin")
                if not plugin_name:
                    continue

                if plugin_name == "__all__":
                    # Reload all plugins - requires restart for now
                    logger.info("Reload all plugins requested - use restart for now")
                    req["status"] = "skipped"
                    req["message"] = "Full reload requires restart"
                else:
                    logger.info(f"Reloading plugin: {plugin_name}")
                    result = await plugin_manager.reload_plugin(plugin_name)
                    req["status"] = result.get("status", "error")
                    req["message"] = result.get("message", "")

                processed.append(req)

            # Update file with results
            if processed:
                with open(reload_file, "w") as f:
                    json.dump(requests, f, indent=2)

                # Clear old requests (keep last 10)
                if len(requests) > 10:
                    with open(reload_file, "w") as f:
                        json.dump(requests[-10:], f, indent=2)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Plugin reload task error: {e}")


async def cleanup_task(plugin_manager: PluginManager, stop_event: asyncio.Event):
    """Periodically cleanup expired sessions and attachments."""
    while not stop_event.is_set():
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_HOURS * 3600)
            if stop_event.is_set():
                break

            sessions_service = plugin_manager.get_service("sessions")
            attachments_service = plugin_manager.get_service("attachments")

            if sessions_service:
                expired_sessions = await sessions_service.cleanup_expired()
                if expired_sessions > 0:
                    logger.info(f"Cleaned up {expired_sessions} expired sessions")
                    # HOOK: on_session_expired for each
                    for _ in range(expired_sessions):
                        await plugin_manager.hooks.execute(
                            HookName.ON_SESSION_EXPIRED, {}
                        )

            if attachments_service:
                count = await attachments_service.cleanup_expired()
                if count > 0:
                    logger.info(f"Cleaned up {count} expired attachment files")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Cleanup task error: {e}")


async def main():
    """Main entry point."""
    logger.info("Starting GridBear...")
    _preflight_check()

    # Initialize PostgreSQL connection pool
    db = None
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        try:
            from core.database import DatabaseManager
            from core.registry import set_database

            db = DatabaseManager(database_url)
            await db.initialize()
            set_database(db)

            # Initialize ORM: inject DB, discover models, run auto-migrations
            from core.orm import Registry as ORMRegistry

            ORMRegistry.initialize(db)

            # Attach DB log handler for WARNING+ persistence
            from config.logging_config import attach_db_log_handler

            attach_db_log_handler()

            # Initialize secrets manager with PG pool so plugins
            # can read/write secrets (tokens, API keys, etc.)
            from ui.secrets_manager import reset_secrets_manager

            reset_secrets_manager()

            # One-time migrations: config files -> PostgreSQL
            from core.config_migration import (
                migrate_admin_config_to_db,
                migrate_agent_configs_to_db,
                migrate_claude_settings_to_db,
                migrate_create_default_company,
                migrate_mcp_perms_to_unified_id,
                migrate_rest_api_config_to_db,
                migrate_unify_users,
                migrate_user_platforms,
            )

            await migrate_admin_config_to_db(BASE_DIR / "config" / "admin_config.json")
            await migrate_rest_api_config_to_db(BASE_DIR / "config" / "rest_api.json")
            await migrate_claude_settings_to_db(
                BASE_DIR / "config" / "claude_settings.json"
            )
            await migrate_mcp_perms_to_unified_id()
            await migrate_create_default_company()
            await migrate_unify_users()
            await migrate_user_platforms()
            await migrate_agent_configs_to_db()
        except Exception as e:
            logger.error(f"PostgreSQL initialization failed: {e}")
            raise

    # Build plugin path resolver (supports EXTRA_PLUGINS_DIRS env var)
    from core.plugin_paths import PluginPathResolver, build_plugin_dirs
    from core.registry import set_path_resolver

    path_resolver = PluginPathResolver(build_plugin_dirs(BASE_DIR))
    set_path_resolver(path_resolver)

    # Initialize models registry (must be before plugin loading so runners can use it)
    from core.models_registry import ModelsRegistry
    from core.registry import set_models_registry

    models_registry = ModelsRegistry()
    set_models_registry(models_registry)

    # Load plugins (channels are excluded - AgentManager creates channel instances)
    plugin_manager = PluginManager(
        path_resolver=path_resolver,
        config_path=BASE_DIR / "config" / "plugins.json",  # migration only
    )
    await plugin_manager.load_all(exclude_types=["channel"])

    # Register plugin_manager in global registry for access from admin panel etc.
    from core.registry import set_agent_manager, set_plugin_manager

    set_plugin_manager(plugin_manager)

    if not plugin_manager.runners:
        logger.warning(
            "No runner plugin enabled. "
            "Enable at least one runner (e.g. claude) via the admin UI plugin manager. "
            "Agents will not respond to messages until a runner is configured."
        )
    else:
        logger.info(f"Runners loaded: {list(plugin_manager.runners.keys())}")

    # Log MCP servers count (config is generated on-demand, never written to disk)
    mcp_servers = plugin_manager.get_all_mcp_server_names()
    logger.info(f"MCP providers ready with {len(mcp_servers)} servers (in-memory only)")

    stop_event = asyncio.Event()

    # Load agents from database
    from core.agent_manager import AgentManager

    agent_manager = AgentManager(
        plugin_manager=plugin_manager,
    )
    set_agent_manager(agent_manager)

    await agent_manager.load_all()
    agents_list = agent_manager.list_agents()

    if not agents_list:
        logger.warning(
            "No agents configured. "
            "Use the Admin UI at %s to create one. "
            "Messaging features are disabled until at least one agent exists.",
            os.environ.get("GRIDBEAR_BASE_URL", "http://localhost:8088"),
        )

    logger.info(
        f"Loaded {len(agents_list)} agent(s): {[a['name'] for a in agents_list]}"
    )

    # Set message handlers for all agent channels
    def create_agent_message_handler(pm, agent_context):
        """Create a message handler with agent context."""
        processor = AgentAwareMessageProcessor(pm, agent_context)
        return processor.process_message

    agent_manager.set_message_handlers(create_agent_message_handler)

    # Provision MCP gateway tokens for all agents
    gateway_url = os.environ.get("MCP_GATEWAY_URL", "http://gridbear-ui:8080")
    try:
        from core.mcp_token_manager import MCPTokenManager, set_mcp_token_manager
        from core.oauth2.models import OAuth2Database

        oauth2_db = OAuth2Database()
        token_manager = MCPTokenManager(oauth2_db, gateway_url)
        token_manager.provision_agent_tokens(list(agent_manager._agents.values()))
        set_mcp_token_manager(token_manager)
        logger.info("MCP gateway tokens provisioned for all agents")
    except Exception as e:
        logger.error(f"Failed to provision MCP gateway tokens: {e}")
        logger.warning("Agents will run without MCP gateway access")

    # Fire lifecycle hooks — let plugins self-wire after agents are loaded
    await plugin_manager.hooks.execute(HookName.ON_STARTUP, {})

    # Initialize MCP Gateway on core container
    mcp_gateway_enabled = os.getenv("MCP_GATEWAY_ENABLED", "true").lower() != "false"
    mcp_client_manager = None
    if mcp_gateway_enabled:
        try:
            from core.mcp_gateway import server as mcp_server
            from core.mcp_gateway.client_manager import MCPClientManager
            from core.mcp_gateway.tool_providers import discover_local_tool_providers

            mcp_client_manager = MCPClientManager()
            mcp_server.set_client_manager(mcp_client_manager)
            await mcp_client_manager.start()

            discover_local_tool_providers(mcp_server)

            from core.mcp_gateway.async_tasks import AsyncTaskManager

            max_concurrent = int(os.getenv("ASYNC_TASKS_MAX_PER_AGENT", "5"))
            task_mgr = AsyncTaskManager(
                notify_callback=mcp_server._send_task_notification,
                max_concurrent_per_agent=max_concurrent,
            )
            mcp_server.set_task_manager(task_mgr)
            await task_mgr.start()

            asyncio.create_task(mcp_client_manager.warm_up())
            logger.info("MCP Gateway initialized on core container")
        except Exception as e:
            logger.warning(
                "MCP Gateway init failed (bot continues without tools): %s", e
            )
            mcp_client_manager = None

    def signal_handler():
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    cleanup = asyncio.create_task(cleanup_task(plugin_manager, stop_event))
    plugin_reload = asyncio.create_task(plugin_reload_task(plugin_manager, stop_event))
    agent_reload = asyncio.create_task(
        agent_reload_task(agent_manager, plugin_manager, stop_event)
    )
    restart_monitor = asyncio.create_task(restart_task(stop_event))

    # Start internal API server (used by admin WebChat)
    api_task = None
    api_server = None
    try:
        import uvicorn

        from core.internal_api.server import create_app as create_internal_app

        internal_app = create_internal_app(
            plugin_manager=plugin_manager,
            mount_mcp=(mcp_client_manager is not None),
            mount_rest_api=True,
        )
        internal_app.state.bot_start_time = time.time()
        api_config = uvicorn.Config(
            internal_app, host="0.0.0.0", port=8000, log_level="warning"
        )
        api_server = uvicorn.Server(api_config)
        api_task = asyncio.create_task(api_server.serve())
        logger.info("Internal API started on port 8000")

        # Start UI app on port 8080 (unified container — single process)
        ui_port = int(os.getenv("UI_PORT", "8080"))
        if os.getenv("UI_ENABLED", "true").lower() != "false":
            try:
                from ui.app import app as ui_app

                ui_config = uvicorn.Config(
                    ui_app, host="0.0.0.0", port=ui_port, log_level="warning"
                )
                ui_server = uvicorn.Server(ui_config)
                asyncio.create_task(ui_server.serve())
                logger.info("Admin UI started on port %d", ui_port)
            except Exception as e:
                logger.warning("Admin UI failed to start: %s", e)
    except Exception as e:
        logger.error(f"Failed to start internal API: {e}")

    try:
        await agent_manager.start_all()
        logger.info("All agents started. Press Ctrl+C to stop.")
        await stop_event.wait()
    finally:
        logger.info("Stopping...")
        if api_server:
            api_server.should_exit = True
        if api_task:
            api_task.cancel()
        cleanup.cancel()
        plugin_reload.cancel()
        agent_reload.cancel()
        restart_monitor.cancel()
        await agent_manager.stop_all()
        await plugin_manager.shutdown_all()

        # Shutdown MCP Gateway (if running on core)
        if mcp_client_manager:
            try:
                from core.mcp_gateway.server import get_task_manager

                task_mgr = get_task_manager()
                if task_mgr:
                    await task_mgr.shutdown()
                await mcp_client_manager.shutdown()
            except Exception as e:
                logger.warning("MCP Gateway shutdown error: %s", e)

        # Shutdown PostgreSQL pool
        if db:
            await db.shutdown()

        # Check if this was a restart request
        restart_file = BASE_DIR / "data" / "restart_requested.json"
        if restart_file.exists():
            try:
                import json

                with open(restart_file) as f:
                    req = json.load(f)
                if req.get("status") == "restarting":
                    # Clear the file and exit with code 1 to trigger Docker restart
                    restart_file.unlink()
                    logger.info("Shutdown complete - restarting via Docker")
                    sys.exit(1)
            except Exception:
                pass

        logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())

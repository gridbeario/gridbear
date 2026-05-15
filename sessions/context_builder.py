from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


@dataclass
class UserInfo:
    user_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    platform: str

    @property
    def display_name(self) -> str:
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        if self.first_name:
            return self.first_name
        if self.username:
            return self.username
        return str(self.user_id)


SYSTEM_CONTEXT_BASE = """[Identity]
You are a virtual assistant powered by GridBear. You are friendly, helpful, and concise.

[Formatting]
- You are responding on a chat platform (Telegram/Discord)
- For tables, ALWAYS use monospace code blocks (triple backticks)
- Do NOT use markdown tables with | and --- as they don't render properly

[Sending Files/Images]
You can send files to the user using the `send_file_to_chat` MCP tool.
After taking a screenshot or generating a file, call the tool with:
- file_path: the absolute path returned by the MCP tool (e.g. /app/data/playwright/page-xxx.png)
- chat_id: the user's chat ID (numeric for telegram/discord, username for webchat)
- platform: "telegram", "discord", or "webchat"
- caption: optional text to accompany the file

Example flow:
1. Take a screenshot with playwright → get file path
2. Call send_file_to_chat(file_path=..., chat_id=..., platform=...)
3. The user receives the file in their chat

[Google Sheets / Docs Access]
You have two ways to access Google documents:
- gws-* tools (e.g. gws-davide_corio_dubhe_it): use the USER's Google account (OAuth). Use these when the user asks you to access THEIR documents.
- google-sheets tools (e.g. google-sheets__sheets_read): use YOUR service account. Use these when accessing documents shared with your SA, or when you need to comment/edit as yourself.

When the user shares a Google document link with you and asks you to access it with your account or service account, ALWAYS use the google-sheets__ prefixed tools, NOT the gws-* tools."""


def _build_email_context() -> str:
    """Build dynamic email context from config."""
    try:
        from ui.config_manager import ConfigManager

        config = ConfigManager()
        settings = config.get_bot_email_settings()

        sender_name = settings.get("sender_name", "GridBear")
        sender_alias = settings.get("sender_alias", "noreply@example.com")
        signature = settings.get("signature", "")

        # Get bot's email account
        bot_identity = config.get_bot_identity()
        gmail_accounts = (
            config.get_user_gmail_accounts(bot_identity) if bot_identity else []
        )
        bot_email = gmail_accounts[0] if gmail_accounts else "noreply@example.com"
        server_name = f"gmail-{bot_email}"

        email_ctx = f"""[Your Email Account]
You have your own email account: {bot_email} with alias {sender_alias}.
When sending emails on your own behalf (not on behalf of the user), ALWAYS use:
- Server: {server_name}
- from_alias: {sender_alias}
- from_name: {sender_name}

Example: mcp__{server_name}__send_email with from_alias="{sender_alias}" and from_name="{sender_name}"

This applies to:
- Responding to support requests received at your address
- Sending notifications or updates as the assistant
- Any email where YOU are the sender, not the user

Only use the user's email account when they explicitly ask you to send an email as them."""

        if signature:
            email_ctx += f"""

[Email Signature]
When sending emails as the assistant, ALWAYS append this signature at the end:

{signature}"""

        return email_ctx
    except Exception:
        return ""


DEFAULT_TIMEZONE = "Europe/Rome"
DEFAULT_LOCALE = "en"

LOCALE_NAMES = {
    "en": "English",
    "it": "Italian",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
}


class ContextBuilder:
    def __init__(self, memory_service: Any = None, sessions_service: Any = None):
        self._parts: list[str] = []
        self._user_info: UserInfo | None = None
        self._gmail_accounts: list[str] = []
        self._shared_accounts: dict[
            str, dict[str, list[str]]
        ] = {}  # plugin -> {uid -> [accounts]}
        self._allowed_mcp_servers: list[str] | None = None  # None = all, [] = none
        self._timezone: str = DEFAULT_TIMEZONE
        self._locale: str = DEFAULT_LOCALE
        self._voice_response: bool = False
        self._memories: list[dict] = []
        self._chat_history: list[dict] = []  # Recent chat history
        self._cold_start: bool = False  # Session was reset
        self._user_message: str = ""  # For memory search
        self._memory_service = memory_service
        self._sessions_service = sessions_service
        self._plugin_context: str = ""  # Context injected by plugins
        # Multi-agent mode: agent identity
        self._agent_name: str | None = None
        self._agent_display_name: str | None = None
        self._agent_system_prompt: str | None = None
        self._agent_email_settings: dict | None = None
        self._tool_loading: str = "full"
        self._conversation_context: str | None = None
        self._channel_metadata: dict | None = None
        self._conversation_documents: list[dict] | None = None

    def set_channel_metadata(self, metadata: dict | None) -> "ContextBuilder":
        """Set channel/conversation metadata for message source tracking."""
        self._channel_metadata = metadata if metadata else None
        return self

    def set_conversation_context(self, context: str | None) -> "ContextBuilder":
        """Set per-conversation working context."""
        self._conversation_context = context if context else None
        return self

    def set_conversation_documents(self, docs: list[dict] | None) -> "ContextBuilder":
        """Set per-conversation documents for RAG injection."""
        self._conversation_documents = docs if docs else None
        return self

    def add_user_info(self, user_info: UserInfo) -> "ContextBuilder":
        """Add user information to context."""
        self._user_info = user_info
        return self

    def add_gmail_account(self, account_name: str) -> "ContextBuilder":
        """Add user's Gmail account name for MCP tool selection."""
        if account_name and account_name not in self._gmail_accounts:
            self._gmail_accounts.append(account_name)
        return self

    def add_gmail_accounts(self, accounts: list[str]) -> "ContextBuilder":
        """Add multiple Gmail accounts for MCP tool selection."""
        for account in accounts:
            self.add_gmail_account(account)
        return self

    def set_allowed_mcp_servers(self, servers: list[str] | None) -> "ContextBuilder":
        """Set allowed MCP servers for this user. None = all allowed, [] = none allowed."""
        self._allowed_mcp_servers = servers
        return self

    def set_timezone(self, timezone: str) -> "ContextBuilder":
        """Set user's timezone."""
        self._timezone = timezone
        return self

    def set_locale(self, locale: str) -> "ContextBuilder":
        """Set user's preferred language for responses."""
        self._locale = locale.lower() if locale else DEFAULT_LOCALE
        return self

    def set_voice_response(self, enabled: bool = True) -> "ContextBuilder":
        """Set whether response will be converted to voice (TTS).

        When enabled, Claude will avoid emojis and special characters.
        """
        self._voice_response = enabled
        return self

    def set_tool_loading(self, mode: str) -> "ContextBuilder":
        """Set tool loading mode (full or search)."""
        self._tool_loading = mode or "full"
        return self

    def set_plugin_context(self, context: str) -> "ContextBuilder":
        """Set additional context from plugins.

        This allows plugins to inject their own instructions into the system prompt.
        """
        self._plugin_context = context
        return self

    def set_agent_identity(
        self,
        name: str,
        display_name: str,
        system_prompt: str | None = None,
    ) -> "ContextBuilder":
        """Set agent identity for multi-agent mode.

        When set, the agent's system prompt replaces the default SYSTEM_CONTEXT_BASE
        identity section.

        Args:
            name: Agent internal name (e.g., "myagent")
            display_name: Agent display name (e.g., "My Agent")
            system_prompt: Custom system prompt/personality. If None, uses default.
        """
        self._agent_name = name
        self._agent_display_name = display_name
        self._agent_system_prompt = system_prompt
        return self

    def set_agent_email_settings(self, settings: dict | None) -> "ContextBuilder":
        """Set agent-specific email settings.

        When set, these override the global email context.

        Args:
            settings: Email settings dict from agent config with keys:
                - account: Agent's email address
                - sender_name: Display name for sent emails
                - signature: Email signature text
        """
        self._agent_email_settings = settings
        return self

    def set_shared_accounts(
        self, accounts: dict[str, dict[str, list[str]]]
    ) -> "ContextBuilder":
        """Set shared MCP accounts from memory group members.

        Args:
            accounts: Dict of {plugin_name: {unified_id: [account_ids]}}
        """
        self._shared_accounts = accounts
        return self

    def add_text(self, text: str) -> "ContextBuilder":
        """Add text to the prompt."""
        if text:
            self._parts.append(text.strip())
            self._user_message = text.strip()  # Store for memory search
        return self

    async def include_memories(self, limit: int = 5) -> "ContextBuilder":
        """Fetch and include relevant memories for the user.

        Should be called after add_user_info and add_text.
        """
        if not self._user_info or not self._user_message:
            return self

        if not self._memory_service or not self._memory_service.enabled:
            return self

        # Use async get_relevant if available, otherwise run sync in executor
        import asyncio

        loop = asyncio.get_event_loop()
        memories = await loop.run_in_executor(
            None,
            lambda: self._memory_service.get_relevant_memories(
                query=self._user_message,
                platform=self._user_info.platform,
                username=self._user_info.username or str(self._user_info.user_id),
                limit=limit,
            ),
        )
        self._memories = memories
        return self

    async def include_chat_history(
        self,
        limit: int = 15,
        max_chars: int = 4000,
        max_hours: int = 48,
        cold_start: bool = False,
    ) -> "ContextBuilder":
        """Fetch and include recent chat history for context.

        This gives Claude memory of recent conversations with the user.
        Should be called after add_user_info.

        Args:
            limit: Max number of messages to include
            max_chars: Max total characters for history section
            max_hours: Only include messages from last N hours
            cold_start: If True, session was reset — adds context recovery note
        """
        if not self._user_info or not self._sessions_service:
            return self

        self._cold_start = cold_start

        try:
            from datetime import datetime, timedelta

            history = await self._sessions_service.get_recent_chat_history(
                user_id=self._user_info.user_id,
                platform=self._user_info.platform,
                limit=limit * 2,  # Fetch more, then filter
            )

            # Filter by time
            cutoff = datetime.utcnow() - timedelta(hours=max_hours)
            filtered = []
            for msg in history:
                created_at = msg.get("created_at", "")
                if created_at:
                    try:
                        msg_time = datetime.fromisoformat(
                            created_at.replace("Z", "+00:00")
                        )
                        if msg_time.replace(tzinfo=None) < cutoff:
                            continue
                    except (ValueError, TypeError):
                        pass
                filtered.append(msg)

            # Reverse for chronological order and limit
            filtered = list(reversed(filtered[:limit]))

            # Truncate to max_chars
            total_chars = 0
            self._chat_history = []
            for msg in filtered:
                content = msg.get("content", "")
                msg_len = len(content) + 50  # Account for metadata
                if total_chars + msg_len > max_chars:
                    break
                self._chat_history.append(msg)
                total_chars += msg_len

        except Exception:
            pass

        return self

    async def include_chat_history_if_relevant(
        self, user_message: str, limit: int = 10, max_chars: int = 3000
    ) -> "ContextBuilder":
        """RAG: Include chat history only if user asks about past conversations.

        Detects keywords indicating the user wants to recall past discussions,
        then searches and injects relevant messages.

        Args:
            user_message: The user's current message
            limit: Max messages to inject
            max_chars: Max total characters
        """
        if not self._user_info or not self._sessions_service:
            return self

        # Keywords that indicate user wants to recall past conversations
        recall_keywords = [
            # Italian
            "ricordi",
            "ricorda",
            "abbiamo parlato",
            "avevamo discusso",
            "ti avevo detto",
            "mi avevi detto",
            "vecchie conversazioni",
            "conversazione precedente",
            "l'altra volta",
            "tempo fa",
            "ieri",
            "giorni fa",
            "settimana scorsa",
            "mese scorso",
            # English
            "remember",
            "we talked",
            "we discussed",
            "i told you",
            "you told me",
            "old conversations",
            "previous conversation",
            "last time",
            "the other day",
            "days ago",
            "last week",
        ]

        message_lower = user_message.lower()
        is_recall_request = any(kw in message_lower for kw in recall_keywords)

        if not is_recall_request:
            return self

        try:
            # Extract potential search terms from the message
            # Remove common recall phrases to get the actual topic
            search_query = message_lower
            for phrase in [
                "ricordi quando",
                "ricordi che",
                "ti ricordi",
                "abbiamo parlato di",
                "avevamo discusso di",
                "remember when",
                "we talked about",
            ]:
                search_query = search_query.replace(phrase, "")
            search_query = search_query.strip()

            # If we have a meaningful query, search
            if len(search_query) > 3:
                results = await self._sessions_service.search_chat_history(
                    user_id=self._user_info.user_id,
                    platform=self._user_info.platform,
                    query=search_query,
                    limit=limit,
                )

                if results:
                    # Convert to chat_history format and respect max_chars
                    total_chars = 0
                    for r in results:
                        content = r.get("content", "")
                        if total_chars + len(content) > max_chars:
                            break
                        self._chat_history.append(r)
                        total_chars += len(content)

        except Exception:
            pass

        return self

    def add_attachment_reference(self, path: Path, filename: str) -> "ContextBuilder":
        """Add reference to downloaded attachment.

        Provides appropriate instructions based on file type.
        """
        # Ensure absolute path
        abs_path = path.resolve() if not path.is_absolute() else path

        # Detect file type for appropriate instructions
        suffix = path.suffix.lower()
        audio_formats = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".webm", ".mp4"}
        image_formats = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

        if suffix in audio_formats:
            instruction = (
                f"This is an audio file. Use the transcription__transcribe tool "
                f'with file_path="{abs_path}" to transcribe it.'
            )
        elif suffix in image_formats:
            instruction = "This is an image file. You can view it using the Read tool."
        else:
            instruction = "Use the Read tool to read this file."

        self._parts.append(
            f"\n[Attachment: {filename}]\nPath: {abs_path}\n{instruction}"
        )
        return self

    def add_system_context(self, context: str) -> "ContextBuilder":
        """Add system context at the beginning."""
        self._parts.insert(0, f"[Context: {context}]")
        return self

    def _build_identity_context(self) -> str:
        """Build the identity section of the system prompt.

        Returns agent-specific identity if set, otherwise default GridBear identity.
        """
        if self._agent_system_prompt:
            # Multi-agent mode: use agent's custom system prompt
            return self._agent_system_prompt

        # Default identity (backward compatible)
        return SYSTEM_CONTEXT_BASE

    def _build_agent_email_context(self) -> str:
        """Build email context from agent-specific settings."""
        if not self._agent_email_settings:
            return ""

        account = self._agent_email_settings.get("account", "")
        if not account:
            return ""

        sender_name = self._agent_email_settings.get(
            "sender_name", self._agent_display_name or "Assistant"
        )
        sender_alias = self._agent_email_settings.get("from_alias", "")
        signature = self._agent_email_settings.get("signature", "")

        # Derive MCP server name from account
        server_name = f"gmail-{account}"

        email_ctx = f"""[Your Email Account]
You have your own email account: {account}.
When sending emails on your own behalf (not on behalf of the user), ALWAYS use:
- Server: {server_name}
- from_name: {sender_name}"""

        if sender_alias:
            email_ctx += f"""
- from_alias: {sender_alias}

Example: mcp__{server_name}__send_email with from_alias="{sender_alias}" and from_name="{sender_name}" """
        else:
            email_ctx += f"""

Example: mcp__{server_name}__send_email with from_name="{sender_name}" """

        email_ctx += f"""

ALWAYS prefer sending from YOUR account ({account}) by default.
This applies to ALL emails unless the user EXPLICITLY asks you to send from their account.
Including:
- Responding to support requests
- Sending notifications or updates
- Forwarding information
- Any email the user asks you to send (use your account, not theirs)

Only use the user's personal email account when they EXPLICITLY say "send from my account" or "send as me"."""

        if signature:
            email_ctx += f"""

[Email Signature]
When sending emails as {self._agent_display_name or "yourself"}, ALWAYS append this signature at the end:

{signature}"""

        return email_ctx

    def build(self) -> str:
        """Build final prompt string."""
        # Start with identity (agent-specific or default)
        identity = self._build_identity_context()
        parts = [identity]

        # Add email context: prefer agent-specific, fall back to global
        if self._agent_email_settings and self._agent_email_settings.get("account"):
            email_ctx = self._build_agent_email_context()
        else:
            email_ctx = _build_email_context()
        if email_ctx:
            parts.append(email_ctx)

        # Add plugin-injected context
        if self._plugin_context:
            parts.append(self._plugin_context)

        if self._user_info:
            # For webchat, use username as chat_id (no numeric ID)
            chat_id = (
                self._user_info.username or str(self._user_info.user_id)
                if self._user_info.platform == "webchat"
                else str(self._user_info.user_id)
            )
            user_ctx = (
                f"[User: {self._user_info.display_name} | "
                f"chat_id: {chat_id} | "
                f"Username: @{self._user_info.username or 'N/A'} | "
                f"Platform: {self._user_info.platform}]"
            )
            parts.append(user_ctx)

        # Add channel/conversation metadata
        if self._channel_metadata:
            channel = self._channel_metadata.get("channel", "")
            source_parts = [f"channel: {channel}"]
            if self._channel_metadata.get("conversation_id"):
                source_parts.append(
                    f"conversation_id: {self._channel_metadata['conversation_id']}"
                )
            if self._channel_metadata.get("chat_type"):
                source_parts.append(f"chat_type: {self._channel_metadata['chat_type']}")
            if self._channel_metadata.get("participants"):
                names = ", ".join(
                    f"{p['name']} (@{p['uid']})"
                    for p in self._channel_metadata["participants"]
                )
                source_parts.append(f"participants: {names}")
                agent_handle = self._agent_name or "yourself"
                source_parts.append(
                    "This is a shared conversation. When replying, "
                    "always start with @username of the person you are "
                    "responding to (e.g. @dcorio). "
                    f"You only respond when mentioned with @{agent_handle}."
                )
            parts.append("[Message Source]\n" + "\n".join(source_parts))

        # Language instruction
        lang_name = LOCALE_NAMES.get(self._locale, self._locale.upper())
        parts.append(
            f"[Language: ALWAYS respond in {lang_name}. This is the user's preferred language.]"
        )

        # Voice response instruction
        if self._voice_response:
            parts.append(
                "[Voice Response Mode: Your response will be converted to speech (TTS). "
                "Do NOT use emojis, special characters, or formatting that doesn't translate well to audio. "
                "Keep responses conversational and natural for spoken language.]"
            )

        # Add current time in user's timezone
        try:
            tz = ZoneInfo(self._timezone)
            now = datetime.now(tz)
            time_ctx = f"[Current date/time: {now.strftime('%A %d %B %Y, %H:%M')} ({self._timezone})]"
            parts.append(time_ctx)
        except Exception:
            pass

        # Built-in tools info
        parts.append(
            "[Built-in Tools: You have access to WebSearch and WebFetch for web searches. "
            "Use WebSearch when the user asks for current news, information, or anything requiring internet search.]"
        )

        # MCP permissions
        if self._allowed_mcp_servers is not None:
            if len(self._allowed_mcp_servers) == 0:
                parts.append(
                    "[MCP Permissions: This user has NO access to external MCP tools. "
                    "DO NOT use Odoo, Gmail or any other MCP server tools. "
                    "If the user asks for operations requiring these tools, "
                    "respond that they don't have the necessary permissions. "
                    "Built-in tools (chat_history, send_file_to_chat, ask_agent, gridbear_help) "
                    "are still available.]"
                )
            else:
                servers_str = ", ".join([f"'{s}'" for s in self._allowed_mcp_servers])
                parts.append(
                    f"[MCP Permissions: For external services, this user can use: {servers_str}. "
                    f"DO NOT use MCP tools from other servers not listed. "
                    f"Built-in tools (chat_history, send_file_to_chat, ask_agent, gridbear_help) "
                    f"are always available regardless of permissions.]"
                )

        # Tool discovery mode instructions
        if self._tool_loading == "search":
            parts.append(
                "[Tool Discovery Mode: External MCP tools (Odoo, Gmail, HomeAssistant, etc.) "
                "are NOT loaded directly. To use any external service:\n"
                "1. Call `search_tools` with keywords describing what you need "
                "(e.g. 'odoo invoice', 'gmail send', 'calendar events')\n"
                "2. Review the results to find the right tool\n"
                "3. Call `execute_discovered_tool` with the exact tool name and arguments\n"
                "IMPORTANT: You MUST search for tools before attempting to use external services. "
                "Do NOT assume tools are unavailable — always search first.]"
            )

        # Shared MCP accounts (for sharing across memory groups)
        for plugin_name, accounts in self._shared_accounts.items():
            account_lines = []
            current_username = (
                self._user_info.username.lower()
                if self._user_info and self._user_info.username
                else ""
            )
            for member_id, account_ids in accounts.items():
                display_name = member_id
                if ":" in member_id:
                    display_name = member_id.split(":", 1)[1]
                is_current = display_name == current_username
                owner_mark = " (you)" if is_current else ""
                for account_id in account_ids:
                    account_lines.append(f"- {display_name}{owner_mark}: {account_id}")

            if account_lines:
                lines_text = "\n".join(account_lines)
                parts.append(
                    f"[Available shared {plugin_name} accounts:]\n{lines_text}\n"
                    f"To use these, look for tools from the '{plugin_name}-<account>' MCP server."
                )

        # Include relevant memories (separated by type)
        if self._memories:
            doc_lines = []  # From group documents/knowledge base
            personal_lines = []  # Personal user memories
            for mem in self._memories:
                content = mem.get("content", "")
                user_id = mem.get("metadata", {}).get("user_id", "")
                if content:
                    if user_id.startswith("__group__:"):
                        doc_lines.append(f"- {content}")
                    else:
                        personal_lines.append(f"- {content}")

            if doc_lines:
                docs_text = "\n".join(doc_lines)
                parts.append(
                    f"[Knowledge Base - Relevant documents and information:]\n{docs_text}"
                )
            if personal_lines:
                personal_text = "\n".join(personal_lines)
                parts.append(
                    f"[Personal Memory - Information about this user:]\n{personal_text}"
                )

        # Include per-conversation working context
        if self._conversation_context:
            parts.append(
                f"[Conversation Context — IMPORTANT]\n"
                f"This conversation is dedicated to the following scope. "
                f"All user messages in this conversation relate to this context "
                f"unless they explicitly say otherwise. "
                f"If a message seems unrelated to this context, ask the user "
                f"if they are in the right conversation before proceeding.\n\n"
                f"{self._conversation_context}"
            )
        elif self._channel_metadata and self._channel_metadata.get(
            "conversation_title"
        ):
            title = self._channel_metadata["conversation_title"]
            parts.append(
                f"[Conversation Topic]\n"
                f'This conversation is titled "{title}". '
                f"User messages likely relate to this topic. "
                f"If a message seems unrelated, ask the user "
                f"if they are in the right conversation."
            )

        # Include conversation documents (metadata only — content via tool)
        if self._conversation_documents:
            doc_lines = []
            for doc in self._conversation_documents:
                fname = doc.get("original_filename", "document")
                text = doc.get("content_text", "")
                size_kb = len(text) / 1024 if text else 0
                status = f"{size_kb:.0f}KB" if text else "no text"
                doc_lines.append(f"- {fname} ({status})")
            conv_id = ""
            if self._channel_metadata:
                conv_id = self._channel_metadata.get("conversation_id", "")
            parts.append(
                "[Conversation Documents]\n"
                "The user has uploaded documents to this conversation. "
                "To read their content, use the conversation_docs__read tool "
                f'with conversation_id="{conv_id}" and the filename.\n\n'
                + "\n".join(doc_lines)
            )

        # Include recent chat history for conversational context
        if self._chat_history:
            # Deduplicate by (role, content) — both include_chat_history and
            # include_chat_history_if_relevant may add overlapping messages
            seen = set()
            deduped = []
            for msg in self._chat_history:
                key = (msg.get("role", ""), msg.get("content", "")[:200])
                if key not in seen:
                    seen.add(key)
                    deduped.append(msg)

            history_lines = []
            # Use agent display name if set, otherwise "Assistant"
            assistant_name = self._agent_display_name or "Assistant"
            for msg in deduped:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                date = msg.get("created_at", "")[:16] if msg.get("created_at") else ""
                # Truncate very long messages
                if len(content) > 500:
                    content = content[:500] + "..."
                role_label = "User" if role == "user" else assistant_name
                history_lines.append(f"[{date}] {role_label}: {content}")

            if history_lines:
                history_text = "\n".join(history_lines)
                parts.append(
                    "[IMPORTANT: The conversation history below shows your recent "
                    "messages with this user. You MUST continue naturally from where "
                    "you left off. Do NOT greet the user as if meeting them for the "
                    "first time. If the user's message seems to continue a previous "
                    "topic, respond in context.]\n\n"
                    f"[Recent Conversation History:]\n{history_text}"
                )

        parts.extend(self._parts)
        return "\n\n".join(parts)


def build_inter_agent_context(agents_info: list[dict], current_agent: str) -> str:
    """Build context string with information about other available agents.

    Args:
        agents_info: List of dicts with {id, name, description}
        current_agent: ID of the current agent (to exclude from list)

    Returns:
        Context string to inject into system prompt
    """
    other_agents = [a for a in agents_info if a["id"] != current_agent]

    if not other_agents:
        return ""

    lines = [
        "[Inter-Agent Communication]",
        "You can communicate with other agents using the `ask_agent` tool.",
        "",
        "Available agents:",
    ]
    for agent in other_agents:
        lines.append(
            f"- {agent['id']}: {agent.get('description', agent.get('name', ''))}"
        )

    lines.extend(
        [
            "",
            "Rules:",
            "- Use the ask_agent tool to send a message and wait for the response",
            "- You cannot send messages to yourself",
            "- Be concise in your messages to other agents",
        ]
    )

    return "\n".join(lines)

"""Session Manager for Kimi multi-turn conversations.

Thin wrapper around the shared BaseSessionManager. Messages are stored in
OpenAI format: {"role": "user|assistant", "content": "..."}.

This is the API backend's conversation store — the only backend this plugin
ships in the current iteration. (A CLI-driven backend, if one existed, would
carry continuity differently: via a resume flag, with the transcript kept by
the CLI rather than here.)
"""

from core.runners.session_manager import BaseSessionManager
from core.runners.session_manager import RunnerSession as KimiSession  # noqa: F401


class SessionManager(BaseSessionManager):
    """Kimi-specific session manager."""

    _log_prefix = "Kimi"

"""Startup hook: launch the artifacts cleanup worker.

The cleanup loop runs on the UI container only to avoid a double-sweep
when both bot and UI processes load the plugin. ``GRIDBEAR_ROLE=bot``
disables the worker; any other value (``ui``, unset for single-process
dev) enables it.
"""

from __future__ import annotations

import asyncio
import logging
import os

from core.hooks import hook

_logger = logging.getLogger(__name__)

_BOT_ROLE = "bot"


@hook("on_startup", priority=50)
async def start_cleanup_worker(data, **_kwargs):
    """Launch the cleanup loop as a background task (non-bot roles only)."""
    role = os.environ.get("GRIDBEAR_ROLE")
    if role == _BOT_ROLE:
        _logger.debug(
            "Artifacts cleanup worker not started on bot container (role=%s)", role
        )
        return data

    from plugins.artifacts.cleanup import cleanup_loop

    _logger.info("Starting artifacts cleanup worker (role=%s)", role or "unset")
    asyncio.create_task(cleanup_loop())
    return data

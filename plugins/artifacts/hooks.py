"""Startup hook: launch the artifacts cleanup worker.

Runs whenever `HookName.ON_STARTUP` is emitted, which in GridBear happens
on the bot entry-point (main.py) and in single-process deployments. The UI
container uses FastAPI's own `on_event("startup")` and does not emit
GridBear's ON_STARTUP, so in split-container deployments cleanup runs on
the bot side only — never double-executes.
"""

from __future__ import annotations

import asyncio
import logging

from core.hooks import hook

_logger = logging.getLogger(__name__)


@hook("on_startup", priority=50)
async def start_cleanup_worker(*_args, **_kwargs) -> None:
    from plugins.artifacts.cleanup import cleanup_loop

    _logger.info("Starting artifacts cleanup worker")
    asyncio.create_task(cleanup_loop())

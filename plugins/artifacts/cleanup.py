"""Background worker: prune expired non-pinned artifacts."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from plugins.artifacts import storage
from plugins.artifacts.models import Artifact

_logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_HOURS = 6


def _load_interval_hours() -> int:
    try:
        from core.plugin_registry.models import PluginRegistryEntry

        entry = PluginRegistryEntry.get_sync(name="artifacts")
        cfg = (entry or {}).get("config") or {}
        return int(cfg.get("cleanup_interval_hours", _DEFAULT_INTERVAL_HOURS))
    except Exception as err:
        _logger.debug("Failed to load cleanup_interval_hours from DB: %s", err)
        return _DEFAULT_INTERVAL_HOURS


async def run_cleanup_once() -> int:
    """Execute one cleanup sweep. Returns the number of artifacts removed."""
    now = datetime.now(UTC)
    to_delete = await Artifact.search(
        [("expires_at", "<", now), ("pinned", "=", False)]
    )
    count = 0
    for row in to_delete:
        uid = row["id"]
        try:
            storage.delete_artifact(uid)
            await Artifact.delete(uid)
            count += 1
            _logger.info("Cleaned up expired artifact %s", uid)
        except Exception as err:
            _logger.exception("Failed to clean up artifact %s: %s", uid, err)
    return count


async def cleanup_loop() -> None:
    """Long-running task invoking run_cleanup_once at a fixed interval."""
    interval = _load_interval_hours() * 3600
    try:
        await run_cleanup_once()
    except Exception as err:
        _logger.exception("Initial cleanup failed: %s", err)
    while True:
        await asyncio.sleep(interval)
        try:
            await run_cleanup_once()
        except Exception as err:
            _logger.exception("Cleanup iteration failed: %s", err)

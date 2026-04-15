"""WhatsApp legacy database layer (DEPRECATED).

This module is superseded by plugins/whatsapp_evolution/models.py (ORM models).
Kept only for legacy migration code that runs on existing installs.
The WhatsAppDB class is no longer imported by any consumer.
"""

import logging

from core.registry import get_database

logger = logging.getLogger(__name__)

PG_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS whatsapp;

CREATE TABLE whatsapp.user_instances (
    id SERIAL PRIMARY KEY,
    unified_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    instance_name TEXT NOT NULL UNIQUE,
    silent_reject BOOLEAN DEFAULT FALSE,
    reject_message TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(unified_id, agent_name)
);

CREATE TABLE whatsapp.authorized_numbers (
    id SERIAL PRIMARY KEY,
    instance_id INTEGER REFERENCES whatsapp.user_instances(id) ON DELETE CASCADE,
    phone TEXT NOT NULL,
    label TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(instance_id, phone)
);
"""

MIGRATION_NAME = "010_whatsapp_multitenant"
MIGRATION_011 = "011_whatsapp_reject_settings"
MIGRATION_012 = "012_whatsapp_wake_words"

_migration_done = False


def _run_migration() -> None:
    """Run PostgreSQL migrations if not already applied."""
    global _migration_done
    if _migration_done:
        return
    db = get_database()
    if not db:
        raise RuntimeError("Database not initialized")
    with db.acquire_sync() as conn:
        # Migration 010: base schema
        row = conn.execute(
            "SELECT 1 FROM public._migrations WHERE name = %s",
            (MIGRATION_NAME,),
        ).fetchone()
        if not row:
            conn.execute(PG_SCHEMA)
            conn.execute(
                "INSERT INTO public._migrations (name) VALUES (%s)",
                (MIGRATION_NAME,),
            )
            conn.commit()
            logger.info("Applied migration: %s", MIGRATION_NAME)

        # Migration 011: silent_reject + reject_message columns
        row = conn.execute(
            "SELECT 1 FROM public._migrations WHERE name = %s",
            (MIGRATION_011,),
        ).fetchone()
        if not row:
            conn.execute(
                "ALTER TABLE whatsapp.user_instances "
                "ADD COLUMN IF NOT EXISTS silent_reject BOOLEAN DEFAULT FALSE"
            )
            conn.execute(
                "ALTER TABLE whatsapp.user_instances "
                "ADD COLUMN IF NOT EXISTS reject_message TEXT DEFAULT ''"
            )
            conn.execute(
                "INSERT INTO public._migrations (name) VALUES (%s)",
                (MIGRATION_011,),
            )
            conn.commit()
            logger.info("Applied migration: %s", MIGRATION_011)

        # Migration 012: wake_words table
        row = conn.execute(
            "SELECT 1 FROM public._migrations WHERE name = %s",
            (MIGRATION_012,),
        ).fetchone()
        if not row:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS whatsapp.wake_words ("
                "  id SERIAL PRIMARY KEY,"
                "  instance_id INTEGER REFERENCES whatsapp.user_instances(id) ON DELETE CASCADE,"
                "  keyword TEXT NOT NULL,"
                "  response TEXT NOT NULL,"
                "  created_at TIMESTAMPTZ DEFAULT NOW(),"
                "  UNIQUE(instance_id, keyword)"
                ")"
            )
            conn.execute(
                "INSERT INTO public._migrations (name) VALUES (%s)",
                (MIGRATION_012,),
            )
            conn.commit()
            logger.info("Applied migration: %s", MIGRATION_012)

        _migration_done = True


class WhatsAppDB:
    """Async interface for WhatsApp multi-tenant data."""

    def __init__(self):
        self._db = get_database()
        if not self._db:
            raise RuntimeError("Database not initialized")
        _run_migration()

    async def get_user_instance(self, unified_id: str, agent_name: str) -> dict | None:
        """Get a user's instance for a specific agent."""
        async with self._db.acquire() as conn:
            row = await conn.execute(
                "SELECT id, unified_id, agent_name, instance_name, "
                "silent_reject, reject_message, created_at "
                "FROM whatsapp.user_instances "
                "WHERE unified_id = %s AND agent_name = %s",
                (unified_id, agent_name),
            )
            r = await row.fetchone()
            return dict(r) if r else None

    async def get_instance_by_name(self, instance_name: str) -> dict | None:
        """Get instance by its unique name."""
        async with self._db.acquire() as conn:
            row = await conn.execute(
                "SELECT id, unified_id, agent_name, instance_name, "
                "silent_reject, reject_message, created_at "
                "FROM whatsapp.user_instances "
                "WHERE instance_name = %s",
                (instance_name,),
            )
            r = await row.fetchone()
            return dict(r) if r else None

    async def create_user_instance(
        self, unified_id: str, agent_name: str, instance_name: str
    ) -> dict:
        """Create a new user instance. Returns the created record."""
        async with self._db.acquire() as conn:
            row = await conn.execute(
                "INSERT INTO whatsapp.user_instances "
                "(unified_id, agent_name, instance_name) "
                "VALUES (%s, %s, %s) "
                "RETURNING id, unified_id, agent_name, instance_name, created_at",
                (unified_id, agent_name, instance_name),
            )
            r = await row.fetchone()
            await conn.execute("COMMIT")
            return dict(r)

    async def delete_user_instance(self, instance_name: str) -> bool:
        """Delete a user instance (cascades to authorized numbers)."""
        async with self._db.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM whatsapp.user_instances WHERE instance_name = %s",
                (instance_name,),
            )
            await conn.execute("COMMIT")
            return result.rowcount > 0

    async def list_agent_instances(self, agent_name: str) -> list[dict]:
        """List all user instances for a given agent."""
        async with self._db.acquire() as conn:
            rows = await conn.execute(
                "SELECT id, unified_id, agent_name, instance_name, "
                "silent_reject, reject_message, created_at "
                "FROM whatsapp.user_instances "
                "WHERE agent_name = %s "
                "ORDER BY created_at",
                (agent_name,),
            )
            return [dict(r) for r in await rows.fetchall()]

    async def get_authorized_numbers(self, instance_id: int) -> list[dict]:
        """Get all authorized numbers for an instance."""
        async with self._db.acquire() as conn:
            rows = await conn.execute(
                "SELECT id, instance_id, phone, label, created_at "
                "FROM whatsapp.authorized_numbers "
                "WHERE instance_id = %s "
                "ORDER BY created_at",
                (instance_id,),
            )
            return [dict(r) for r in await rows.fetchall()]

    async def add_authorized_number(
        self, instance_id: int, phone: str, label: str = ""
    ) -> dict | None:
        """Add an authorized number. Returns None on duplicate."""
        async with self._db.acquire() as conn:
            try:
                row = await conn.execute(
                    "INSERT INTO whatsapp.authorized_numbers "
                    "(instance_id, phone, label) "
                    "VALUES (%s, %s, %s) "
                    "RETURNING id, instance_id, phone, label, created_at",
                    (instance_id, phone, label),
                )
                r = await row.fetchone()
                await conn.execute("COMMIT")
                return dict(r) if r else None
            except Exception:
                await conn.execute("ROLLBACK")
                return None

    async def remove_authorized_number(self, instance_id: int, phone: str) -> bool:
        """Remove an authorized number."""
        async with self._db.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM whatsapp.authorized_numbers "
                "WHERE instance_id = %s AND phone = %s",
                (instance_id, phone),
            )
            await conn.execute("COMMIT")
            return result.rowcount > 0

    async def update_reject_settings(
        self, instance_name: str, silent_reject: bool, reject_message: str
    ) -> bool:
        """Update reject behavior for unauthorized numbers."""
        async with self._db.acquire() as conn:
            result = await conn.execute(
                "UPDATE whatsapp.user_instances "
                "SET silent_reject = %s, reject_message = %s "
                "WHERE instance_name = %s",
                (silent_reject, reject_message, instance_name),
            )
            await conn.execute("COMMIT")
            return result.rowcount > 0

    async def check_phone_auth(self, instance_name: str, phone: str) -> dict:
        """Check authorization and return reject settings.

        Returns {"authorized": bool, "silent_reject": bool, "reject_message": str}
        Single query for the hot path.
        """
        async with self._db.acquire() as conn:
            # Get instance settings
            inst_row = await conn.execute(
                "SELECT id, silent_reject, reject_message "
                "FROM whatsapp.user_instances "
                "WHERE instance_name = %s",
                (instance_name,),
            )
            inst = await inst_row.fetchone()
            if not inst:
                return {
                    "authorized": False,
                    "silent_reject": True,
                    "reject_message": "",
                }

            # Check if phone is authorized
            auth_row = await conn.execute(
                "SELECT 1 FROM whatsapp.authorized_numbers "
                "WHERE instance_id = %s AND phone = %s",
                (inst["id"], phone),
            )
            authorized = (await auth_row.fetchone()) is not None
            return {
                "authorized": authorized,
                "silent_reject": inst["silent_reject"],
                "reject_message": inst["reject_message"] or "",
            }

    async def get_wake_words(self, instance_id: int) -> list[dict]:
        """Get all wake words for an instance."""
        async with self._db.acquire() as conn:
            rows = await conn.execute(
                "SELECT keyword, response "
                "FROM whatsapp.wake_words "
                "WHERE instance_id = %s "
                "ORDER BY keyword",
                (instance_id,),
            )
            return [dict(r) for r in await rows.fetchall()]

    async def add_wake_word(
        self, instance_id: int, keyword: str, response: str
    ) -> dict | None:
        """Add a wake word. Returns None on duplicate."""
        async with self._db.acquire() as conn:
            try:
                row = await conn.execute(
                    "INSERT INTO whatsapp.wake_words "
                    "(instance_id, keyword, response) "
                    "VALUES (%s, %s, %s) "
                    "RETURNING keyword, response",
                    (instance_id, keyword.lower().strip(), response.strip()),
                )
                r = await row.fetchone()
                await conn.execute("COMMIT")
                return dict(r) if r else None
            except Exception:
                await conn.execute("ROLLBACK")
                return None

    async def remove_wake_word(self, instance_id: int, keyword: str) -> bool:
        """Remove a wake word."""
        async with self._db.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM whatsapp.wake_words "
                "WHERE instance_id = %s AND keyword = %s",
                (instance_id, keyword.lower().strip()),
            )
            await conn.execute("COMMIT")
            return result.rowcount > 0

    async def check_wake_words(self, instance_name: str, text: str) -> str | None:
        """Check if text contains any wake word for the instance.

        Returns the response string if a match is found, None otherwise.
        """
        if not text:
            return None
        text_lower = text.lower()
        async with self._db.acquire() as conn:
            rows = await conn.execute(
                "SELECT ww.keyword, ww.response "
                "FROM whatsapp.wake_words ww "
                "JOIN whatsapp.user_instances ui ON ui.id = ww.instance_id "
                "WHERE ui.instance_name = %s",
                (instance_name,),
            )
            for row in await rows.fetchall():
                if row["keyword"] in text_lower:
                    return row["response"]
        return None

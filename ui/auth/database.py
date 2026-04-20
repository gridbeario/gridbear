"""Authentication database schema and operations.

PostgreSQL backend for:
- Admin users with password hashes and TOTP secrets
- Recovery codes (one-time use)
- Persistent sessions
- Audit log for security events
- WebAuthn credentials
"""

import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

PG_SCHEMA = """
-- Migration: 001_admin_auth
-- NOTE: admin.users no longer created here — the canonical user table
-- is app.users, managed by the ORM (core/models/user.py).
-- Auth-related tables stay in admin schema but FK to app.users.
CREATE SCHEMA IF NOT EXISTS admin;
CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS admin.recovery_codes (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    code_hash TEXT NOT NULL,
    used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS admin.sessions (
    id SERIAL PRIMARY KEY,
    session_token TEXT UNIQUE NOT NULL,
    user_id INTEGER NOT NULL,
    ip_address TEXT,
    user_agent TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    last_activity TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS admin.audit_log (
    id SERIAL PRIMARY KEY,
    user_id INTEGER,
    username TEXT,
    event_type TEXT NOT NULL,
    ip_address TEXT,
    success BOOLEAN,
    details TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS admin.webauthn_credentials (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    credential_id BYTEA NOT NULL UNIQUE,
    public_key BYTEA NOT NULL,
    sign_count INTEGER DEFAULT 0,
    device_name TEXT DEFAULT 'Security Key',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_used_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS admin.user_tool_preferences (
    id SERIAL PRIMARY KEY,
    unified_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(unified_id, tool_name)
);
CREATE INDEX IF NOT EXISTS idx_sessions_token ON admin.sessions(session_token);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON admin.sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON admin.sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_recovery_user ON admin.recovery_codes(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_user ON admin.audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_created ON admin.audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_webauthn_user ON admin.webauthn_credentials(user_id);
CREATE INDEX IF NOT EXISTS idx_webauthn_credential ON admin.webauthn_credentials(credential_id);
CREATE INDEX IF NOT EXISTS idx_tool_prefs_uid ON admin.user_tool_preferences(unified_id);
"""


def _init_pg(db) -> None:
    """Run the PostgreSQL migration if not already applied."""
    with db.acquire_sync() as conn:
        # Check if migration already applied
        row = conn.execute(
            "SELECT 1 FROM public._migrations WHERE name = %s",
            ("001_admin_auth",),
        ).fetchone()
        if row:
            return

        # Execute embedded DDL
        conn.execute(PG_SCHEMA)

        # Register migration
        conn.execute(
            "INSERT INTO public._migrations (name) VALUES (%s)",
            ("001_admin_auth",),
        )
        conn.commit()
        logger.info("Applied migration: 001_admin_auth")


def init_auth_db() -> None:
    """Initialize the authentication database (PostgreSQL)."""
    from core.registry import get_database

    db = get_database()
    if db is None:
        raise RuntimeError("DatabaseManager not available for auth")
    _init_pg(db)


class AuthDatabase:
    """Database operations for authentication system (PostgreSQL).

    Uses ORM models for standard CRUD; raw SQL only where needed
    (increment, upsert, IS NULL WHERE clauses).
    """

    def __init__(self):
        from core.registry import get_database

        self._db = get_database()
        if self._db is None:
            raise RuntimeError("DatabaseManager not available for auth")
        _init_pg(self._db)
        logger.debug("AuthDatabase: using PostgreSQL backend")

    # ──────────────────────────────────────────────
    # User operations
    # ──────────────────────────────────────────────

    def create_user(
        self,
        username: str,
        password_hash: str,
        email: Optional[str] = None,
        is_superadmin: bool = False,
        display_name: Optional[str] = None,
        locale: str = "en",
    ) -> int:
        """Create a new user. Returns the user ID."""
        from ui.auth.models import AdminUser

        row = AdminUser.create_sync(
            username=username.lower(),
            password_hash=password_hash,
            email=email,
            is_superadmin=is_superadmin,
            display_name=display_name,
            locale=locale,
            company_id=1,
        )
        user_id = row["id"]

        # Auto-assign new user to default company (id=1)
        try:
            from core.models.company_user import CompanyUser

            CompanyUser.create_sync(
                company_id=1, user_id=user_id, role="member", is_default=True
            )
        except Exception as exc:
            logger.debug("Auto-assign to default company: %s", exc)

        return user_id

    def get_user_by_username(self, username: str) -> Optional[dict]:
        """Get user by username."""
        from ui.auth.models import AdminUser

        results = AdminUser.search_sync(
            [("username", "=", username.lower()), ("is_active", "=", True)],
            limit=1,
        )
        return dict(results[0]) if results else None

    def get_user_by_id(self, user_id: int) -> Optional[dict]:
        """Get user by ID."""
        from ui.auth.models import AdminUser

        results = AdminUser.search_sync([("id", "=", user_id)], limit=1)
        return dict(results[0]) if results else None

    def get_all_users(self) -> list[dict]:
        """Get all admin users."""
        from ui.auth.models import AdminUser

        rows = AdminUser.search_sync([], order="username")
        return [dict(r) for r in rows]

    def user_count(self) -> int:
        """Get total number of admin users."""
        from ui.auth.models import AdminUser

        return AdminUser.count_sync()

    def update_user(self, user_id: int, **fields) -> bool:
        """Update user fields."""
        if not fields:
            return False

        allowed_fields = {
            "username",
            "email",
            "password_hash",
            "totp_secret",
            "totp_enabled",
            "is_active",
            "is_superadmin",
            "display_name",
            "avatar_url",
            "locale",
            "last_login",
            "failed_login_attempts",
            "lockout_until",
            "webauthn_enabled",
        }
        fields = {k: v for k, v in fields.items() if k in allowed_fields}
        if not fields:
            return False

        from ui.auth.models import AdminUser

        return AdminUser.write_sync(user_id, **fields) is not None

    def increment_failed_attempts(self, user_id: int) -> int:
        """Increment failed login attempts and return new count."""
        from ui.auth.models import AdminUser

        rows = AdminUser.raw_search_sync(
            "UPDATE {table} "
            "SET failed_login_attempts = failed_login_attempts + 1 "
            "WHERE id = %s "
            "RETURNING failed_login_attempts",
            (user_id,),
        )
        return rows[0]["failed_login_attempts"] if rows else 0

    def reset_failed_attempts(self, user_id: int) -> None:
        """Reset failed login attempts to zero."""
        self.update_user(user_id, failed_login_attempts=0, lockout_until=None)

    def set_lockout(self, user_id: int, until: datetime) -> None:
        """Set lockout until specified time."""
        self.update_user(user_id, lockout_until=until)

    def is_locked_out(self, user_id: int) -> bool:
        """Check if user is currently locked out."""
        user = self.get_user_by_id(user_id)
        if not user or not user.get("lockout_until"):
            return False
        lockout = user["lockout_until"]
        if isinstance(lockout, str):
            lockout = datetime.fromisoformat(lockout)
        elif hasattr(lockout, "tzinfo") and lockout.tzinfo is not None:
            lockout = lockout.replace(tzinfo=None)
        return datetime.now() < lockout

    def delete_user(self, user_id: int) -> bool:
        """Delete a user (cascades to sessions and recovery codes)."""
        from ui.auth.models import AdminUser

        return AdminUser.delete_sync(user_id) > 0

    # ──────────────────────────────────────────────
    # Recovery codes operations
    # ──────────────────────────────────────────────

    def add_recovery_codes(self, user_id: int, code_hashes: list[str]) -> None:
        """Add recovery codes for a user (replaces existing)."""
        from ui.auth.models import RecoveryCode

        RecoveryCode.delete_multi_sync([("user_id", "=", user_id)])
        for code_hash in code_hashes:
            RecoveryCode.create_sync(user_id=user_id, code_hash=code_hash)

    def get_recovery_codes(self, user_id: int) -> list[dict]:
        """Get all recovery codes for a user."""
        from ui.auth.models import RecoveryCode

        rows = RecoveryCode.search_sync([("user_id", "=", user_id)], order="id")
        return [dict(r) for r in rows]

    def get_unused_recovery_codes(self, user_id: int) -> list[dict]:
        """Get unused recovery codes for a user."""
        from ui.auth.models import RecoveryCode

        rows = RecoveryCode.search_sync(
            [("user_id", "=", user_id), ("used_at", "is", None)],
            order="id",
        )
        return [dict(r) for r in rows]

    def mark_recovery_code_used(self, code_id: int) -> bool:
        """Mark a recovery code as used."""
        from ui.auth.models import RecoveryCode

        updated = RecoveryCode.write_multi_sync(
            [("id", "=", code_id), ("used_at", "is", None)],
            used_at=datetime.now(),
        )
        return updated > 0

    # ──────────────────────────────────────────────
    # Session operations
    # ──────────────────────────────────────────────

    def create_session(
        self,
        session_token: str,
        user_id: int,
        expires_at: datetime,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> int:
        """Create a new session."""
        from ui.auth.models import AdminSession

        row = AdminSession.create_sync(
            session_token=session_token,
            user_id=user_id,
            ip_address=ip_address,
            user_agent=user_agent,
            expires_at=expires_at,
            last_activity=datetime.now(),
        )
        return row["id"]

    def get_session(self, session_token: str) -> Optional[dict]:
        """Get session by token."""
        from ui.auth.models import AdminSession

        results = AdminSession.search_sync(
            [("session_token", "=", session_token)], limit=1
        )
        return dict(results[0]) if results else None

    def get_user_sessions(self, user_id: int) -> list[dict]:
        """Get all sessions for a user."""
        from ui.auth.models import AdminSession

        rows = AdminSession.search_sync(
            [("user_id", "=", user_id)], order="created_at DESC"
        )
        return [dict(r) for r in rows]

    def update_session_activity(
        self,
        session_token: str,
        new_expires_at: Optional[datetime] = None,
    ) -> bool:
        """Update last activity timestamp, and optionally slide expires_at."""
        from ui.auth.models import AdminSession

        fields = {"last_activity": datetime.now()}
        if new_expires_at is not None:
            fields["expires_at"] = new_expires_at
        updated = AdminSession.write_multi_sync(
            [("session_token", "=", session_token)],
            **fields,
        )
        return updated > 0

    def delete_session(self, session_token: str) -> bool:
        """Delete a session."""
        from ui.auth.models import AdminSession

        return (
            AdminSession.delete_multi_sync([("session_token", "=", session_token)]) > 0
        )

    def delete_user_sessions(
        self,
        user_id: int,
        except_token: Optional[str] = None,
    ) -> int:
        """Delete all sessions for a user, optionally except one."""
        from ui.auth.models import AdminSession

        domain = [("user_id", "=", user_id)]
        if except_token:
            domain.append(("session_token", "!=", except_token))
        return AdminSession.delete_multi_sync(domain)

    def cleanup_expired_sessions(self) -> int:
        """Delete all expired sessions."""
        from ui.auth.models import AdminSession

        return AdminSession.delete_multi_sync([("expires_at", "<", datetime.now())])

    # ──────────────────────────────────────────────
    # Audit log operations
    # ──────────────────────────────────────────────

    def log_event(
        self,
        event_type: str,
        user_id: Optional[int] = None,
        username: Optional[str] = None,
        ip_address: Optional[str] = None,
        success: bool = True,
        details: Optional[str] = None,
    ) -> int:
        """Log an authentication event."""
        from ui.auth.models import AuditLog

        row = AuditLog.create_sync(
            user_id=user_id,
            username=username,
            event_type=event_type,
            ip_address=ip_address,
            success=success,
            details=details,
        )
        return row["id"]

    def get_audit_logs(
        self,
        user_id: Optional[int] = None,
        event_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Get audit log entries."""
        from ui.auth.models import AuditLog

        domain = []
        if user_id is not None:
            domain.append(("user_id", "=", user_id))
        if event_type:
            domain.append(("event_type", "=", event_type))

        rows = AuditLog.search_sync(
            domain, order="created_at DESC", limit=limit, offset=offset
        )
        return [dict(r) for r in rows]

    # ──────────────────────────────────────────────
    # WebAuthn credentials
    # ──────────────────────────────────────────────

    def add_webauthn_credential(
        self,
        user_id: int,
        credential_id: bytes,
        public_key: bytes,
        sign_count: int,
        device_name: str = "Security Key",
    ) -> int:
        """Store a new WebAuthn credential. Returns the row ID."""
        from ui.auth.models import WebAuthnCredential

        row = WebAuthnCredential.create_sync(
            user_id=user_id,
            credential_id=credential_id,
            public_key=public_key,
            sign_count=sign_count,
            device_name=device_name,
        )
        return row["id"]

    def get_webauthn_credentials(self, user_id: int) -> list[dict]:
        """Get all WebAuthn credentials for a user."""
        from ui.auth.models import WebAuthnCredential

        rows = WebAuthnCredential.search_sync(
            [("user_id", "=", user_id)], order="created_at"
        )
        return [dict(r) for r in rows]

    def get_webauthn_credential_by_id(self, credential_id: bytes) -> dict | None:
        """Get a WebAuthn credential by its credential_id."""
        from ui.auth.models import WebAuthnCredential

        results = WebAuthnCredential.search_sync(
            [("credential_id", "=", credential_id)], limit=1
        )
        return dict(results[0]) if results else None

    def update_webauthn_sign_count(self, credential_id: bytes, sign_count: int) -> None:
        """Update the sign count for a credential after authentication."""
        from ui.auth.models import WebAuthnCredential

        WebAuthnCredential.write_multi_sync(
            [("credential_id", "=", credential_id)],
            sign_count=sign_count,
            last_used_at=datetime.now(),
        )

    def rename_webauthn_credential(
        self, cred_id: int, user_id: int, new_name: str
    ) -> bool:
        """Rename a WebAuthn credential. Returns True if found and updated."""
        from ui.auth.models import WebAuthnCredential

        updated = WebAuthnCredential.write_multi_sync(
            [("id", "=", cred_id), ("user_id", "=", user_id)],
            device_name=new_name,
        )
        return updated > 0

    def delete_webauthn_credential(self, cred_id: int, user_id: int) -> bool:
        """Delete a WebAuthn credential. Returns True if found and deleted."""
        from ui.auth.models import WebAuthnCredential

        return (
            WebAuthnCredential.delete_multi_sync(
                [("id", "=", cred_id), ("user_id", "=", user_id)]
            )
            > 0
        )

    def count_webauthn_credentials(self, user_id: int) -> int:
        """Count WebAuthn credentials for a user."""
        from ui.auth.models import WebAuthnCredential

        return WebAuthnCredential.count_sync([("user_id", "=", user_id)])

    def cleanup_old_audit_logs(self, days: int = 90) -> int:
        """Delete audit logs older than specified days."""
        from datetime import timedelta

        from ui.auth.models import AuditLog

        cutoff = datetime.now() - timedelta(days=days)
        return AuditLog.delete_multi_sync([("created_at", "<", cutoff)])

    # ──────────────────────────────────────────────
    # User tool preferences (migrated from user_preferences.db)
    # ──────────────────────────────────────────────

    def get_user_tool_prefs(self, unified_id: str) -> dict[str, bool]:
        """Get all tool preferences for a user."""
        from ui.auth.models import UserToolPreference

        rows = UserToolPreference.search_sync([("unified_id", "=", unified_id)])
        return {r["tool_name"]: r["enabled"] for r in rows}

    def set_user_tool_pref(
        self, unified_id: str, tool_name: str, enabled: bool
    ) -> None:
        """Set a tool preference for a user (upsert)."""
        from ui.auth.models import UserToolPreference

        UserToolPreference.raw_execute_sync(
            "INSERT INTO {table} (unified_id, tool_name, enabled) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT(unified_id, tool_name) DO UPDATE SET enabled = EXCLUDED.enabled",
            (unified_id, tool_name, enabled),
        )

    def get_disabled_tools(self, unified_id: str) -> set[str]:
        """Get the set of tool names the user has explicitly disabled."""
        from ui.auth.models import UserToolPreference

        rows = UserToolPreference.search_sync(
            [("unified_id", "=", unified_id), ("enabled", "=", False)]
        )
        return {r["tool_name"] for r in rows}

    # ──────────────────────────────────────────────
    # Agent tool preferences (per-agent tool disable)
    # ──────────────────────────────────────────────

    def get_agent_disabled_tools(self, agent_name: str) -> set[str]:
        """Get the set of tool names disabled for an agent."""
        from ui.auth.models import AgentToolPreference

        rows = AgentToolPreference.search_sync(
            [("agent_name", "=", agent_name), ("enabled", "=", False)]
        )
        return {r["tool_name"] for r in rows}

    def set_agent_tool_pref(
        self, agent_name: str, tool_name: str, enabled: bool
    ) -> None:
        """Set a tool preference for an agent (upsert)."""
        from ui.auth.models import AgentToolPreference

        AgentToolPreference.raw_execute_sync(
            "INSERT INTO {table} (agent_name, tool_name, enabled) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT(agent_name, tool_name) DO UPDATE SET enabled = EXCLUDED.enabled",
            (agent_name, tool_name, enabled),
        )


# Singleton instance - lazy initialization for testability
_auth_db_instance: Optional[AuthDatabase] = None


def get_auth_db() -> AuthDatabase:
    """Get the singleton AuthDatabase instance.

    Uses lazy initialization to allow tests to patch paths
    before the instance is created.
    """
    global _auth_db_instance
    if _auth_db_instance is None:
        _auth_db_instance = AuthDatabase()
    return _auth_db_instance


def reset_auth_db() -> None:
    """Reset the singleton instance. Used for testing."""
    global _auth_db_instance
    _auth_db_instance = None


# Backward compatibility alias
class _AuthDbProxy:
    """Proxy for backward compatibility with `auth_db` usage."""

    def __getattr__(self, name):
        return getattr(get_auth_db(), name)


auth_db = _AuthDbProxy()

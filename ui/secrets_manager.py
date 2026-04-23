"""Secrets Manager for GridBear.

Provides encrypted storage for sensitive data like API keys and tokens.
Uses AES-256-GCM for authenticated encryption.
Master key is derived from SSH private key (~/.ssh/id_rsa or id_ed25519).

Backend: PostgreSQL (vault.secrets schema).

Usage:
    from ui.secrets_manager import secrets_manager

    # Store a secret
    secrets_manager.set("TELEGRAM_BOT_TOKEN", "your-token-here")

    # Retrieve a secret
    token = secrets_manager.get("TELEGRAM_BOT_TOKEN")

    # Get with fallback to env var (for migration)
    token = secrets_manager.get("TELEGRAM_BOT_TOKEN", fallback_env=True)

    # Export/Import for key rotation
    data = secrets_manager.export_all()  # Returns dict
    secrets_manager.import_all(data)     # Re-encrypts with current key
"""

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

BASE_DIR = Path(__file__).resolve().parent.parent

# Key paths to try (in order of preference)
# 1. GridBear-specific key in config dir (preferred - portable)
# 2. SSH keys as fallback
# Candidate key file locations, in preference order. Dedicated
# config/secrets.key wins; the home-dir SSH paths remain for operators
# who were relying on the fallback. The hard-coded /root/.ssh/* paths
# have been removed: the container should not run as root, and if it
# does, using a root-owned SSH key as the vault master key is a
# host-pinned surprise (different host → different key → vault lost).
KEY_PATHS = [
    BASE_DIR / "config" / "secrets.key",
    Path.home() / ".ssh" / "id_ed25519",
    Path.home() / ".ssh" / "id_rsa",
    Path("/app/config/secrets.key"),
]

MASTER_KEY_ENV = "GRIDBEAR_MASTER_KEY"

# PostgreSQL DDL — applied once via _init_pg()
PG_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS vault;
CREATE SCHEMA IF NOT EXISTS oauth2;

CREATE TABLE IF NOT EXISTS vault.secrets (
    key_name TEXT PRIMARY KEY,
    encrypted_value TEXT NOT NULL,
    nonce TEXT NOT NULL,
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS oauth2.clients (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    client_id TEXT NOT NULL UNIQUE,
    client_secret_hash TEXT,
    client_type TEXT NOT NULL CHECK(client_type IN ('confidential', 'public')) DEFAULT 'confidential',
    redirect_uris TEXT,
    allowed_scopes TEXT DEFAULT 'openid profile email',
    access_token_expiry INTEGER DEFAULT 3600,
    refresh_token_expiry INTEGER DEFAULT 2592000,
    require_pkce BOOLEAN DEFAULT TRUE,
    agent_name TEXT,
    gridbear_user_id TEXT,
    mcp_permissions TEXT,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    description TEXT
);

CREATE TABLE IF NOT EXISTS oauth2.authorization_codes (
    id SERIAL PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    client_id INTEGER NOT NULL REFERENCES oauth2.clients(id),
    user_identity TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    scope TEXT,
    code_challenge TEXT,
    code_challenge_method TEXT DEFAULT 'S256',
    state TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    used BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS oauth2.access_tokens (
    id SERIAL PRIMARY KEY,
    token TEXT NOT NULL UNIQUE,
    token_type TEXT DEFAULT 'Bearer',
    client_id INTEGER NOT NULL REFERENCES oauth2.clients(id),
    user_identity TEXT,
    scope TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    refresh_token TEXT UNIQUE,
    refresh_expires_at TIMESTAMPTZ,
    revoked BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_used_at TIMESTAMPTZ,
    ip_address TEXT,
    user_agent TEXT
);

CREATE INDEX IF NOT EXISTS idx_oauth2_client_client_id ON oauth2.clients(client_id);
CREATE INDEX IF NOT EXISTS idx_oauth2_client_agent ON oauth2.clients(agent_name);
CREATE INDEX IF NOT EXISTS idx_oauth2_auth_code ON oauth2.authorization_codes(code);
CREATE INDEX IF NOT EXISTS idx_oauth2_auth_expires ON oauth2.authorization_codes(expires_at);
CREATE INDEX IF NOT EXISTS idx_oauth2_token_token ON oauth2.access_tokens(token);
CREATE INDEX IF NOT EXISTS idx_oauth2_token_refresh ON oauth2.access_tokens(refresh_token);
CREATE INDEX IF NOT EXISTS idx_oauth2_token_client ON oauth2.access_tokens(client_id);
CREATE INDEX IF NOT EXISTS idx_oauth2_token_expires ON oauth2.access_tokens(expires_at);
"""


def _init_pg(db) -> None:
    """Run the PostgreSQL migration if not already applied."""
    with db.acquire_sync() as conn:
        row = conn.execute(
            "SELECT 1 FROM public._migrations WHERE name = %s",
            ("002_secrets_oauth2",),
        ).fetchone()
        if row:
            return

        conn.execute(PG_SCHEMA)
        conn.execute(
            "INSERT INTO public._migrations (name) VALUES (%s)",
            ("002_secrets_oauth2",),
        )
        conn.commit()
        import logging

        logging.getLogger("gridbear").info("Applied migration: 002_secrets_oauth2")


class SecretsManager:
    """Encrypted secrets storage using AES-256-GCM with PostgreSQL backend."""

    def __init__(self, ssh_key_path: Path = None):
        self.ssh_key_path = ssh_key_path
        self._key: Optional[bytes] = None
        self._key_source: Optional[str] = None
        self._pg = None

        # PostgreSQL detection (lazy import to avoid circular deps at module load)
        from core.registry import get_database

        db = get_database()
        if db:
            _init_pg(db)
            self._pg = db

    def _require_pg(self):
        """Raise if PostgreSQL is not available."""
        if self._pg is None:
            raise RuntimeError(
                "SecretsManager: PostgreSQL not available. "
                "Ensure DATABASE_URL is set and reset_secrets_manager() is called after DB init."
            )

    def _find_key_file(self) -> Optional[Path]:
        """Find an encryption key file."""
        try:
            if self.ssh_key_path and self.ssh_key_path.exists():
                return self.ssh_key_path
        except (PermissionError, OSError):
            pass

        for path in KEY_PATHS:
            try:
                # os.access(R_OK) is the right check: `.exists()` succeeds
                # on a file the non-root container user can stat but not
                # read (e.g. /root/.ssh/id_ed25519 on a deploy where
                # KEY_PATHS still lists it). Picking such a path makes
                # downstream read_bytes() crash with PermissionError.
                if path.exists() and os.access(path, os.R_OK):
                    return path
            except (PermissionError, OSError):
                continue
        return None

    @staticmethod
    def generate_key_file(path: Path = None) -> Path:
        """Generate a new random key file for GridBear secrets.

        Args:
            path: Where to save the key. Defaults to config/secrets.key

        Returns:
            Path to the generated key file
        """
        import secrets as stdlib_secrets

        if path is None:
            path = BASE_DIR / "config" / "secrets.key"

        path.parent.mkdir(parents=True, exist_ok=True)
        # Tighten the parent directory — the key file itself is 0600,
        # but a 0755 containing dir still lets any local user enumerate
        # "there is a secret here" and race a replacement if perms ever
        # slip. 0700 matches the OpenSSH .ssh convention.
        try:
            path.parent.chmod(0o700)
        except OSError:
            # Non-fatal: shared filesystems (e.g. Windows bind mounts)
            # reject chmod. The file permission above is still enforced.
            pass

        # Generate 64 bytes of random data (512 bits)
        key_data = stdlib_secrets.token_bytes(64)

        # Write with restrictive permissions
        path.write_bytes(key_data)
        path.chmod(0o600)

        return path

    def _get_master_key(self) -> bytes:
        """Get or derive the master encryption key from key file."""
        if self._key:
            return self._key

        # Try key file first
        key_path = self._find_key_file()
        if key_path:
            try:
                key_content = key_path.read_bytes()
                # Derive a 256-bit key using SHA-256 of the key file content
                self._key = hashlib.sha256(key_content).digest()
                self._key_source = str(key_path)
                # Purge the env-var fallback now that we've derived the
                # key from a file — leaving GRIDBEAR_MASTER_KEY visible
                # in os.environ would let any child process
                # (plugin subprocesses, Claude CLI, Playwright, MCP
                # stdio servers) read the plaintext master key out of
                # /proc/<pid>/environ, which bypasses the AES layer
                # entirely.
                os.environ.pop(MASTER_KEY_ENV, None)
                return self._key
            except PermissionError:
                pass  # Fall through to env var

        # Fallback to environment variable (try new name, then legacy)
        master_key = os.getenv(MASTER_KEY_ENV)
        if master_key:
            self._key = hashlib.sha256(master_key.encode()).digest()
            self._key_source = f"env:{MASTER_KEY_ENV}"
            # Same purge rationale as above — the env var is no longer
            # needed once we've derived the key.
            os.environ.pop(MASTER_KEY_ENV, None)
            return self._key

        raise RuntimeError(
            "No encryption key found. Either:\n"
            '1. Run: python -c "from ui.secrets_manager import SecretsManager; SecretsManager.generate_key_file()"\n'
            "2. Or ensure SSH key exists at ~/.ssh/id_rsa or ~/.ssh/id_ed25519\n"
            f"3. Or set {MASTER_KEY_ENV} environment variable"
        )

    def get_key_source(self) -> Optional[str]:
        """Return the source of the master key (for display)."""
        if not self._key_source:
            try:
                self._get_master_key()
            except RuntimeError:
                return None
        return self._key_source

    def is_available(self) -> bool:
        """Check if encryption is available and PG is connected."""
        if self._pg is None:
            return False
        try:
            self._get_master_key()
            return True
        except RuntimeError:
            return False

    def _encrypt(self, plaintext: str) -> tuple[str, str]:
        """Encrypt plaintext using AES-256-GCM.

        Returns:
            Tuple of (base64 encrypted value, base64 nonce)
        """
        key = self._get_master_key()
        aesgcm = AESGCM(key)

        # Generate random nonce (96 bits for GCM)
        nonce = os.urandom(12)

        # Encrypt
        ciphertext = aesgcm.encrypt(nonce, plaintext.encode(), None)

        # Return base64 encoded
        return (
            base64.b64encode(ciphertext).decode(),
            base64.b64encode(nonce).decode(),
        )

    def _decrypt(self, encrypted_value: str, nonce: str) -> str:
        """Decrypt ciphertext using AES-256-GCM."""
        key = self._get_master_key()
        aesgcm = AESGCM(key)

        ciphertext = base64.b64decode(encrypted_value)
        nonce_bytes = base64.b64decode(nonce)

        plaintext = aesgcm.decrypt(nonce_bytes, ciphertext, None)
        return plaintext.decode()

    def set(self, key_name: str, value: str, description: str = None) -> None:
        """Store an encrypted secret."""
        self._require_pg()
        encrypted_value, nonce = self._encrypt(value)

        with self._pg.acquire_sync() as conn:
            conn.execute(
                """INSERT INTO vault.secrets
                   (key_name, encrypted_value, nonce, description, updated_at)
                   VALUES (%s, %s, %s, %s, NOW())
                   ON CONFLICT(key_name) DO UPDATE SET
                       encrypted_value = EXCLUDED.encrypted_value,
                       nonce = EXCLUDED.nonce,
                       description = COALESCE(EXCLUDED.description, vault.secrets.description),
                       updated_at = NOW()
                """,
                (key_name, encrypted_value, nonce, description),
            )
            conn.commit()

    def get(self, key_name: str, fallback_env: bool = True) -> SecretStr | None:
        """Retrieve a decrypted secret wrapped in SecretStr.

        Returns SecretStr to prevent accidental exposure in logs/tracebacks.
        Use get_plain() when you need the raw string (API calls, env vars).
        """
        if self._pg is None:
            # Before PG init, only env fallback is available
            if fallback_env:
                env_val = os.getenv(key_name)
                return SecretStr(env_val) if env_val is not None else None
            return None

        with self._pg.acquire_sync() as conn:
            row = conn.execute(
                "SELECT encrypted_value, nonce FROM vault.secrets WHERE key_name = %s",
                (key_name,),
            ).fetchone()

        if row:
            try:
                return SecretStr(self._decrypt(row["encrypted_value"], row["nonce"]))
            except Exception:
                pass

        if fallback_env:
            env_val = os.getenv(key_name)
            return SecretStr(env_val) if env_val is not None else None

        return None

    def get_plain(
        self, key_name: str, fallback_env: bool = True, default: str = ""
    ) -> str:
        """Get secret as plain string for API calls, env vars, subprocess args.

        Convenience wrapper around get() that unwraps SecretStr and provides
        a default value when the secret is not found.
        """
        secret = self.get(key_name, fallback_env=fallback_env)
        return secret.get_secret_value() if secret else default

    def delete(self, key_name: str) -> bool:
        """Delete a secret. Returns True if deleted, False if not found."""
        self._require_pg()

        with self._pg.acquire_sync() as conn:
            cursor = conn.execute(
                "DELETE FROM vault.secrets WHERE key_name = %s", (key_name,)
            )
            conn.commit()
            return cursor.rowcount > 0

    def list_keys(self) -> list[dict]:
        """List all secret keys (without values)."""
        self._require_pg()

        with self._pg.acquire_sync() as conn:
            rows = conn.execute(
                """SELECT key_name, description, created_at, updated_at
                   FROM vault.secrets ORDER BY key_name"""
            ).fetchall()
            return [
                {
                    "key_name": r["key_name"],
                    "description": r["description"],
                    "created_at": str(r["created_at"]) if r["created_at"] else None,
                    "updated_at": str(r["updated_at"]) if r["updated_at"] else None,
                }
                for r in rows
            ]

    def list_keys_by_prefix(self, prefix: str) -> list[dict]:
        """List secret keys matching a prefix (without values)."""
        self._require_pg()

        with self._pg.acquire_sync() as conn:
            rows = conn.execute(
                """SELECT key_name, description, created_at, updated_at
                   FROM vault.secrets
                   WHERE key_name LIKE %s
                   ORDER BY key_name""",
                (prefix + "%",),
            ).fetchall()
            return [
                {
                    "key_name": r["key_name"],
                    "description": r["description"],
                    "created_at": str(r["created_at"]) if r["created_at"] else None,
                    "updated_at": str(r["updated_at"]) if r["updated_at"] else None,
                }
                for r in rows
            ]

    def exists(self, key_name: str) -> bool:
        """Check if a secret exists."""
        self._require_pg()

        with self._pg.acquire_sync() as conn:
            row = conn.execute(
                "SELECT 1 FROM vault.secrets WHERE key_name = %s", (key_name,)
            ).fetchone()
            return row is not None

    def import_from_env(
        self, key_names: list[str], overwrite: bool = False
    ) -> dict[str, bool]:
        """Import secrets from environment variables."""
        results = {}
        for key_name in key_names:
            value = os.getenv(key_name)
            if not value:
                results[key_name] = False
                continue

            if self.exists(key_name) and not overwrite:
                results[key_name] = False
                continue

            try:
                self.set(
                    key_name, value, description=f"Imported from env var {key_name}"
                )
                results[key_name] = True
            except Exception:
                results[key_name] = False

        return results

    def export_all(self) -> dict:
        """Export all secrets as a dictionary (for backup/migration).

        WARNING: This returns plaintext secrets!
        """
        from datetime import datetime

        secrets_list = []
        for secret in self.list_keys():
            key_name = secret["key_name"]
            value = self.get_plain(key_name, fallback_env=False)
            if value:
                secrets_list.append(
                    {
                        "key": key_name,
                        "value": value,
                        "description": secret.get("description", ""),
                    }
                )

        return {
            "secrets": secrets_list,
            "exported_at": datetime.now().isoformat(),
            "key_source": self._key_source,
        }

    def export_to_json(self) -> str:
        """Export all secrets as JSON string."""
        return json.dumps(self.export_all(), indent=2)

    def import_all(self, data: dict, overwrite: bool = True) -> dict[str, bool]:
        """Import secrets from exported data."""
        results = {}
        secrets_list = data.get("secrets", [])

        for item in secrets_list:
            key_name = item.get("key")
            value = item.get("value")
            description = item.get("description", "")

            if not key_name or not value:
                continue

            if self.exists(key_name) and not overwrite:
                results[key_name] = False
                continue

            try:
                self.set(key_name, value, description=description)
                results[key_name] = True
            except Exception:
                results[key_name] = False

        return results

    def import_from_json(
        self, json_str: str, overwrite: bool = True
    ) -> dict[str, bool]:
        """Import secrets from JSON string."""
        data = json.loads(json_str)
        return self.import_all(data, overwrite=overwrite)


# Singleton instance (PG not ready yet at import time — reset after DB init)
secrets_manager = SecretsManager()


def reset_secrets_manager() -> None:
    """Reinitialize the singleton's PG connection after pool is ready.

    Updates the existing instance in-place so that all modules which imported
    ``secrets_manager`` at the module level keep a valid reference.
    """
    from core.registry import get_database

    db = get_database()
    if db:
        _init_pg(db)
        secrets_manager._pg = db
        # Reset cached key so it's reloaded from PG on next access
        secrets_manager._key = None
        secrets_manager._key_source = None


def get_secret(key_name: str, fallback_env: bool = True) -> SecretStr | None:
    """Convenience function to get a secret (returns SecretStr)."""
    return secrets_manager.get(key_name, fallback_env=fallback_env)

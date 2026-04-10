"""OAuth2 Data Models.

PostgreSQL-backed storage for OAuth2 clients, authorization codes, and access tokens.
Uses oauth2 schema (oauth2.clients, oauth2.authorization_codes, oauth2.access_tokens).
Based on the pattern from dub_oauth2_provider (Odoo module), adapted for FastAPI.
"""

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from config.logging_config import logger

PG_SCHEMA = "admin"  # OAuth2 tables live in admin schema


def _parse_utc(val) -> datetime:
    """Parse datetime value, handling both strings and datetime objects."""
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val
    dt = datetime.fromisoformat(str(val))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dt_to_str(val) -> str | None:
    """Convert a datetime or string to ISO string for dataclass fields."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)


@dataclass
class OAuth2Client:
    id: int
    name: str
    client_id: str
    client_secret_hash: str | None
    client_type: str
    redirect_uris: str | None
    allowed_scopes: str
    access_token_expiry: int
    refresh_token_expiry: int
    require_pkce: bool
    agent_name: str | None
    gridbear_user_id: str | None
    mcp_permissions: str | None
    active: bool
    created_at: str
    description: str | None

    def get_redirect_uris(self) -> list[str]:
        if not self.redirect_uris:
            return []
        return [uri.strip() for uri in self.redirect_uris.split("\n") if uri.strip()]

    def validate_redirect_uri(self, uri: str) -> bool:
        return uri in self.get_redirect_uris()

    def validate_scope(self, scope: str) -> bool:
        if not scope:
            return True
        allowed = set(self.allowed_scopes.split()) if self.allowed_scopes else set()
        requested = set(scope.split())
        return requested.issubset(allowed)

    def verify_secret(self, secret: str) -> bool:
        if not secret or not self.client_secret_hash:
            return False
        computed = hashlib.sha256(secret.encode()).hexdigest()
        return hmac.compare_digest(self.client_secret_hash, computed)

    def get_mcp_permissions_list(self) -> list[str] | None:
        if not self.mcp_permissions:
            return None
        try:
            return json.loads(self.mcp_permissions)
        except (json.JSONDecodeError, TypeError):
            return None

    @staticmethod
    def verify_pkce(
        code_verifier: str, code_challenge: str, method: str = "S256"
    ) -> bool:
        if method != "S256":
            return False
        digest = hashlib.sha256(code_verifier.encode()).digest()
        computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        return hmac.compare_digest(computed, code_challenge)


@dataclass
class OAuth2AuthorizationCode:
    id: int
    code: str
    client_id: int
    user_identity: str
    redirect_uri: str
    scope: str | None
    code_challenge: str | None
    code_challenge_method: str | None
    state: str | None
    expires_at: str
    used: bool

    def is_valid(self) -> bool:
        if self.used:
            return False
        expires = _parse_utc(self.expires_at)
        return _utcnow() < expires


@dataclass
class OAuth2AccessToken:
    id: int
    token: str
    token_type: str
    client_id: int
    user_identity: str | None
    scope: str | None
    expires_at: str
    refresh_token: str | None
    refresh_expires_at: str | None
    revoked: bool
    created_at: str
    last_used_at: str | None
    ip_address: str | None
    user_agent: str | None

    def is_valid(self) -> bool:
        if self.revoked:
            return False
        expires = _parse_utc(self.expires_at)
        return _utcnow() < expires

    def is_refresh_valid(self) -> bool:
        if self.revoked or not self.refresh_token or not self.refresh_expires_at:
            return False
        expires = _parse_utc(self.refresh_expires_at)
        return _utcnow() < expires

    @property
    def expires_in(self) -> int:
        expires = _parse_utc(self.expires_at)
        created = _parse_utc(self.created_at)
        return int((expires - created).total_seconds())


# ── ORM Models ──────────────────────────────────────────────────

from core.orm import Model, fields


class ClientRecord(Model):
    """ORM model for oauth2.clients table."""

    _schema = "oauth2"
    _name = "clients"

    name = fields.Text(required=True)
    client_id = fields.Text(required=True, unique=True)
    client_secret_hash = fields.Text()
    client_type = fields.Text(required=True, default="confidential")
    redirect_uris = fields.Text()
    allowed_scopes = fields.Text(default="openid profile email")
    access_token_expiry = fields.Integer(default=3600)
    refresh_token_expiry = fields.Integer(default=2592000)
    require_pkce = fields.Boolean(default=True)
    agent_name = fields.Text()
    gridbear_user_id = fields.Text()
    mcp_permissions = fields.Text()
    active = fields.Boolean(default=True)
    created_at = fields.DateTime(auto_now_add=True)
    description = fields.Text()


class AuthCodeRecord(Model):
    """ORM model for oauth2.authorization_codes table."""

    _schema = "oauth2"
    _name = "authorization_codes"

    code = fields.Text(required=True, unique=True)
    client_id = fields.ForeignKey(ClientRecord, on_delete="CASCADE")
    user_identity = fields.Text(required=True)
    redirect_uri = fields.Text(required=True)
    scope = fields.Text()
    code_challenge = fields.Text()
    code_challenge_method = fields.Text(default="S256")
    state = fields.Text()
    expires_at = fields.DateTime(required=True)
    used = fields.Boolean(default=False)


class TokenRecord(Model):
    """ORM model for oauth2.access_tokens table."""

    _schema = "oauth2"
    _name = "access_tokens"

    token = fields.Text(required=True, unique=True)
    token_type = fields.Text(default="Bearer")
    client_id = fields.ForeignKey(ClientRecord, on_delete="CASCADE")
    user_identity = fields.Text()
    scope = fields.Text()
    expires_at = fields.DateTime(required=True)
    refresh_token = fields.Text(unique=True)
    refresh_expires_at = fields.DateTime()
    revoked = fields.Boolean(default=False)
    created_at = fields.DateTime(auto_now_add=True)
    last_used_at = fields.DateTime()
    ip_address = fields.Text()
    user_agent = fields.Text()


# ── Row conversion helpers ──────────────────────────────────────


def _row_to_client(row) -> OAuth2Client:
    """Convert a PG dict row to OAuth2Client."""
    d = dict(row)
    d["created_at"] = _dt_to_str(d.get("created_at"))
    return OAuth2Client(**d)


def _row_to_auth_code(row) -> OAuth2AuthorizationCode:
    d = dict(row)
    d["expires_at"] = _dt_to_str(d.get("expires_at"))
    return OAuth2AuthorizationCode(**d)


def _row_to_token(row) -> OAuth2AccessToken:
    d = dict(row)
    for f in ("expires_at", "refresh_expires_at", "created_at", "last_used_at"):
        d[f] = _dt_to_str(d.get(f))
    return OAuth2AccessToken(**d)


def _init_pg(db) -> None:
    """Run the PostgreSQL migration if not already applied.

    The actual DDL is in admin.secrets_manager.PG_SCHEMA (shared migration 002).
    We import it lazily to avoid circular dependencies.
    """
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
        logger.info("Applied migration: 002_secrets_oauth2")


class OAuth2Database:
    """PostgreSQL-backed OAuth2 storage (oauth2 schema).

    Uses ORM models (ClientRecord, AuthCodeRecord, TokenRecord) for data access.
    Legacy _init_pg migration is preserved for backward compatibility on existing installs.
    """

    def __init__(self):
        from core.registry import get_database

        self._pg = get_database()
        if self._pg is None:
            raise RuntimeError(
                "OAuth2Database: PostgreSQL not available. "
                "Ensure DATABASE_URL is set and DB pool is initialized before creating OAuth2Database."
            )
        _init_pg(self._pg)
        logger.debug("OAuth2Database: using PostgreSQL backend")

    # ==================== CLIENT OPERATIONS ====================

    def create_client(
        self,
        name: str,
        client_type: str = "confidential",
        redirect_uris: str | None = None,
        allowed_scopes: str = "openid profile email",
        access_token_expiry: int = 3600,
        refresh_token_expiry: int = 2592000,
        require_pkce: bool = True,
        mcp_permissions: list[str] | None = None,
        description: str | None = None,
        active: bool = True,
        agent_name: str | None = None,
    ) -> tuple[OAuth2Client, str | None]:
        """Create a new OAuth2 client.

        Returns (client, plain_secret) where plain_secret is only set
        for confidential clients and must be shown to the user once.
        """
        client_id = secrets.token_urlsafe(24)
        plain_secret = None
        secret_hash = None

        if client_type == "confidential":
            plain_secret = secrets.token_urlsafe(32)
            secret_hash = hashlib.sha256(plain_secret.encode()).hexdigest()

        mcp_json = json.dumps(mcp_permissions) if mcp_permissions else None

        row = ClientRecord.create_sync(
            name=name,
            client_id=client_id,
            client_secret_hash=secret_hash,
            client_type=client_type,
            redirect_uris=redirect_uris,
            allowed_scopes=allowed_scopes,
            access_token_expiry=access_token_expiry,
            refresh_token_expiry=refresh_token_expiry,
            require_pkce=require_pkce,
            mcp_permissions=mcp_json,
            active=active,
            description=description,
            agent_name=agent_name,
        )
        return _row_to_client(row), plain_secret

    def get_client(self, client_id: str) -> OAuth2Client | None:
        """Find client by client_id string."""
        results = ClientRecord.search_sync(
            [("client_id", "=", client_id), ("active", "=", True)],
            limit=1,
        )
        return _row_to_client(results[0]) if results else None

    def get_client_by_id(self, pk: int) -> OAuth2Client | None:
        """Find client by primary key."""
        results = ClientRecord.search_sync([("id", "=", pk)], limit=1)
        return _row_to_client(results[0]) if results else None

    def get_by_agent_name(self, agent_name: str) -> OAuth2Client | None:
        """Find active client by agent_name."""
        results = ClientRecord.search_sync(
            [("agent_name", "=", agent_name), ("active", "=", True)],
            limit=1,
        )
        return _row_to_client(results[0]) if results else None

    def list_clients(self, include_inactive: bool = False) -> list[OAuth2Client]:
        domain = [] if include_inactive else [("active", "=", True)]
        rows = ClientRecord.search_sync(domain, order="created_at DESC")
        return [_row_to_client(r) for r in rows]

    def update_client(self, pk: int, **kwargs) -> bool:
        """Update client fields. Returns True if updated."""
        allowed_fields = {
            "name",
            "redirect_uris",
            "allowed_scopes",
            "access_token_expiry",
            "refresh_token_expiry",
            "require_pkce",
            "mcp_permissions",
            "active",
            "description",
            "agent_name",
        }
        updates = {k: v for k, v in kwargs.items() if k in allowed_fields}
        if not updates:
            return False

        if "mcp_permissions" in updates and isinstance(
            updates["mcp_permissions"], list
        ):
            updates["mcp_permissions"] = json.dumps(updates["mcp_permissions"])

        return ClientRecord.write_sync(pk, **updates) is not None

    def deactivate_client(self, pk: int) -> bool:
        return self.update_client(pk, active=False)

    def regenerate_secret(self, pk: int) -> str | None:
        """Regenerate client secret. Returns new plain secret."""
        client = self.get_client_by_id(pk)
        if not client or client.client_type != "confidential":
            return None

        plain_secret = secrets.token_urlsafe(32)
        secret_hash = hashlib.sha256(plain_secret.encode()).hexdigest()

        ClientRecord.write_sync(pk, client_secret_hash=secret_hash)
        return plain_secret

    # ==================== AUTHORIZATION CODE OPERATIONS ====================

    def create_authorization_code(
        self,
        client_pk: int,
        user_identity: str,
        redirect_uri: str,
        scope: str | None = None,
        code_challenge: str | None = None,
        code_challenge_method: str = "S256",
        state: str | None = None,
    ) -> OAuth2AuthorizationCode:
        """Create a new authorization code (valid for 10 minutes)."""
        code = secrets.token_urlsafe(32)
        expires_at = _utcnow() + timedelta(minutes=10)

        row = AuthCodeRecord.create_sync(
            code=code,
            client_id=client_pk,
            user_identity=user_identity,
            redirect_uri=redirect_uri,
            scope=scope,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            state=state,
            expires_at=expires_at,
        )
        return _row_to_auth_code(row)

    def find_authorization_code(self, code: str) -> OAuth2AuthorizationCode | None:
        results = AuthCodeRecord.search_sync([("code", "=", code)], limit=1)
        return _row_to_auth_code(results[0]) if results else None

    def mark_code_used_atomic(self, code_id: int) -> bool:
        """Atomically mark code as used. Returns True if successful."""
        updated = AuthCodeRecord.write_multi_sync(
            [("id", "=", code_id), ("used", "=", False)],
            used=True,
        )
        return updated > 0

    # ==================== ACCESS TOKEN OPERATIONS ====================

    def create_access_token(
        self,
        client_pk: int,
        user_identity: str | None = None,
        scope: str | None = None,
        access_expiry: int = 3600,
        refresh_expiry: int = 2592000,
        ip_address: str | None = None,
        user_agent: str | None = None,
        include_refresh: bool = True,
    ) -> OAuth2AccessToken:
        """Create a new access token with optional refresh token."""
        now = _utcnow()
        token = secrets.token_urlsafe(32)
        expires_at = now + timedelta(seconds=access_expiry)

        refresh_token = None
        refresh_expires_at = None
        if include_refresh:
            refresh_token = secrets.token_urlsafe(32)
            refresh_expires_at = now + timedelta(seconds=refresh_expiry)

        row = TokenRecord.create_sync(
            token=token,
            client_id=client_pk,
            user_identity=user_identity,
            scope=scope,
            expires_at=expires_at,
            refresh_token=refresh_token,
            refresh_expires_at=refresh_expires_at,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return _row_to_token(row)

    def find_token(self, token_string: str) -> OAuth2AccessToken | None:
        """Find access token by token string (timing-safe)."""
        results = TokenRecord.search_sync(
            [("token", "=", token_string), ("revoked", "=", False)],
            limit=1,
        )
        if not results:
            secrets.compare_digest(token_string, "x" * len(token_string))
            return None
        token_obj = _row_to_token(results[0])
        if not hmac.compare_digest(token_obj.token, token_string):
            return None
        return token_obj

    def find_by_refresh_token(self, refresh_token: str) -> OAuth2AccessToken | None:
        """Find token by refresh token string (timing-safe)."""
        results = TokenRecord.search_sync(
            [("refresh_token", "=", refresh_token), ("revoked", "=", False)],
            limit=1,
        )
        if not results:
            secrets.compare_digest(refresh_token, "x" * len(refresh_token))
            return None
        token_obj = _row_to_token(results[0])
        if not token_obj.refresh_token or not hmac.compare_digest(
            token_obj.refresh_token, refresh_token
        ):
            return None
        return token_obj

    def validate_token(
        self, token_string: str
    ) -> tuple[OAuth2AccessToken | None, OAuth2Client | None]:
        """Validate a bearer token. Returns (token, client) or (None, None)."""
        token_obj = self.find_token(token_string)
        if not token_obj or not token_obj.is_valid():
            return None, None

        client = self.get_client_by_id(token_obj.client_id)
        if not client or not client.active:
            return None, None

        # Update last used (best effort)
        try:
            TokenRecord.write_sync(token_obj.id, last_used_at=_utcnow())
        except Exception:
            pass

        return token_obj, client

    def revoke_token(self, token_id: int) -> bool:
        return TokenRecord.write_sync(token_id, revoked=True) is not None

    def revoke_by_token_string(self, token_string: str) -> bool:
        """Revoke by access token or refresh token string."""
        # OR condition requires raw SQL
        updated = TokenRecord.raw_execute_sync(
            "UPDATE {table} SET revoked = TRUE "
            "WHERE (token = %s OR refresh_token = %s) AND revoked = FALSE",
            (token_string, token_string),
        )
        return updated > 0

    def update_last_used(self, token_id: int, ip_address: str | None = None):
        """Update last used timestamp (best effort)."""
        try:
            values = {"last_used_at": _utcnow()}
            if ip_address:
                values["ip_address"] = ip_address
            TokenRecord.write_sync(token_id, **values)
        except Exception:
            pass

    def list_tokens_for_client(self, client_pk: int) -> list[OAuth2AccessToken]:
        rows = TokenRecord.search_sync(
            [("client_id", "=", client_pk), ("revoked", "=", False)],
            order="created_at DESC",
        )
        return [_row_to_token(r) for r in rows]

    # ==================== CLEANUP ====================

    def cleanup_expired(self) -> dict[str, int]:
        """Remove expired tokens and codes. Returns counts."""
        now = _utcnow()
        cutoff_24h = now - timedelta(hours=24)
        cutoff_48h = now - timedelta(hours=48)
        cutoff_1h = now - timedelta(hours=1)

        revoked_deleted = TokenRecord.delete_multi_sync(
            [("expires_at", "<", cutoff_24h), ("revoked", "=", True)]
        )
        expired_deleted = TokenRecord.delete_multi_sync(
            [("expires_at", "<", cutoff_48h)]
        )
        codes_deleted = AuthCodeRecord.delete_multi_sync(
            [("expires_at", "<", cutoff_1h)]
        )

        counts = {
            "revoked_tokens": revoked_deleted,
            "expired_tokens": expired_deleted,
            "expired_codes": codes_deleted,
        }
        if any(v > 0 for v in counts.values()):
            logger.info(f"OAuth2 cleanup: {counts}")
        return counts

    # ==================== STATS ====================

    def get_stats(self) -> dict:
        clients = ClientRecord.count_sync([("active", "=", True)])
        tokens = TokenRecord.count_sync([("revoked", "=", False)])
        active_tokens = TokenRecord.count_sync(
            [("revoked", "=", False), ("expires_at", ">", _utcnow())]
        )
        return {
            "clients": clients,
            "total_tokens": tokens,
            "active_tokens": active_tokens,
        }

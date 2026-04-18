"""HMAC-SHA256 signing for capability URLs.

Uses the shared INTERNAL_API_SECRET env var so either container can sign
and verify. Token is the first 32 hex chars of HMAC-SHA256(secret, uuid).
Deterministic: same uuid -> same token. Revocation via revoked_at column.
"""

from __future__ import annotations

import hmac as _hmac
import os
from hashlib import sha256


def _get_secret() -> bytes:
    secret = os.environ.get("INTERNAL_API_SECRET")
    if not secret:
        raise RuntimeError(
            "INTERNAL_API_SECRET is not set — cannot sign or verify artifact tokens"
        )
    return secret.encode()


def sign_uuid(uuid_str: str) -> str:
    """Return 32-hex-char HMAC token for the given UUID string."""
    return _hmac.new(_get_secret(), uuid_str.encode(), sha256).hexdigest()[:32]


def verify_signature(uuid_str: str, token: str) -> bool:
    """Verify a capability token in constant time."""
    try:
        expected = sign_uuid(uuid_str)
    except RuntimeError:
        return False
    return _hmac.compare_digest(expected, token)


def build_capability_url(uuid_str: str, share_token: str | None = None) -> str:
    """Build a full /artifacts/<uuid>?t=<hmac>[&s=<share>] URL.

    ``share_token`` is the per-artifact random token stored in the DB — when
    included, anyone with this URL can view without logging in. Revoking the
    column on the row kills the link.
    """
    base = os.environ.get("GRIDBEAR_BASE_URL", "").rstrip("/")
    token = sign_uuid(uuid_str)
    url = f"{base}/artifacts/{uuid_str}?t={token}"
    if share_token:
        url += f"&s={share_token}"
    return url

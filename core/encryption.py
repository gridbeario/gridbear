"""Application-layer encryption for user data at rest.

Uses AES-256-GCM with the same master key as SecretsManager (config/secrets.key).
Storage format: base64(nonce_12bytes + ciphertext) as a single TEXT string.
"""

import base64
import hashlib
import logging
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_logger = logging.getLogger(__name__)

# Key search paths — same as ui/secrets_manager.py (no import dependency on ui/)
_BASE_DIR = Path(__file__).resolve().parent.parent
# Candidate key file locations, in preference order. The dedicated
# `config/secrets.key` wins; the home-dir SSH paths stay for operators
# who were already relying on that fallback. The previous hard-coded
# `/root/.ssh/*` entries were dropped — the container should never run
# as root in the first place, and if it does, silently consuming a
# root-owned SSH key as the vault master key is a surprise bomb
# (different host → different key → vault unreadable).
KEY_PATHS = [
    _BASE_DIR / "config" / "secrets.key",
    Path.home() / ".ssh" / "id_ed25519",
    Path.home() / ".ssh" / "id_rsa",
    Path("/app/config/secrets.key"),
]
MASTER_KEY_ENV = "GRIDBEAR_MASTER_KEY"

_cached_key: bytes | None = None
_cached_key_source: str | None = None


def _find_key_file() -> Path | None:
    """Return the first path from KEY_PATHS that is BOTH existent AND readable.

    Using `.exists()` alone hits a trap in containerised deploys: paths
    like `/root/.ssh/id_ed25519` may exist (owned by root) but the
    non-root container process can't read them. Picking such a path
    here would crash every caller with `PermissionError [Errno 13]`.
    """
    for p in KEY_PATHS:
        try:
            if p.exists() and os.access(p, os.R_OK):
                return p
        except OSError:
            # stat() itself can fail on exotic mount errors — skip the path.
            continue
    return None


def _get_key() -> bytes:
    """Derive a 32-byte AES key from the master key source.

    This is the single source of truth for the vault master key —
    ``ui.secrets_manager.SecretsManager`` delegates to it instead of
    keeping a parallel cache. Without that, the eager purge in
    ``ensure_master_key_loaded()`` would race the SecretsManager's
    first read in env-var-only deployments and disable the vault.
    See gridbeario/gridbear#147.
    """
    global _cached_key, _cached_key_source
    if _cached_key is not None:
        return _cached_key

    key_file = _find_key_file()
    if key_file:
        try:
            raw = key_file.read_bytes()
            _cached_key = hashlib.sha256(raw).digest()
            _cached_key_source = str(key_file)
            return _cached_key
        except PermissionError as exc:
            # Belt-and-suspenders: _find_key_file already filters unreadable
            # paths, but a race (permissions dropped between check and read)
            # is possible. Fall through to the env var instead of crashing.
            _logger.warning(
                "core.encryption: candidate key file %s unreadable (%s); "
                "falling through to GRIDBEAR_MASTER_KEY",
                key_file,
                exc,
            )

    env_val = os.environ.get(MASTER_KEY_ENV)
    if env_val:
        _cached_key = hashlib.sha256(env_val.encode()).digest()
        _cached_key_source = f"env:{MASTER_KEY_ENV}"
        # Purge the env var once cached so child processes (plugin
        # subprocesses, Claude CLI, Playwright, MCP stdio servers)
        # can't `os.environ[MASTER_KEY_ENV]` to pull the plaintext
        # master key out of /proc/<pid>/environ.
        os.environ.pop(MASTER_KEY_ENV, None)
        return _cached_key

    raise RuntimeError(
        "No encryption key found. Create config/secrets.key or set GRIDBEAR_MASTER_KEY."
    )


def get_key_source() -> str | None:
    """Return a short string describing where the cached key came from.

    Returns ``None`` if the key has not been derived yet (e.g. before
    ``_get_key()`` or ``ensure_master_key_loaded()`` is called).
    The value is either an absolute file path or ``env:GRIDBEAR_MASTER_KEY``.
    """
    return _cached_key_source


def ensure_master_key_loaded() -> None:
    """Force master-key derivation and purge ``GRIDBEAR_MASTER_KEY`` from env.

    Call eagerly during app startup, before any plugin subprocess is
    spawned. Two guarantees after the call returns:

    1. The cached key is derived (from file if present, else env var),
       so later encrypt/decrypt calls are purely file-free.
    2. ``GRIDBEAR_MASTER_KEY`` is removed from ``os.environ`` regardless
       of which source actually fed the cache, so any subprocess
       launched afterwards (Claude CLI, Playwright, MCP stdio servers)
       cannot read the plaintext master key out of
       ``/proc/<pid>/environ``.

    Safe to call multiple times — ``_get_key`` is cached.
    """
    _get_key()
    os.environ.pop(MASTER_KEY_ENV, None)


def encrypt(plaintext: str) -> str:
    """Encrypt a string using AES-256-GCM.

    Returns base64(nonce_12bytes + ciphertext).
    """
    key = _get_key()
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt(encrypted: str) -> str:
    """Decrypt a base64(nonce + ciphertext) string."""
    key = _get_key()
    raw = base64.b64decode(encrypted)
    nonce = raw[:12]
    ct = raw[12:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None).decode("utf-8")


def is_encrypted(value: str) -> bool:
    """Heuristic check: is the value an encrypted blob?

    Tries base64-decode and checks that the decoded length is > 28 bytes
    (12 nonce + 16 GCM tag minimum). Used by migration scripts and the
    Encrypted field to avoid double-encrypting or decrypting plaintext.
    """
    if not value or len(value) < 40:
        return False
    try:
        raw = base64.b64decode(value, validate=True)
        # AES-GCM: 12 nonce + at least 16 tag + 1 byte ciphertext = 29 min
        return len(raw) >= 29
    except Exception:
        return False

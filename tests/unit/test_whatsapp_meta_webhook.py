from __future__ import annotations

import hashlib
import hmac

from plugins.whatsapp_api.api.routes import _verify_signature

APP_SECRET = "test_secret_key_12345"


def _make_signature(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class TestVerifySignature:
    def test_valid_signature(self):
        body = b'{"entry": []}'
        sig = _make_signature(body, APP_SECRET)
        assert _verify_signature(body, sig, APP_SECRET) is True

    def test_invalid_signature(self):
        body = b'{"entry": []}'
        sig = _make_signature(body, "wrong_secret")
        assert _verify_signature(body, sig, APP_SECRET) is False

    def test_empty_signature(self):
        body = b'{"entry": []}'
        assert _verify_signature(body, "", APP_SECRET) is False

    def test_missing_prefix(self):
        body = b'{"entry": []}'
        digest = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
        assert _verify_signature(body, digest, APP_SECRET) is False

    def test_none_like_signature(self):
        body = b'{"entry": []}'
        assert _verify_signature(body, None, APP_SECRET) is False

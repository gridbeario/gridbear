"""Tests for HMAC signing helpers."""

from uuid import uuid4


def test_sign_roundtrip(hmac_secret):
    from plugins.artifacts.signing import sign_uuid, verify_signature

    uid = str(uuid4())
    token = sign_uuid(uid)
    assert verify_signature(uid, token) is True


def test_verify_rejects_tampered_uuid(hmac_secret):
    from plugins.artifacts.signing import sign_uuid, verify_signature

    uid = str(uuid4())
    token = sign_uuid(uid)
    assert verify_signature(str(uuid4()), token) is False


def test_verify_rejects_tampered_token(hmac_secret):
    from plugins.artifacts.signing import sign_uuid, verify_signature

    uid = str(uuid4())
    token = sign_uuid(uid)
    mangled = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert verify_signature(uid, mangled) is False


def test_token_is_32_hex_chars(hmac_secret):
    from plugins.artifacts.signing import sign_uuid

    token = sign_uuid(str(uuid4()))
    assert len(token) == 32
    assert all(c in "0123456789abcdef" for c in token)


def test_build_capability_url(hmac_secret, monkeypatch):
    from plugins.artifacts.signing import build_capability_url

    monkeypatch.setenv("GRIDBEAR_BASE_URL", "https://gb.example.com/")
    uid = str(uuid4())
    url = build_capability_url(uid)
    assert url.startswith(f"https://gb.example.com/artifacts/{uid}?t=")


def test_sign_missing_secret_raises(monkeypatch):
    from plugins.artifacts.signing import sign_uuid

    monkeypatch.delenv("INTERNAL_API_SECRET", raising=False)
    raised = False
    try:
        sign_uuid("deadbeef")
    except RuntimeError as e:
        raised = True
        assert "INTERNAL_API_SECRET" in str(e)
    assert raised

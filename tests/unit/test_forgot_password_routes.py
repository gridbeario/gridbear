def test_password_reset_category_exists():
    from ui.rate_limit import RATE_LIMITS

    cfg = RATE_LIMITS["password_reset"]
    assert cfg.requests == 3
    assert cfg.window == 300


def test_check_rate_limit_key_trips_after_threshold():
    from ui.rate_limit import RateLimiter

    limiter = RateLimiter()
    key = "acct:someone@example.com"
    allowed = [limiter.is_allowed_key(key, "password_reset")[0] for _ in range(4)]
    assert allowed == [True, True, True, False]


def _client():
    # Route-logic test only: no CSRFMiddleware here. The POST is sessionless,
    # which the CSRF middleware skips by design (ui/csrf.py). In production the
    # GET creates a session + token and the template's csrf_field submits it,
    # same as /auth/setup-password (also not CSRF-exempt).
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.middleware.sessions import SessionMiddleware

    from ui.routes.auth import router

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-key")
    app.include_router(router, prefix="/auth")
    return TestClient(app)


def test_get_forgot_password_renders_form():
    client = _client()
    resp = client.get("/auth/forgot-password")
    assert resp.status_code == 200
    assert 'name="email"' in resp.text
    assert "csrf" in resp.text.lower()


def test_post_known_and_unknown_email_identical_response():
    from unittest.mock import patch

    client = _client()
    with patch("ui.routes.auth.request_password_reset") as mock_req:
        mock_req.return_value = None
        r_known = client.post(
            "/auth/forgot-password", data={"email": "davide.corio@dubhe.it"}
        )
        r_unknown = client.post(
            "/auth/forgot-password", data={"email": "nobody@example.com"}
        )
    assert r_known.status_code == r_unknown.status_code == 200
    assert r_known.text == r_unknown.text

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

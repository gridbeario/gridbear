"""Rate Limiting for GridBear Admin.

Simple IP-based rate limiting for login and sensitive endpoints.
Uses in-memory storage (resets on restart).
"""

import time
from collections import defaultdict
from dataclasses import dataclass
from functools import wraps

from fastapi import HTTPException, Request


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting."""

    requests: int  # Number of requests allowed
    window: int  # Time window in seconds


# Default rate limits per endpoint category
RATE_LIMITS = {
    "login": RateLimitConfig(requests=5, window=60),  # 5 attempts per minute
    "api": RateLimitConfig(requests=100, window=60),  # 100 requests per minute
    "default": RateLimitConfig(requests=60, window=60),  # 60 requests per minute
    "password_reset": RateLimitConfig(requests=3, window=300),  # 3 per 5 min
}


class RateLimiter:
    """In-memory rate limiter using sliding window."""

    def __init__(self):
        # {(ip, endpoint_category): [(timestamp, count), ...]}
        self._requests: dict[tuple[str, str], list[float]] = defaultdict(list)
        self._cleanup_interval = 300  # Cleanup every 5 minutes
        self._last_cleanup = time.time()

    def _cleanup_old_entries(self):
        """Remove entries older than the maximum window."""
        now = time.time()
        if now - self._last_cleanup < self._cleanup_interval:
            return

        max_window = max(cfg.window for cfg in RATE_LIMITS.values())
        cutoff = now - max_window

        keys_to_remove = []
        for key, timestamps in self._requests.items():
            self._requests[key] = [ts for ts in timestamps if ts > cutoff]
            if not self._requests[key]:
                keys_to_remove.append(key)

        for key in keys_to_remove:
            del self._requests[key]

        self._last_cleanup = now

    def is_allowed(self, ip: str, category: str = "default") -> tuple[bool, int]:
        """Check if request is allowed.

        Returns:
            Tuple of (is_allowed, retry_after_seconds)
        """
        self._cleanup_old_entries()

        config = RATE_LIMITS.get(category, RATE_LIMITS["default"])
        key = (ip, category)
        now = time.time()

        # Filter to requests within the window
        window_start = now - config.window
        self._requests[key] = [ts for ts in self._requests[key] if ts > window_start]

        current_count = len(self._requests[key])

        if current_count >= config.requests:
            # Rate limited
            oldest_in_window = min(self._requests[key]) if self._requests[key] else now
            retry_after = int(oldest_in_window + config.window - now) + 1
            return False, max(1, retry_after)

        # Allow request
        self._requests[key].append(now)
        return True, 0

    def is_allowed_key(self, key: str, category: str = "default") -> tuple[bool, int]:
        """Check if a request is allowed for an arbitrary key (e.g. an account).

        Alias for `is_allowed` kept separate for call-site clarity (accounts
        vs. IPs); the sliding-window logic is identical.
        """
        return self.is_allowed(key, category)

    def get_remaining(self, ip: str, category: str = "default") -> int:
        """Get remaining requests in current window."""
        config = RATE_LIMITS.get(category, RATE_LIMITS["default"])
        key = (ip, category)
        now = time.time()
        window_start = now - config.window

        current_count = len([ts for ts in self._requests[key] if ts > window_start])
        return max(0, config.requests - current_count)


# Global rate limiter instance
rate_limiter = RateLimiter()


def get_client_ip(request: Request) -> str:
    """Extract client IP from request, handling proxies."""
    # Check X-Forwarded-For header (for reverse proxy setups)
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        # Take the first IP (original client)
        return forwarded_for.split(",")[0].strip()

    # Check X-Real-IP header
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip

    # Fall back to direct connection
    if request.client:
        return request.client.host

    return "unknown"


async def check_rate_limit(request: Request, category: str = "default"):
    """Check rate limit for the current request.

    Raises HTTPException 429 if rate limited.
    """
    ip = get_client_ip(request)
    allowed, retry_after = rate_limiter.is_allowed(ip, category)

    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Too many requests. Please try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )


async def check_rate_limit_key(key: str, category: str = "default"):
    """Check rate limit for an arbitrary key (e.g. an account).

    Raises HTTPException 429 if rate limited.
    """
    allowed, retry_after = rate_limiter.is_allowed_key(key, category)

    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Too many requests. Please try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )


def rate_limit(category: str = "default"):
    """Decorator to apply rate limiting to a route.

    Usage:
        @app.post("/login")
        @rate_limit("login")
        async def login(request: Request):
            ...
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Extract request from args or kwargs
            request = None
            for arg in args:
                if isinstance(arg, Request):
                    request = arg
                    break
            if request is None:
                request = kwargs.get("request")

            if request:
                await check_rate_limit(request, category)

            return await func(*args, **kwargs)

        return wrapper

    return decorator

"""Session management for GridBear Admin.

Provides persistent database-backed sessions with:
- Secure 64-byte tokens
- HttpOnly, Secure, SameSite=Lax cookies
- Automatic expiration and cleanup
- Support for multiple sessions per user
"""

import os
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Request, Response


def _ensure_naive_dt(val: datetime) -> datetime:
    """Strip timezone from PG TIMESTAMPTZ for comparison with datetime.now()."""
    return val.replace(tzinfo=None)


from ui.auth.database import auth_db

SESSION_COOKIE_NAME = "gridbear_session_token"
# Inactivity timeout: the session expires after this much time without any
# authenticated request. Each validated request slides expires_at forward.
SESSION_INACTIVITY_HOURS = int(os.getenv("GRIDBEAR_SESSION_INACTIVITY_HOURS", "72"))
# Absolute hard cap from the moment of login — the session cannot be
# extended past this, even under continuous activity. Protects against a
# stolen cookie being refreshed indefinitely.
SESSION_ABSOLUTE_MAX_DAYS = int(os.getenv("GRIDBEAR_SESSION_MAX_DAYS", "30"))
SESSION_TOKEN_BYTES = 64


class SessionManager:
    """Manages persistent admin sessions with sliding expiration.

    Sessions expire after ``SESSION_INACTIVITY_HOURS`` of inactivity,
    but never live longer than ``SESSION_ABSOLUTE_MAX_DAYS`` from login
    regardless of activity.
    """

    def __init__(self):
        self.db = auth_db
        self.cookie_name = SESSION_COOKIE_NAME
        self.inactivity_window = timedelta(hours=SESSION_INACTIVITY_HOURS)
        self.absolute_max = timedelta(days=SESSION_ABSOLUTE_MAX_DAYS)

    def create_session(
        self,
        user_id: int,
        request: Request,
        response: Response,
    ) -> str:
        """Create a new session and set the cookie.

        Returns the session token.
        """
        token = secrets.token_hex(SESSION_TOKEN_BYTES)
        expires_at = datetime.now() + self.inactivity_window

        ip_address = self._get_client_ip(request)
        user_agent = request.headers.get("user-agent", "")[:500]

        self.db.create_session(
            session_token=token,
            user_id=user_id,
            expires_at=expires_at,
            ip_address=ip_address,
            user_agent=user_agent,
        )

        self._set_cookie(response, token)

        self.db.update_user(user_id, last_login=datetime.now().isoformat())

        return token

    def validate_session(self, request: Request) -> Optional[dict]:
        """Validate session from request cookies.

        Returns user dict if valid, None otherwise.
        """
        token = request.cookies.get(self.cookie_name)
        if not token:
            return None

        session = self.db.get_session(token)
        if not session:
            return None

        now = datetime.now()
        if _ensure_naive_dt(session["expires_at"]) < now:
            self.db.delete_session(token)
            return None

        # Absolute hard cap: expire sessions that have been alive longer
        # than SESSION_ABSOLUTE_MAX_DAYS regardless of activity.
        created_at = session.get("created_at")
        if created_at is not None:
            age_limit = _ensure_naive_dt(created_at) + self.absolute_max
            if now > age_limit:
                self.db.delete_session(token)
                return None
        else:
            age_limit = now + self.inactivity_window

        user = self.db.get_user_by_id(session["user_id"])
        if not user or not user.get("is_active"):
            return None

        # Slide expires_at forward on activity, capped at the absolute max.
        new_expires_at = min(now + self.inactivity_window, age_limit)
        self.db.update_session_activity(token, new_expires_at=new_expires_at)

        return user

    def destroy_session(self, request: Request, response: Response) -> bool:
        """Destroy the current session and clear cookie."""
        token = request.cookies.get(self.cookie_name)
        if token:
            self.db.delete_session(token)

        self._clear_cookie(response)
        return True

    def destroy_all_user_sessions(
        self,
        user_id: int,
        except_current: bool = False,
        request: Optional[Request] = None,
    ) -> int:
        """Destroy all sessions for a user.

        Args:
            user_id: The user ID
            except_current: If True, keep the current session
            request: Request object (required if except_current is True)

        Returns:
            Number of sessions deleted
        """
        except_token = None
        if except_current and request:
            except_token = request.cookies.get(self.cookie_name)

        return self.db.delete_user_sessions(user_id, except_token=except_token)

    def get_user_sessions(self, user_id: int) -> list[dict]:
        """Get all active sessions for a user."""
        sessions = self.db.get_user_sessions(user_id)
        now = datetime.now()
        return [s for s in sessions if _ensure_naive_dt(s["expires_at"]) > now]

    def cleanup_expired(self) -> int:
        """Remove all expired sessions from database."""
        return self.db.cleanup_expired_sessions()

    def _get_client_ip(self, request: Request) -> str:
        """Extract client IP from request, handling proxies."""
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        if request.client:
            return request.client.host
        return "unknown"

    @staticmethod
    def _is_https() -> bool:
        return os.getenv("ADMIN_HTTPS_ONLY", "false").lower() == "true"

    def _set_cookie(self, response: Response, token: str) -> None:
        """Set the session cookie with security flags.

        Cookie lifetime is pinned to the absolute max so the browser
        keeps sending it as long as the server considers the session
        valid. Server-side inactivity enforcement happens in
        ``validate_session`` (the DB row's ``expires_at`` shrinks the
        effective window even though the cookie lives longer).
        """
        https = self._is_https()
        max_age = int(self.absolute_max.total_seconds())
        response.set_cookie(
            key=self.cookie_name,
            value=token,
            max_age=max_age,
            httponly=True,
            secure=https,
            samesite="strict" if https else "lax",
            path="/",
        )

    def _clear_cookie(self, response: Response) -> None:
        """Clear the session cookie."""
        https = self._is_https()
        response.delete_cookie(
            key=self.cookie_name,
            path="/",
            httponly=True,
            secure=https,
            samesite="strict" if https else "lax",
        )


session_manager = SessionManager()


def get_current_user(request: Request) -> Optional[dict]:
    """Get the current logged-in user from request.

    Returns user dict if authenticated, None otherwise.
    """
    return session_manager.validate_session(request)


def get_session_token(request: Request) -> Optional[str]:
    """Get the current session token from request cookies."""
    return request.cookies.get(SESSION_COOKIE_NAME)

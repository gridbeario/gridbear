"""Self-service password reset: lookup, token issuance, email delivery.

request_password_reset() always returns None and never reveals whether the
address matched an eligible account — non-disclosure is a property of the
signature.
"""

import logging

from starlette.concurrency import run_in_threadpool

from core.models.user import User
from ui.auth.invite import RESET_TTL_HOURS, generate_token, send_invite_email

logger = logging.getLogger(__name__)

RESET_SUBJECT = "GridBear — Reset your password"


def _lookup_and_issue_token(
    email: str, base_url: str
) -> tuple[dict | None, str | None]:
    """Sync half: exact case-insensitive lookup, eligibility, token.

    Returns (user_dict, token_url) or (None, None). Runs bcrypt (in
    generate_token) and sync DB calls, so it MUST run off the event loop.
    """
    rows = User.raw_search_sync(
        "SELECT * FROM {table} WHERE lower(email) = %s",
        (email.strip().lower(),),
    )
    if not rows:
        logger.warning("reset_requested: no match for submitted address")
        return None, None

    user = rows[0]
    if not user.get("is_active") or not user.get("password_hash"):
        logger.warning(
            "reset_requested: user_id=%s ineligible (active=%s has_pw=%s)",
            user.get("id"),
            user.get("is_active"),
            bool(user.get("password_hash")),
        )
        return None, None

    raw_token = generate_token(user["id"], purpose="reset")
    token_url = f"{base_url.rstrip('/')}/auth/setup-password?token={raw_token}"
    logger.warning("reset_requested: user_id=%s token issued", user["id"])
    return user, token_url


async def request_password_reset(email: str, base_url: str) -> None:
    """Issue and email a password-reset link if the address is eligible.

    Always returns None regardless of outcome.
    """
    try:
        user, token_url = await run_in_threadpool(
            _lookup_and_issue_token, email, base_url
        )
    except Exception as err:
        logger.warning("reset_requested: unexpected error: %s", err)
        return None

    if user is None:
        return None

    try:
        sent = await send_invite_email(
            user, token_url, subject=RESET_SUBJECT, ttl_hours=RESET_TTL_HOURS
        )
        logger.warning(
            "reset_email_%s: user_id=%s", "sent" if sent else "failed", user["id"]
        )
    except Exception as err:
        logger.warning("reset_email_failed: user_id=%s err=%s", user["id"], err)

    return None

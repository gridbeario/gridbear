"""Token-based password setup for user invites and password resets.

Generates cryptographically random tokens, stores bcrypt hashes,
and validates/consumes tokens for the password setup flow.
"""

import logging
import secrets
from datetime import datetime, timedelta

import bcrypt

from core.config_models import PasswordToken
from core.mcp_gateway.server import get_client_manager
from core.models.user import User
from core.system_config import SystemConfig

logger = logging.getLogger(__name__)

INVITE_TTL_HOURS = 48
RESET_TTL_HOURS = 1


def generate_token(user_id: int, purpose: str, ttl_hours: int | None = None) -> str:
    """Generate a password setup token for a user.

    Creates a 32-byte random token, stores its bcrypt hash in the DB,
    and returns the raw token (shown once, never stored).

    Args:
        user_id: Target user ID.
        purpose: "invite" or "reset".
        ttl_hours: Override TTL (defaults to 48h for invite, 1h for reset).

    Returns:
        Raw token string (URL-safe base64).
    """
    if purpose not in ("invite", "reset"):
        raise ValueError(f"Invalid purpose: {purpose}")

    if ttl_hours is None:
        ttl_hours = INVITE_TTL_HOURS if purpose == "invite" else RESET_TTL_HOURS

    raw_token = secrets.token_urlsafe(32)
    token_hash = bcrypt.hashpw(raw_token.encode(), bcrypt.gensalt(rounds=12)).decode()
    expires_at = datetime.now() + timedelta(hours=ttl_hours)

    # Invalidate any existing unused tokens for same user+purpose
    existing = PasswordToken.search_sync(
        [("user_id", "=", user_id), ("purpose", "=", purpose), ("used_at", "=", None)]
    )
    for old in existing:
        PasswordToken.write_sync(old["id"], used_at=datetime.now())

    PasswordToken.create_sync(
        user_id=user_id,
        token_hash=token_hash,
        purpose=purpose,
        expires_at=expires_at,
    )

    return raw_token


def validate_token(raw_token: str) -> dict | None:
    """Validate a raw token and return the associated user, or None.

    Checks: token matches a hash, not expired, not already used.

    Returns:
        User dict if valid, None otherwise.
    """
    # Find all unused, non-expired tokens
    candidates = PasswordToken.raw_search_sync(
        "SELECT * FROM app.password_tokens "
        "WHERE used_at IS NULL AND expires_at > NOW() "
        "ORDER BY created_at DESC"
    )

    for token_row in candidates:
        try:
            if bcrypt.checkpw(raw_token.encode(), token_row["token_hash"].encode()):
                user = User.get_sync(id=token_row["user_id"])
                if user:
                    return {
                        "token_id": token_row["id"],
                        "user_id": token_row["user_id"],
                        "purpose": token_row["purpose"],
                        "unified_id": user["username"],
                        "display_name": user.get("display_name"),
                    }
        except (ValueError, TypeError):
            continue

    return None


def consume_token(token_id: int) -> bool:
    """Mark a token as used. Returns True if successful."""
    token = PasswordToken.get_sync(id=token_id)
    if not token or token.get("used_at"):
        return False
    PasswordToken.write_sync(token_id, used_at=datetime.now())
    return True


def get_agent_email_config(agent_id: str) -> dict | None:
    """Read the email section from an agent's DB config.

    Returns the email dict (account, sender_name, signature, etc.)
    or None if the agent has no email config.
    """
    try:
        from core.models.agent_config import AgentConfigRecord

        record = AgentConfigRecord.get_sync(id=agent_id)
        if not record:
            return None
        email_cfg = record.get("email") or {}
        if not email_cfg or not email_cfg.get("account"):
            return None
        return email_cfg
    except Exception as err:
        logger.warning("Failed to read email config for agent %s: %s", agent_id, err)
        return None


async def send_invite_email(
    user: dict,
    token_url: str,
    subject: str | None = None,
    ttl_hours: int | None = None,
) -> bool:
    """Try to send an invite email via the system agent's Gmail MCP tool.

    Reads the system_agent setting, looks up its email config (account,
    sender_name, signature), and calls the correct namespaced Gmail tool.

    Args:
        user: User dict with email, display_name, username.
        token_url: Setup/reset link to embed in the email body.
        subject: Email subject override (defaults to the invite subject).
        ttl_hours: Expiry TTL shown in the body (defaults to INVITE_TTL_HOURS).

    Returns True if email was sent, False if delivery failed or unavailable.
    """
    email = user.get("email")
    if not email:
        logger.info(
            "No email address for user %s, skipping email delivery",
            user.get("username"),
        )
        return False

    if subject is None:
        subject = "GridBear — Set up your password"
    if ttl_hours is None:
        ttl_hours = INVITE_TTL_HOURS

    try:
        from core.mcp_gateway.client_manager import _sanitize_name

        client_manager = get_client_manager()
        if not client_manager:
            logger.info("MCP client manager not available, skipping email delivery")
            return False

        system_agent = SystemConfig.get_param_sync("system_agent")
        if not system_agent:
            logger.warning("No system_agent configured, cannot send invite email")
            return False

        email_cfg = get_agent_email_config(system_agent)
        if not email_cfg:
            logger.warning(
                "System agent '%s' has no email config, cannot send invite email",
                system_agent,
            )
            return False

        account = email_cfg["account"]
        sender_name = email_cfg.get("sender_name", "")
        signature = email_cfg.get("signature", "")

        # Derive the namespaced tool name: gmail-hello@dubhe.it → gmail_hello_dubhe_it__send_email
        server_name = f"gmail-{account}"
        sanitized = _sanitize_name(server_name)
        tool_name = f"{sanitized}__send_email"

        display_name = user.get("display_name") or user.get("username", "")
        hours_label = "1 hour" if ttl_hours == 1 else f"{ttl_hours} hours"
        body = (
            f"Hi {display_name},\n\n"
            f"You've been invited to set up your GridBear portal password.\n\n"
            f"Click the link below to create your password:\n"
            f"{token_url}\n\n"
            f"This link expires in {hours_label}.\n\n"
            f"If you didn't expect this email, you can safely ignore it."
        )
        if signature:
            body += f"\n\n{signature}"

        tool_args = {"to": email, "subject": subject, "body": body}
        if sender_name:
            tool_args["from_name"] = sender_name

        result = await client_manager.call_tool(tool_name, tool_args)
        logger.info("Invite email sent to %s via %s: %s", email, tool_name, result)
        return True
    except Exception as err:
        logger.warning("Failed to send invite email to %s: %s", email, err)
        return False

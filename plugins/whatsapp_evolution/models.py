"""WhatsApp plugin ORM models.

Declarative models for the WhatsApp multi-tenant data layer:
- UserInstance: per-user WhatsApp instance linked to an agent
- AuthorizedNumber: phone numbers allowed to message through an instance
- WakeWord: auto-response keywords for unauthorized senders
"""

from __future__ import annotations

from core.orm import Model, fields


class UserInstance(Model):
    """A user's WhatsApp instance for a specific agent."""

    _schema = "whatsapp"
    _name = "user_instances"

    unified_id = fields.Text(required=True)
    agent_name = fields.Text(required=True)
    instance_name = fields.Text(required=True, unique=True)
    silent_reject = fields.Boolean(default=False)
    reject_message = fields.Text(default="")
    created_at = fields.DateTime(auto_now_add=True)

    _constraints = [
        ("uq_user_instances_unified_agent", "UNIQUE (unified_id, agent_name)"),
    ]

    @classmethod
    async def check_phone_auth(cls, instance_name: str, phone: str) -> dict:
        """Check authorization and return reject settings.

        Single logical operation for the hot path.
        Returns {"authorized": bool, "silent_reject": bool, "reject_message": str}
        """
        inst = await cls.get(instance_name=instance_name)
        if not inst:
            return {"authorized": False, "silent_reject": True, "reject_message": ""}

        authorized = await AuthorizedNumber.exists(instance_id=inst["id"], phone=phone)
        return {
            "authorized": authorized,
            "silent_reject": inst["silent_reject"],
            "reject_message": inst["reject_message"] or "",
        }


class AuthorizedNumber(Model):
    """A phone number authorized to use a WhatsApp instance."""

    _schema = "whatsapp"
    _name = "authorized_numbers"

    instance_id = fields.ForeignKey(UserInstance, on_delete="CASCADE")
    phone = fields.Text(required=True)
    label = fields.Text(default="")
    created_at = fields.DateTime(auto_now_add=True)

    _constraints = [
        ("uq_authorized_numbers_instance_phone", "UNIQUE (instance_id, phone)"),
    ]

    @classmethod
    async def add_number(
        cls, instance_id: int, phone: str, label: str = ""
    ) -> dict | None:
        """Add an authorized number. Returns None on duplicate."""
        try:
            return await cls.create(instance_id=instance_id, phone=phone, label=label)
        except Exception:
            return None


class WakeWord(Model):
    """Auto-response keyword for unauthorized senders."""

    _schema = "whatsapp"
    _name = "wake_words"

    instance_id = fields.ForeignKey(UserInstance, on_delete="CASCADE")
    keyword = fields.Text(required=True)
    response = fields.Text(required=True)
    created_at = fields.DateTime(auto_now_add=True)

    _constraints = [
        ("uq_wake_words_instance_keyword", "UNIQUE (instance_id, keyword)"),
    ]

    @classmethod
    async def add_word(
        cls, instance_id: int, keyword: str, response: str
    ) -> dict | None:
        """Add a wake word. Returns None on duplicate."""
        try:
            return await cls.create(
                instance_id=instance_id,
                keyword=keyword.lower().strip(),
                response=response.strip(),
            )
        except Exception:
            return None

    @classmethod
    async def check_wake_words(cls, instance_name: str, text: str) -> str | None:
        """Check if text contains any wake word for the instance.

        Returns the response string if a match is found, None otherwise.
        """
        if not text:
            return None
        text_lower = text.lower()
        inst = await UserInstance.get(instance_name=instance_name)
        if not inst:
            return None
        words = await cls.search([("instance_id", "=", inst["id"])], order="keyword")
        for w in words:
            if w["keyword"] in text_lower:
                return w["response"]
        return None

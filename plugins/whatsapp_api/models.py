"""ORM models for whatsapp_api plugin."""

from core.orm import Model, fields


class AuthorizedNumber(Model):
    """Phone numbers authorized to interact with an agent via WhatsApp."""

    _schema = "whatsapp_api"
    _name = "authorized_numbers"

    phone_number_id = fields.Text(required=True, index=True)
    phone = fields.Text(required=True)
    label = fields.Text()

    _constraints = [
        ("uq_wa_api_auth", "UNIQUE (phone_number_id, phone)"),
    ]

    @classmethod
    def get_authorized(cls, phone_number_id: str) -> list[str]:
        """Get list of authorized phone numbers. Empty list = open mode."""
        rows = cls.search_sync([("phone_number_id", "=", phone_number_id)])
        return [r["phone"] for r in rows]

    @classmethod
    def get_label(cls, phone_number_id: str, phone: str) -> str | None:
        """Get label for a phone number, or None."""
        row = cls.get_sync(phone_number_id=phone_number_id, phone=phone)
        return row["label"] if row else None

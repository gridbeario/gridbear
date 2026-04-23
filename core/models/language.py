"""ORM model for i18n languages (replaces dormant SQL migration 009)."""

from core.orm import Model, fields


class Language(Model):
    _schema = "i18n"
    _name = "languages"
    _primary_key = "code"

    code = fields.Text(required=True)
    name = fields.Text(required=True)
    active = fields.Boolean(default=False)
    direction = fields.Text(default="ltr")
    date_format = fields.Text(default="%Y-%m-%d")
    is_default = fields.Boolean(default=False)
    created_at = fields.DateTime(auto_now_add=True)

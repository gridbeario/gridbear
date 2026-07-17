"""User model — canonical user entity for authentication and multi-tenancy.

Single source of truth for all users (replaces the split admin.users / app.users).
AdminUser in ui/auth/models.py is an alias pointing here.
"""

from core.orm import fields
from core.orm.model import Model

from .company import Company


class User(Model):
    _schema = "app"
    _name = "users"
    _tenant_field = None  # User itself is not tenant-scoped

    username = fields.Text(unique=True)
    email = fields.Text()
    # Uniqueness enforced by partial index idx_users_email_lower
    # (migration 013 in ui/auth/database.py), not the ORM

    password_hash = fields.Text()  # nullable = bot-only user
    display_name = fields.Text()
    avatar_url = fields.Text()
    locale = fields.Text(default="en")
    company_id = fields.ForeignKey(Company, on_delete="SET NULL")
    # Auth fields
    totp_secret = fields.Text()
    totp_enabled = fields.Boolean(default=False)
    webauthn_enabled = fields.Boolean(default=False)
    is_active = fields.Boolean(default=True)
    is_superadmin = fields.Boolean(default=False)
    failed_login_attempts = fields.Integer(default=0)
    lockout_until = fields.DateTime()
    created_at = fields.DateTime(auto_now_add=True)
    last_login = fields.DateTime()

from unittest.mock import MagicMock, patch

import pytest


def _unique_violation(constraint_name):
    """Build a UniqueViolation whose .diag.constraint_name is set.

    The real psycopg .diag is a read-only C attribute populated only when the
    error comes from the server, so we subclass and override it for unit tests.
    Subclassing keeps the stub isolated (no global mutation of the psycopg
    class) while remaining a genuine UniqueViolation for the route's except.
    """
    import psycopg

    class _StubUniqueViolation(psycopg.errors.UniqueViolation):
        diag = MagicMock(constraint_name=constraint_name)

    return _StubUniqueViolation("dup")


@pytest.mark.asyncio
async def test_create_portal_user_duplicate_email_redirects():
    from ui.routes import users

    mock_db = MagicMock()
    mock_db.get_user_by_username.return_value = None
    mock_db.create_user.side_effect = _unique_violation("idx_users_email_lower")

    with (
        patch("ui.routes.users.auth_db", new=mock_db),
        patch("ui.routes.auth.hash_password", return_value="h"),
    ):
        resp = await users.create_portal_user(
            request=None,
            username="newuser",
            password="a-strong-password-123",
            display_name="New",
            email="dup@example.com",
            is_superadmin="",
            _=True,
        )
    assert resp.status_code == 303
    assert "error=email_exists" in resp.headers["location"]


@pytest.mark.asyncio
async def test_create_portal_user_username_race_redirects():
    from ui.routes import users

    mock_db = MagicMock()
    mock_db.get_user_by_username.return_value = None
    mock_db.create_user.side_effect = _unique_violation("users_username_key")

    with (
        patch("ui.routes.users.auth_db", new=mock_db),
        patch("ui.routes.auth.hash_password", return_value="h"),
    ):
        resp = await users.create_portal_user(
            request=None,
            username="raced",
            password="a-strong-password-123",
            display_name="R",
            email="r@example.com",
            is_superadmin="",
            _=True,
        )
    assert resp.status_code == 303
    assert "error=username_exists" in resp.headers["location"]


@pytest.mark.asyncio
async def test_update_portal_user_duplicate_email_redirects():
    import psycopg

    from ui.routes import users

    mock_db = MagicMock()
    mock_db.update_user.side_effect = psycopg.errors.UniqueViolation("dup email")

    with patch("ui.routes.users.auth_db", new=mock_db):
        resp = await users.update_portal_user(
            request=None,
            user_id=1,
            display_name="Existing",
            email="dup@example.com",
            is_superadmin="",
            is_active="1",
            _=True,
        )
    assert resp.status_code == 303
    assert "error=email_exists" in resp.headers["location"]

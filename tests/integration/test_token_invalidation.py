import os
from unittest.mock import patch

import pytest

DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL not set"),
]


@pytest.fixture(scope="module")
def pg_db():
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from core.config_models import PasswordToken
    from core.database import DatabaseManager
    from core.models.company import Company
    from core.models.user import User
    from core.orm.migrate import migrate_all
    from core.orm.model import set_database as orm_set_database

    dm = DatabaseManager(DATABASE_URL)
    dm._sync_pool = ConnectionPool(
        DATABASE_URL,
        min_size=1,
        max_size=3,
        open=False,
        kwargs={"row_factory": dict_row},
    )
    dm._sync_pool.open()

    orm_set_database(dm)

    with dm.acquire_sync() as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS admin")
        conn.execute("CREATE SCHEMA IF NOT EXISTS app")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS public._migrations ("
            "id SERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL, "
            "applied_at TIMESTAMPTZ DEFAULT NOW())"
        )
        # Stale environment fixup: this test DB may contain a leftover
        # app.users table predating the current User model (PK on
        # `username`, no `id` column). migrate_all() only ever ADDs
        # columns to existing tables, it never fixes a wrong PK, so a
        # stale table would break the PasswordToken.user_id FK to
        # app.users(id). Drop and let migrate_all recreate it correctly.
        conn.execute("DROP TABLE IF EXISTS app.password_tokens CASCADE")
        conn.execute("DROP TABLE IF EXISTS app.users CASCADE")
        # Dropping app.users also destroys the idx_users_email_lower
        # index applied by ui.auth.database migration 013. Clear its
        # marker so init_auth_db() (used by other integration tests
        # sharing this DB) re-applies it against the recreated table.
        conn.execute(
            "DELETE FROM public._migrations WHERE name = %s",
            ("013_users_email_unique",),
        )
        conn.commit()

    migrate_all([Company, User, PasswordToken], dm)

    yield dm
    dm._sync_pool.close()


def test_generate_token_invalidates_prior_unused_token(pg_db):
    """generate_token must invalidate prior unused reset tokens for the
    same user+purpose. Regression for `("used_at", "=", None)` which is
    SQL `used_at = NULL` (always false) instead of `used_at IS NULL`.
    """
    from core.models.user import User

    with patch("core.registry.get_database", return_value=pg_db):
        user = User.create_sync(
            username="token_invalidation_test_user",
            password_hash="x",
            is_active=True,
        )
        user_id = user["id"] if isinstance(user, dict) else user

        try:
            from ui.auth.invite import generate_token

            t1 = generate_token(user_id, "reset")
            t2 = generate_token(user_id, "reset")

            assert t1 != t2

            with pg_db.acquire_sync() as conn:
                rows = conn.execute(
                    "SELECT used_at FROM app.password_tokens "
                    "WHERE user_id = %s AND purpose = 'reset' "
                    "ORDER BY created_at",
                    (user_id,),
                ).fetchall()

            assert len(rows) == 2
            assert rows[0]["used_at"] is not None, (
                "first token should have been invalidated by the second "
                "generate_token() call"
            )
            assert rows[1]["used_at"] is None

            unused_count_row = None
            with pg_db.acquire_sync() as conn:
                unused_count_row = conn.execute(
                    "SELECT count(*) AS c FROM app.password_tokens "
                    "WHERE user_id = %s AND purpose = 'reset' "
                    "AND used_at IS NULL",
                    (user_id,),
                ).fetchone()

            assert unused_count_row["c"] == 1
        finally:
            with pg_db.acquire_sync() as conn:
                conn.execute(
                    "DELETE FROM app.password_tokens WHERE user_id = %s",
                    (user_id,),
                )
                conn.execute(
                    "DELETE FROM app.users WHERE id = %s",
                    (user_id,),
                )
                conn.commit()

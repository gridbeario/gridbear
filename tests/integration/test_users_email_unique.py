import os

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
        conn.commit()

    # Ensure app.users exists (ORM-managed) before the migration under test
    # creates a unique index against it. Company must be migrated first —
    # User.company_id is a FK to app.companies.
    migrate_all([Company, User], dm)

    yield dm
    dm._sync_pool.close()


def test_migration_creates_partial_unique_index_and_is_idempotent(pg_db):
    from unittest.mock import patch

    # init_auth_db() does `from core.registry import get_database` INSIDE the
    # function body, so patching core.registry.get_database is sufficient —
    # the local import resolves the attribute on core.registry at call time.
    # ui.auth.database has no module-level get_database name to patch.
    with patch("core.registry.get_database", return_value=pg_db):
        from ui.auth.database import init_auth_db

        init_auth_db()
        init_auth_db()  # second call must not raise

    with pg_db.acquire_sync() as conn:
        row = conn.execute(
            "SELECT 1 FROM pg_indexes WHERE schemaname='app' "
            "AND indexname='idx_users_email_lower'"
        ).fetchone()
    assert row is not None


def test_create_user_duplicate_email_rejected(pg_db):
    """Two users sharing an email (case-insensitive) must be rejected at DB level."""
    import psycopg

    with pg_db.acquire_sync() as conn:
        conn.execute(
            "INSERT INTO app.users (username, email, is_active) "
            "VALUES ('dup_a', 'dup@example.com', true) "
            "ON CONFLICT (username) DO NOTHING"
        )
        conn.commit()
    with pg_db.acquire_sync() as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO app.users (username, email, is_active) "
                "VALUES ('dup_b', 'DUP@example.com', true)"
            )
            conn.commit()
    with pg_db.acquire_sync() as conn:
        conn.execute("DELETE FROM app.users WHERE username IN ('dup_a','dup_b')")
        conn.commit()

"""Shared fixtures for artifacts plugin unit tests.

Tests requiring a live database are gated on ``TEST_DATABASE_URL``.
Run with::

    TEST_DATABASE_URL="postgresql://user:pass@host:5432/testdb" \\
        pytest tests/unit/plugins/artifacts -v
"""

import os
from uuid import uuid4

import pytest

DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest.fixture
def fake_uuid() -> str:
    return str(uuid4())


@pytest.fixture
def hmac_secret(monkeypatch) -> str:
    secret = "test-secret-do-not-use-in-prod"
    monkeypatch.setenv("INTERNAL_API_SECRET", secret)
    return secret


@pytest.fixture
def tmp_data_dir(monkeypatch, tmp_path):
    """Redirect artifacts storage to a temp dir."""
    d = tmp_path / "artifacts"
    d.mkdir()
    monkeypatch.setenv("GRIDBEAR_DATA_DIR", str(tmp_path))
    return d


@pytest.fixture(scope="module")
def _pg_sync_bootstrap():
    """Run table migration once per module via a short-lived sync pool.

    Module-scoped so auto-migration (CREATE TABLE / ADD COLUMN) only runs once.
    Skips when TEST_DATABASE_URL is not set.
    """
    if not DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set — skipping live-DB tests")

    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from core.database import DatabaseManager
    from core.orm.migrate import migrate_all
    from plugins.artifacts.models import Artifact

    dm = DatabaseManager(DATABASE_URL)
    dm._sync_pool = ConnectionPool(
        DATABASE_URL,
        min_size=1,
        max_size=2,
        open=False,
        kwargs={"row_factory": dict_row},
    )
    dm._sync_pool.open()

    # Run migration for Artifact model only
    migrate_all([Artifact], dm)

    yield dm

    dm._sync_pool.close()


@pytest.fixture
async def db_initialised(_pg_sync_bootstrap):
    """Wire the ORM against the current test event loop's async pool.

    psycopg_pool.AsyncConnectionPool binds to the loop that opens it, so we
    create a fresh pool per test (function scope) to match pytest-asyncio's
    default loop policy.
    """
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    from core.database import DatabaseManager
    from core.orm.model import set_database

    dm = DatabaseManager(DATABASE_URL)
    # Reuse the module-scoped sync pool (for ORM sync paths, if any)
    dm._sync_pool = _pg_sync_bootstrap._sync_pool
    dm._async_pool = AsyncConnectionPool(
        DATABASE_URL,
        min_size=1,
        max_size=2,
        open=False,
        kwargs={"row_factory": dict_row},
    )
    await dm._async_pool.open()

    set_database(dm)

    # Clean state for this test
    with dm.acquire_sync() as conn:
        conn.execute("TRUNCATE TABLE app.artifacts")
        conn.commit()

    try:
        yield dm
    finally:
        await dm._async_pool.close()

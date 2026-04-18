"""Tests for the Artifact ORM model."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("TEST_DATABASE_URL"),
        reason="TEST_DATABASE_URL not set",
    ),
]


async def test_artifact_schema_and_table(db_initialised):
    from plugins.artifacts.models import Artifact

    assert Artifact._schema == "app"
    assert Artifact._name == "artifacts"


async def test_artifact_create_and_retrieve(db_initialised):
    from plugins.artifacts.models import Artifact

    aid = str(uuid4())
    now = datetime.now(UTC)
    await Artifact.create(
        id=aid,
        title="Test artifact",
        agent_id="peggy",
        owner_user_id="davide",
        conversation_id=None,
        file_path=f"artifacts/{aid}.html",
        size_bytes=1024,
        content_hash="a" * 64,
        pinned=False,
        expires_at=now + timedelta(days=30),
    )
    rows = await Artifact.search([("id", "=", aid)])
    assert len(rows) == 1
    assert rows[0]["title"] == "Test artifact"
    assert rows[0]["pinned"] is False
    assert rows[0]["revoked_at"] is None


async def test_artifact_update_pin(db_initialised):
    from plugins.artifacts.models import Artifact

    aid = str(uuid4())
    now = datetime.now(UTC)
    await Artifact.create(
        id=aid,
        title="T",
        agent_id="a",
        owner_user_id="u",
        conversation_id=None,
        file_path=f"artifacts/{aid}.html",
        size_bytes=10,
        content_hash="h",
        pinned=False,
        expires_at=now + timedelta(days=30),
    )
    await Artifact.write(aid, pinned=True)
    rows = await Artifact.search([("id", "=", aid)])
    assert rows[0]["pinned"] is True


async def test_size_bytes_check_constraint_rejects_oversized(db_initialised):
    """CHECK constraint rejects size_bytes > 10485760 (10 MB hard safety)."""
    from plugins.artifacts.models import Artifact

    aid = str(uuid4())
    now = datetime.now(UTC)
    with pytest.raises(Exception) as exc_info:
        await Artifact.create(
            id=aid,
            title="oversized",
            agent_id="a",
            owner_user_id="u",
            conversation_id=None,
            file_path=f"artifacts/{aid}.html",
            size_bytes=10_485_761,  # 1 byte over the cap
            content_hash="h" * 64,
            pinned=False,
            expires_at=now + timedelta(days=30),
        )
    # Expect the postgres CHECK violation to surface
    assert (
        "chk_artifact_size" in str(exc_info.value)
        or "check" in str(exc_info.value).lower()
    )


async def test_pinned_defaults_to_false_when_omitted(db_initialised):
    """`pinned` column default is false — row created without the kwarg comes back False."""
    from plugins.artifacts.models import Artifact

    aid = str(uuid4())
    now = datetime.now(UTC)
    await Artifact.create(
        id=aid,
        title="default-pin",
        agent_id="a",
        owner_user_id="u",
        conversation_id=None,
        file_path=f"artifacts/{aid}.html",
        size_bytes=42,
        content_hash="h" * 64,
        # pinned intentionally omitted
        expires_at=now + timedelta(days=30),
    )
    rows = await Artifact.search([("id", "=", aid)])
    assert rows[0]["pinned"] is False


async def test_created_at_and_updated_at_auto_populated(db_initialised):
    """created_at and updated_at both populate on insert; updated_at changes on write."""
    from plugins.artifacts.models import Artifact

    aid = str(uuid4())
    now = datetime.now(UTC)
    await Artifact.create(
        id=aid,
        title="timestamps",
        agent_id="a",
        owner_user_id="u",
        conversation_id=None,
        file_path=f"artifacts/{aid}.html",
        size_bytes=42,
        content_hash="h" * 64,
        pinned=False,
        expires_at=now + timedelta(days=30),
    )
    rows = await Artifact.search([("id", "=", aid)])
    created_initial = rows[0]["created_at"]
    updated_initial = rows[0]["updated_at"]
    assert created_initial is not None
    assert updated_initial is not None

    import asyncio

    await asyncio.sleep(0.01)  # ensure timestamp can advance
    await Artifact.write(aid, title="timestamps-renamed")
    rows = await Artifact.search([("id", "=", aid)])
    assert rows[0]["created_at"] == created_initial  # insert-only
    assert rows[0]["updated_at"] > updated_initial  # updated on write

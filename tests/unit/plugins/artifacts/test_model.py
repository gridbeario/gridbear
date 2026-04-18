"""Tests for the Artifact ORM model."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration


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

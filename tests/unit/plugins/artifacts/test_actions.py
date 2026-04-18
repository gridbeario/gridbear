"""Tests for pin/unpin/revoke/unrevoke/hard_delete via the service layer."""

import os
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("TEST_DATABASE_URL"),
        reason="TEST_DATABASE_URL not set",
    ),
]


async def test_pin_sets_flag(db_initialised, tmp_data_dir, hmac_secret):
    from plugins.artifacts.models import Artifact
    from plugins.artifacts.service import ArtifactsService

    svc = ArtifactsService()
    r = await svc.create(
        title="x",
        html="<!doctype html><html></html>",
        agent_id="a",
        owner_user_id="u",
    )
    await svc.pin(r["artifact_id"], pinned=True)
    row = (await Artifact.search([("id", "=", r["artifact_id"])]))[0]
    assert row["pinned"] is True


async def test_unpin_resets_expires_at(db_initialised, tmp_data_dir, hmac_secret):
    from plugins.artifacts.models import Artifact
    from plugins.artifacts.service import ArtifactsService

    svc = ArtifactsService()
    r = await svc.create(
        title="x",
        html="<!doctype html><html></html>",
        agent_id="a",
        owner_user_id="u",
        pin=True,
    )
    uid = r["artifact_id"]
    # Force expires_at into the distant past
    await Artifact.write(uid, expires_at=datetime.now(UTC) - timedelta(days=365))
    await svc.pin(uid, pinned=False)
    row = (await Artifact.search([("id", "=", uid)]))[0]
    assert row["pinned"] is False
    assert row["expires_at"] > datetime.now(UTC) + timedelta(days=20)


async def test_revoke_and_unrevoke(db_initialised, tmp_data_dir, hmac_secret):
    from plugins.artifacts.models import Artifact
    from plugins.artifacts.service import ArtifactsService

    svc = ArtifactsService()
    r = await svc.create(
        title="x",
        html="<!doctype html><html></html>",
        agent_id="a",
        owner_user_id="u",
    )
    uid = r["artifact_id"]
    await svc.revoke(uid)
    assert (await Artifact.search([("id", "=", uid)]))[0]["revoked_at"] is not None

    await svc.unrevoke(uid)
    assert (await Artifact.search([("id", "=", uid)]))[0]["revoked_at"] is None


async def test_hard_delete_removes_row_and_file(
    db_initialised, tmp_data_dir, hmac_secret
):
    from plugins.artifacts.models import Artifact
    from plugins.artifacts.service import ArtifactsService
    from plugins.artifacts.storage import exists

    svc = ArtifactsService()
    r = await svc.create(
        title="x",
        html="<!doctype html><html></html>",
        agent_id="a",
        owner_user_id="u",
    )
    uid = r["artifact_id"]
    assert exists(uid)
    await svc.hard_delete(uid)
    assert not exists(uid)
    assert len(await Artifact.search([("id", "=", uid)])) == 0

"""Tests for the cleanup worker."""

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


async def test_cleanup_removes_expired_not_pinned(
    db_initialised, tmp_data_dir, hmac_secret
):
    from plugins.artifacts.cleanup import run_cleanup_once
    from plugins.artifacts.models import Artifact
    from plugins.artifacts.service import ArtifactsService
    from plugins.artifacts.storage import exists

    r = await ArtifactsService().create(
        title="x",
        html="<!doctype html><html></html>",
        agent_id="a",
        owner_user_id="u",
    )
    uid = r["artifact_id"]
    await Artifact.write(uid, expires_at=datetime.now(UTC) - timedelta(days=1))
    await run_cleanup_once()
    assert not exists(uid)
    assert len(await Artifact.search([("id", "=", uid)])) == 0


async def test_cleanup_keeps_pinned_expired(db_initialised, tmp_data_dir, hmac_secret):
    from plugins.artifacts.cleanup import run_cleanup_once
    from plugins.artifacts.models import Artifact
    from plugins.artifacts.service import ArtifactsService
    from plugins.artifacts.storage import exists

    r = await ArtifactsService().create(
        title="x",
        html="<!doctype html><html></html>",
        agent_id="a",
        owner_user_id="u",
        pin=True,
    )
    uid = r["artifact_id"]
    await Artifact.write(uid, expires_at=datetime.now(UTC) - timedelta(days=1))
    await run_cleanup_once()
    assert exists(uid)
    assert len(await Artifact.search([("id", "=", uid)])) == 1


async def test_cleanup_missing_file_is_graceful(
    db_initialised, tmp_data_dir, hmac_secret
):
    from plugins.artifacts.cleanup import run_cleanup_once
    from plugins.artifacts.models import Artifact
    from plugins.artifacts.service import ArtifactsService
    from plugins.artifacts.storage import delete_artifact

    r = await ArtifactsService().create(
        title="x",
        html="<!doctype html><html></html>",
        agent_id="a",
        owner_user_id="u",
    )
    uid = r["artifact_id"]
    delete_artifact(uid)
    await Artifact.write(uid, expires_at=datetime.now(UTC) - timedelta(days=1))
    await run_cleanup_once()
    assert len(await Artifact.search([("id", "=", uid)])) == 0

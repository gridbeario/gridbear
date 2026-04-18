"""Tests for ArtifactsService.create()."""

import os

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("TEST_DATABASE_URL"),
        reason="TEST_DATABASE_URL not set",
    ),
]


async def test_create_persists_row_and_file(
    db_initialised, tmp_data_dir, hmac_secret, monkeypatch
):
    monkeypatch.setenv("GRIDBEAR_BASE_URL", "https://gb.example.com")
    from plugins.artifacts.models import Artifact
    from plugins.artifacts.service import ArtifactsService
    from plugins.artifacts.storage import read_artifact

    svc = ArtifactsService()
    html = "<!doctype html><html><body>ok</body></html>"
    result = await svc.create(
        title="Hello",
        html=html,
        agent_id="peggy",
        owner_user_id="davide",
        conversation_id="conv-1",
        pin=False,
        ttl_days=30,
    )
    assert "artifact_id" in result
    assert result["url"].startswith("https://gb.example.com/artifacts/")
    rows = await Artifact.search([("id", "=", result["artifact_id"])])
    assert len(rows) == 1
    assert read_artifact(result["artifact_id"]) == html


async def test_create_rejects_non_doctype(db_initialised, tmp_data_dir, hmac_secret):
    from plugins.artifacts.errors import InvalidHtmlError
    from plugins.artifacts.service import ArtifactsService

    with pytest.raises(InvalidHtmlError):
        await ArtifactsService().create(
            title="x",
            html="<div>no doctype</div>",
            agent_id="a",
            owner_user_id="u",
        )


async def test_create_pin_true(db_initialised, tmp_data_dir, hmac_secret):
    from plugins.artifacts.models import Artifact
    from plugins.artifacts.service import ArtifactsService

    result = await ArtifactsService().create(
        title="T",
        html="<!doctype html><html></html>",
        agent_id="a",
        owner_user_id="u",
        pin=True,
    )
    rows = await Artifact.search([("id", "=", result["artifact_id"])])
    assert rows[0]["pinned"] is True


async def test_create_empty_title_raises(db_initialised, tmp_data_dir, hmac_secret):
    from plugins.artifacts.service import ArtifactsService

    with pytest.raises(ValueError):
        await ArtifactsService().create(
            title="   ",
            html="<!doctype html><html></html>",
            agent_id="a",
            owner_user_id="u",
        )

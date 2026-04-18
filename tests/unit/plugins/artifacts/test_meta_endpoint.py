"""Tests for the public meta endpoint."""

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("TEST_DATABASE_URL"),
        reason="TEST_DATABASE_URL not set",
    ),
]


@pytest.fixture
async def client(db_initialised, tmp_data_dir, hmac_secret):
    # Warm the async pool on the test's event loop so the first connection
    # is already checked out / returned by the time TestClient's portal
    # thread needs one (psycopg_pool's internal asyncio.Lock is loop-bound).
    from plugins.artifacts.api import routes as art_routes
    from plugins.artifacts.models import Artifact

    await Artifact.search([("id", "=", "__warmup__")])

    app = FastAPI()
    app.include_router(art_routes.router, prefix="/artifacts")
    return TestClient(app)


async def test_meta_returns_public_fields(client):
    from plugins.artifacts.service import ArtifactsService

    r = await ArtifactsService().create(
        title="Dashboard",
        html="<!doctype html><html></html>",
        agent_id="peggy",
        owner_user_id="davide",
    )
    resp = client.get(f"/artifacts/{r['artifact_id']}/meta")
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Dashboard"
    assert body["agent_id"] == "peggy"
    assert "size_bytes" in body
    assert "created_at" in body
    # Make sure sensitive / internal fields are NOT exposed
    assert "content_hash" not in body
    assert "file_path" not in body
    assert "owner_user_id" not in body


def test_meta_unknown_returns_404(client):
    resp = client.get("/artifacts/00000000-0000-0000-0000-000000000000/meta")
    assert resp.status_code == 404

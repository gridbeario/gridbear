"""Tests for the public serve route."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

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


async def _create_valid(**kw):
    from plugins.artifacts.service import ArtifactsService

    defaults = dict(
        title="T",
        html="<!doctype html><html><body>hi</body></html>",
        agent_id="a",
        owner_user_id="u",
        pin=False,
        ttl_days=30,
    )
    defaults.update(kw)
    return await ArtifactsService().create(**defaults)


async def test_wrapper_response(client):
    r = await _create_valid()
    uid = r["artifact_id"]
    token = r["url"].split("t=")[1]
    resp = client.get(f"/artifacts/{uid}", params={"t": token})
    assert resp.status_code == 200
    assert "<iframe" in resp.text
    assert "mode=embed" in resp.text


async def test_embed_response_has_csp(client):
    r = await _create_valid(html="<!doctype html><html><body>payload</body></html>")
    uid = r["artifact_id"]
    token = r["url"].split("t=")[1]
    resp = client.get(f"/artifacts/{uid}", params={"t": token, "mode": "embed"})
    assert resp.status_code == 200
    assert "payload" in resp.text
    csp = resp.headers.get("content-security-policy", "")
    assert "connect-src 'none'" in csp
    assert "frame-ancestors 'self'" in csp
    assert resp.headers.get("x-frame-options") == "SAMEORIGIN"


async def test_invalid_token_returns_403(client):
    r = await _create_valid()
    resp = client.get(f"/artifacts/{r['artifact_id']}", params={"t": "deadbeef" * 4})
    assert resp.status_code == 403


async def test_unknown_uuid_returns_404(client):
    # Async so the db_initialised async pool is bound to the same event loop
    # that TestClient's portal uses for the endpoint coroutine.
    from plugins.artifacts.signing import sign_uuid

    uid = str(uuid4())
    resp = client.get(f"/artifacts/{uid}", params={"t": sign_uuid(uid)})
    assert resp.status_code == 404


async def test_revoked_returns_410(client):
    from plugins.artifacts.models import Artifact

    r = await _create_valid()
    uid = r["artifact_id"]
    token = r["url"].split("t=")[1]
    await Artifact.write(uid, revoked_at=datetime.now(UTC))
    resp = client.get(f"/artifacts/{uid}", params={"t": token})
    assert resp.status_code == 410
    assert "revoked" in resp.text.lower()


async def test_expired_not_pinned_returns_410(client):
    from plugins.artifacts.models import Artifact

    r = await _create_valid()
    uid = r["artifact_id"]
    token = r["url"].split("t=")[1]
    await Artifact.write(uid, expires_at=datetime.now(UTC) - timedelta(days=1))
    resp = client.get(f"/artifacts/{uid}", params={"t": token})
    assert resp.status_code == 410
    assert "expired" in resp.text.lower()


async def test_expired_but_pinned_returns_200(client):
    from plugins.artifacts.models import Artifact

    r = await _create_valid(pin=True)
    uid = r["artifact_id"]
    token = r["url"].split("t=")[1]
    await Artifact.write(uid, expires_at=datetime.now(UTC) - timedelta(days=1))
    resp = client.get(f"/artifacts/{uid}", params={"t": token})
    assert resp.status_code == 200


async def test_file_missing_returns_404(client):
    from plugins.artifacts.storage import delete_artifact

    r = await _create_valid()
    uid = r["artifact_id"]
    token = r["url"].split("t=")[1]
    delete_artifact(uid)
    resp = client.get(f"/artifacts/{uid}", params={"t": token, "mode": "embed"})
    assert resp.status_code == 404

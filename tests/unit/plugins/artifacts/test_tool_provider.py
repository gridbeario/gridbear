"""Tests for ArtifactsToolProvider."""

import os

import pytest


def test_server_name():
    from plugins.artifacts.service import ArtifactsToolProvider

    assert ArtifactsToolProvider().get_server_name() == "artifacts"


def test_tools_contain_create_artifact():
    from plugins.artifacts.service import ArtifactsToolProvider

    names = [t["name"] for t in ArtifactsToolProvider().get_tools()]
    assert "artifacts__create_artifact" in names


async def test_unknown_tool_returns_error_text():
    from plugins.artifacts.service import ArtifactsToolProvider

    out = await ArtifactsToolProvider().handle_tool_call("artifacts__nope", {})
    assert "Unknown" in out[0]["text"]


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL not set",
)
async def test_create_tool_success(
    db_initialised, tmp_data_dir, hmac_secret, monkeypatch
):
    monkeypatch.setenv("GRIDBEAR_BASE_URL", "https://gb.example.com")
    from plugins.artifacts.service import ArtifactsToolProvider

    out = await ArtifactsToolProvider().handle_tool_call(
        "artifacts__create_artifact",
        {"title": "Hi", "html": "<!doctype html><html></html>"},
        agent_name="peggy",
        oauth2_user="davide",
    )
    assert "URL:" in out[0]["text"]
    assert "https://gb.example.com/artifacts/" in out[0]["text"]

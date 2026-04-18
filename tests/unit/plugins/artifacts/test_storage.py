"""Tests for filesystem storage helpers."""

import hashlib
from uuid import uuid4

import pytest


def test_write_and_read_roundtrip(tmp_data_dir):
    from plugins.artifacts.storage import read_artifact, write_artifact

    uid = str(uuid4())
    html = "<!doctype html><html><body>hi</body></html>"
    rel = write_artifact(uid, html)
    assert rel == f"artifacts/{uid}.html"
    assert read_artifact(uid) == html


def test_compute_hash_and_size(tmp_data_dir):
    from plugins.artifacts.storage import compute_hash_and_size

    html = "<!doctype html><html></html>"
    h, s = compute_hash_and_size(html)
    assert h == hashlib.sha256(html.encode()).hexdigest()
    assert s == len(html.encode())


def test_validate_rejects_non_doctype(tmp_data_dir):
    from plugins.artifacts.errors import InvalidHtmlError
    from plugins.artifacts.storage import validate_html

    with pytest.raises(InvalidHtmlError):
        validate_html("<div>no doc</div>", max_bytes=1_000_000)


def test_validate_rejects_oversized(tmp_data_dir):
    from plugins.artifacts.errors import HtmlTooLargeError
    from plugins.artifacts.storage import validate_html

    big = "<!doctype html>" + "x" * 1_000_000
    with pytest.raises(HtmlTooLargeError):
        validate_html(big, max_bytes=500)


def test_validate_accepts_uppercase_doctype(tmp_data_dir):
    from plugins.artifacts.storage import validate_html

    validate_html("<!DOCTYPE HTML><html></html>", max_bytes=1_000_000)


def test_delete_removes_file(tmp_data_dir):
    from plugins.artifacts.storage import delete_artifact, write_artifact

    uid = str(uuid4())
    write_artifact(uid, "<!doctype html><html></html>")
    delete_artifact(uid)
    assert not (tmp_data_dir / f"{uid}.html").exists()


def test_delete_missing_is_idempotent(tmp_data_dir):
    from plugins.artifacts.storage import delete_artifact

    delete_artifact("nonexistent-uuid")

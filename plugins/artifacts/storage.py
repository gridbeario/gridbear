"""Filesystem storage helpers for artifact HTML files.

Files live at {DATA_DIR}/artifacts/<uuid>.html. DATA_DIR defaults to
/app/data inside the container; tests override via GRIDBEAR_DATA_DIR.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from plugins.artifacts.errors import HtmlTooLargeError, InvalidHtmlError

_SUBDIR = "artifacts"


def _base_dir() -> Path:
    data = os.environ.get("GRIDBEAR_DATA_DIR", "/app/data")
    p = Path(data) / _SUBDIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def _absolute_path(uuid_str: str) -> Path:
    return _base_dir() / f"{uuid_str}.html"


def validate_html(html: str, *, max_bytes: int) -> None:
    stripped = html.lstrip()
    if not stripped.lower().startswith("<!doctype html"):
        raise InvalidHtmlError("HTML must start with <!doctype html>")
    size = len(html.encode())
    if size > max_bytes:
        raise HtmlTooLargeError(f"HTML is {size} bytes; max allowed is {max_bytes}")


def compute_hash_and_size(html: str) -> tuple[str, int]:
    encoded = html.encode()
    return hashlib.sha256(encoded).hexdigest(), len(encoded)


def write_artifact(uuid_str: str, html: str) -> str:
    _absolute_path(uuid_str).write_text(html, encoding="utf-8")
    return f"{_SUBDIR}/{uuid_str}.html"


def read_artifact(uuid_str: str) -> str:
    return _absolute_path(uuid_str).read_text(encoding="utf-8")


def delete_artifact(uuid_str: str) -> None:
    p = _absolute_path(uuid_str)
    if p.exists():
        p.unlink()


def exists(uuid_str: str) -> bool:
    return _absolute_path(uuid_str).exists()

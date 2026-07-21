from unittest.mock import AsyncMock

import pytest

import plugins.ms365.server as srv


def _server():
    s = srv.MS365Server()
    s._get_valid_token = AsyncMock(return_value="fake-token")
    return s


class _FakeStream:
    """Minimal async context manager mimicking httpx stream response."""

    def __init__(self, chunks, status=200, headers=None):
        self._chunks = chunks
        self.status_code = status
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c


class _FakeClient:
    def __init__(self, stream):
        self._stream = stream

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url):
        return self._stream


@pytest.mark.asyncio
async def test_download_stream_ok(monkeypatch):
    s = _server()
    stream = _FakeStream([b"abc", b"def"])
    monkeypatch.setattr(srv.httpx, "AsyncClient", lambda **kw: _FakeClient(stream))
    assert await s._download_stream("https://dl/x") == b"abcdef"


@pytest.mark.asyncio
async def test_download_stream_size_guard_streamed(monkeypatch):
    s = _server()
    # No Content-Length; over the cap only becomes clear while streaming.
    stream = _FakeStream([b"x" * 10, b"x" * 10])
    monkeypatch.setattr(srv.httpx, "AsyncClient", lambda **kw: _FakeClient(stream))
    with pytest.raises(srv.SharedReadError, match="too large"):
        await s._download_stream("https://dl/x", max_bytes=15)


@pytest.mark.asyncio
async def test_read_item_folder_guard():
    s = _server()
    with pytest.raises(srv.SharedReadError, match="folder"):
        await s._read_item({"name": "Docs", "folder": {"childCount": 3}})


@pytest.mark.asyncio
async def test_read_item_downloads(monkeypatch):
    s = _server()
    monkeypatch.setattr(s, "_download_stream", AsyncMock(return_value=b"BYTES"))
    content, name = await s._read_item(
        {"name": "f.docx", "@microsoft.graph.downloadUrl": "https://dl/f"}
    )
    assert content == b"BYTES" and name == "f.docx"


class _FakeResp:
    def __init__(self, status, headers):
        self.status_code = status
        self.headers = headers


class _FakeGetClient:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        return self._resp


@pytest.mark.asyncio
async def test_read_item_fallback_302(monkeypatch):
    s = _server()
    getcli = _FakeGetClient(_FakeResp(302, {"location": "https://dl/redir"}))
    monkeypatch.setattr(srv.httpx, "AsyncClient", lambda **kw: getcli)
    monkeypatch.setattr(s, "_download_stream", AsyncMock(return_value=b"FB"))
    content, name = await s._read_item(
        {"name": "f.pdf", "id": "I1", "parentReference": {"driveId": "D1"}}
    )
    assert content == b"FB" and name == "f.pdf"


@pytest.mark.asyncio
async def test_read_item_fallback_error_status(monkeypatch):
    s = _server()
    getcli = _FakeGetClient(_FakeResp(403, {}))
    monkeypatch.setattr(srv.httpx, "AsyncClient", lambda **kw: getcli)
    with pytest.raises(srv.SharedReadError, match="HTTP 403"):
        await s._read_item(
            {"name": "f.pdf", "id": "I1", "parentReference": {"driveId": "D1"}}
        )


@pytest.mark.asyncio
async def test_read_shared_by_link(monkeypatch):
    s = _server()
    s._graph_request = AsyncMock(
        return_value={
            "name": "plan.docx",
            "@microsoft.graph.downloadUrl": "https://dl/p",
        }
    )
    monkeypatch.setattr(s, "_download_stream", AsyncMock(return_value=b"raw"))
    monkeypatch.setattr(srv, "extract_text", lambda b, n, **k: f"TEXT:{n}")
    res = await s._call_tool_impl(
        "m365_read_shared", {"sharing_url": "https://share/x"}
    )
    assert res["success"] is True and res["content"] == "TEXT:plan.docx"
    endpoint = s._graph_request.call_args[0][1]
    assert endpoint.startswith("/shares/u!") and "u!u!" not in endpoint


@pytest.mark.asyncio
async def test_read_shared_by_drive_item(monkeypatch):
    s = _server()
    s._graph_request = AsyncMock(
        return_value={"name": "s.xlsx", "@microsoft.graph.downloadUrl": "https://dl/s"}
    )
    monkeypatch.setattr(s, "_download_stream", AsyncMock(return_value=b"raw"))
    monkeypatch.setattr(srv, "extract_text", lambda b, n, **k: "OK")
    res = await s._call_tool_impl(
        "m365_read_shared", {"drive_id": "D1", "item_id": "I1"}
    )
    assert res["success"] is True
    assert s._graph_request.call_args[0][1] == "/drives/D1/items/I1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args,msg",
    [
        ({}, "provide either"),
        ({"drive_id": "D1"}, "supplied together"),
        ({"item_id": "I1"}, "supplied together"),
        ({"sharing_url": "u", "drive_id": "D1", "item_id": "I1"}, "only one"),
    ],
)
async def test_read_shared_input_contract(args, msg):
    s = _server()
    s._graph_request = AsyncMock()
    res = await s._call_tool_impl("m365_read_shared", args)
    assert res["success"] is False and msg in res["error"]
    s._graph_request.assert_not_called()


@pytest.mark.asyncio
async def test_read_shared_folder_guard():
    s = _server()
    s._graph_request = AsyncMock(
        return_value={"name": "Dir", "folder": {"childCount": 2}}
    )
    res = await s._call_tool_impl("m365_read_shared", {"sharing_url": "https://s/d"})
    assert res["success"] is False and "folder" in res["error"]


@pytest.mark.asyncio
async def test_read_shared_403_scope_message():
    s = _server()
    s._graph_request = AsyncMock(side_effect=Exception("Graph API error (403): denied"))
    res = await s._call_tool_impl("m365_read_shared", {"sharing_url": "https://s/d"})
    assert res["success"] is False and "Files.Read.All" in res["error"]

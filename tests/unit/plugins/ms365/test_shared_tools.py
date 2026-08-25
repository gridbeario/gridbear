import io
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


def _shared_item(name, drive, item, folder=False):
    it = {
        "name": name,
        "webUrl": f"https://w/{item}",
        "lastModifiedDateTime": "2026-07-20T10:00:00Z",
        "remoteItem": {
            "id": item,
            "parentReference": {"driveId": drive},
            "createdBy": {"user": {"displayName": "Alice"}},
        },
    }
    if folder:
        it["remoteItem"]["folder"] = {"childCount": 1}
    return it


@pytest.mark.asyncio
async def test_list_shared_single_page():
    s = _server()
    s._graph_request = AsyncMock(
        return_value={"value": [_shared_item("a.docx", "D", "I1")]}
    )
    res = await s._call_tool_impl("m365_list_shared", {})
    assert res["success"] is True and res["count"] == 1
    row = res["items"][0]
    assert row["name"] == "a.docx" and row["drive_id"] == "D" and row["item_id"] == "I1"
    assert row["is_folder"] is False and row["shared_by"] == "Alice"


@pytest.mark.asyncio
async def test_list_shared_follows_nextlink():
    s = _server()
    page1 = {
        "value": [_shared_item("a", "D", "I1")],
        "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/drive/sharedWithMe?$skip=1",
    }
    page2 = {"value": [_shared_item("b", "D", "I2")]}
    s._graph_request = AsyncMock(side_effect=[page1, page2])
    res = await s._call_tool_impl("m365_list_shared", {})
    assert res["count"] == 2 and res.get("truncated") is not True
    assert s._graph_request.call_args_list[1][0][1].startswith(
        "https://graph.microsoft.com"
    )


@pytest.mark.asyncio
async def test_list_shared_truncates_at_cap(monkeypatch):
    s = _server()
    monkeypatch.setattr(srv, "_SHARED_MAX_PAGES", 2)
    page = {
        "value": [_shared_item("x", "D", "I")],
        "@odata.nextLink": "https://graph.microsoft.com/v1.0/next",
    }
    s._graph_request = AsyncMock(return_value=page)  # always returns a nextLink
    res = await s._call_tool_impl("m365_list_shared", {})
    assert res["truncated"] is True
    assert s._graph_request.call_count == 2  # stopped at the page cap


@pytest.mark.asyncio
async def test_list_shared_null_fields_robust():
    s = _server()
    s._graph_request = AsyncMock(
        return_value={
            "value": [
                {
                    "name": "x",
                    "remoteItem": {
                        "id": "I",
                        "parentReference": None,
                        "createdBy": None,
                    },
                }
            ]
        }
    )
    res = await s._call_tool_impl("m365_list_shared", {})
    assert res["success"] is True and res["count"] == 1
    row = res["items"][0]
    assert (
        row["drive_id"] is None
        and row["shared_by"] is None
        and row["is_folder"] is False
    )


def _docx_bytes_local(text):
    from docx import Document

    d = Document()
    d.add_paragraph(text)
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


@pytest.mark.asyncio
async def test_read_file_extracts_docx():
    s = _server()
    s._graph_request = AsyncMock(
        side_effect=[
            {"value": [{"id": "drv1"}]},  # drives lookup
            _docx_bytes_local("Report body"),  # /content bytes
        ]
    )
    res = await s._call_tool_impl(
        "m365_read_file", {"site_id": "S", "file_path": "/r.docx"}
    )
    assert res["success"] is True and "Report body" in res["content"]


@pytest.mark.asyncio
async def test_read_drive_file_extracts_docx():
    s = _server()
    s._graph_request = AsyncMock(return_value=_docx_bytes_local("Drive doc"))
    res = await s._call_tool_impl("m365_read_drive_file", {"file_path": "/d.docx"})
    assert res["success"] is True and "Drive doc" in res["content"]


@pytest.mark.asyncio
async def test_read_file_plain_text_preserved():
    # A non-Office utf-8 file (e.g. .py) must return its content (old behavior).
    s = _server()
    s._graph_request = AsyncMock(
        side_effect=[
            {"value": [{"id": "drv1"}]},
            b"print('hello')",
        ]
    )
    res = await s._call_tool_impl(
        "m365_read_file", {"site_id": "S", "file_path": "/app.py"}
    )
    assert res["success"] is True and "print('hello')" in res["content"]


def test_new_tools_in_provider_allowlist():
    from plugins.ms365.provider import MS365Provider

    provider = MS365Provider({"client_id": "x"})
    provider._server_names = ["ms365-test"]
    allowed = provider.get_allowed_tools()
    assert "mcp__ms365-test__m365_list_shared" in allowed
    assert "mcp__ms365-test__m365_read_shared" in allowed


def test_graph_error_message_helper():
    assert "Files.Read.All" in srv._graph_error_message(
        Exception("Graph API error (403): x"), "read"
    )
    assert "not found" in srv._graph_error_message(
        Exception("Graph API error (404): x"), "read"
    )
    assert (
        srv._graph_error_message(Exception("Graph API error (500): boom"), "list")
        == "list failed: Graph API error (500): boom"
    )


@pytest.mark.asyncio
async def test_read_shared_404_message():
    s = _server()
    s._graph_request = AsyncMock(side_effect=Exception("Graph API error (404): gone"))
    res = await s._call_tool_impl("m365_read_shared", {"sharing_url": "https://s/d"})
    assert res["success"] is False and "not found" in res["error"]


def _shared_item_graph_shape(name, drive, item, sharer="Alice Example"):
    """Shape /me/drive/sharedWithMe actually returns (captured from live Graph).

    Graph leaves createdBy null on these entries and reports the sharer under
    remoteItem.shared.sharedBy, so a fixture modelled on createdBy alone cannot
    tell whether the mapping works.
    """
    return {
        "name": name,
        "webUrl": f"https://w/{item}",
        "lastModifiedDateTime": "2026-06-17T09:46:26Z",
        "createdBy": None,
        "remoteItem": {
            "id": item,
            "parentReference": {"driveId": drive},
            "createdBy": None,
            "lastModifiedBy": None,
            "shared": {
                "scope": "users",
                "sharedDateTime": "2026-06-17T09:47:00Z",
                "sharedBy": {"user": {"displayName": sharer}},
            },
        },
    }


@pytest.mark.asyncio
async def test_list_shared_reports_who_shared_the_item():
    s = _server()
    s._graph_request = AsyncMock(
        return_value={"value": [_shared_item_graph_shape("a.docx", "D", "I1")]}
    )
    res = await s._call_tool_impl("m365_list_shared", {})
    assert res["items"][0]["shared_by"] == "Alice Example"


@pytest.mark.asyncio
async def test_list_shared_survives_a_share_block_without_an_identity():
    s = _server()
    item = _shared_item_graph_shape("a.docx", "D", "I1")
    item["remoteItem"]["shared"] = {"scope": "anonymous"}
    s._graph_request = AsyncMock(return_value={"value": [item]})
    res = await s._call_tool_impl("m365_list_shared", {})
    assert res["success"] is True and res["items"][0]["shared_by"] is None


@pytest.mark.asyncio
async def test_list_sites_keeps_the_search_term_in_the_request():
    """httpx drops a URL's existing query when params= is also given.

    Asserting the call shape is not enough — the check has to go through the
    request httpx actually builds, which is where the term was being lost.
    """
    import httpx

    s = _server()
    s._graph_request = AsyncMock(return_value={"value": []})
    await s._call_tool_impl("m365_list_sites", {"search": "Contoso"})

    call_args, call_kwargs = s._graph_request.call_args
    built = httpx.Client(base_url="https://graph.microsoft.com/v1.0").build_request(
        call_args[0], call_args[1], params=call_kwargs.get("params")
    )
    assert "search=Contoso" in str(built.url), str(built.url)


@pytest.mark.asyncio
async def test_list_sites_encodes_a_search_term_with_special_characters():
    import httpx

    s = _server()
    s._graph_request = AsyncMock(return_value={"value": []})
    await s._call_tool_impl("m365_list_sites", {"search": "R&D team"})

    call_args, call_kwargs = s._graph_request.call_args
    built = httpx.Client(base_url="https://graph.microsoft.com/v1.0").build_request(
        call_args[0], call_args[1], params=call_kwargs.get("params")
    )
    assert "R%26D" in str(built.url), str(built.url)


def test_denied_message_names_both_plausible_causes():
    """A 403 on a shared item has two very different causes.

    Blaming the scope alone sends the operator to re-authenticate an account
    that already carries Files.Read.All, when the item may simply live in a
    tenant where their access is a guest grant.
    """
    msg = srv._graph_error_message(Exception("Graph API error (403): denied"), "read")
    assert "Files.Read.All" in msg
    assert "tenant" in msg.lower()

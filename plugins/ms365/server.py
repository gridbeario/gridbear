#!/usr/bin/env python3
"""Microsoft 365 MCP Server.

Provides MCP tools for SharePoint, Planner, and OneDrive operations.
Token data is passed via environment variable MS365_TOKEN_DATA.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any

import httpx
import msal
from pydantic import Field

# Launched as a bare script by provider.get_server_config, so the repo root is
# not on sys.path. Add it before importing sibling plugin modules.
_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from plugins.ms365.extract import encode_sharing_url, extract_text

# MCP server imports
try:
    # The ergonomic server API is FastMCP on mcp 1.x and MCPServer on 2.x. They
    # are identical for what this file uses, so accepting either lets the major
    # be switched from pyproject alone. Drop this once the pin moves.
    try:  # mcp 2.x
        from mcp.server.mcpserver import MCPServer as _McpServer
    except ImportError:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP as _McpServer
except ImportError:
    print("MCP server library not installed", file=sys.stderr)
    sys.exit(1)


# Configuration from environment
CLIENT_ID = os.environ.get("MS365_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("MS365_CLIENT_SECRET", "")
TENANT_NAME = os.environ.get("MS365_TENANT_NAME", "default")
TENANT_ID = os.environ.get("MS365_TENANT_ID", "common")
TOKEN_DATA = os.environ.get("MS365_TOKEN_DATA", "{}")
ROLE = os.environ.get("MS365_ROLE", "guest")

GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"
MAX_INLINE_READ_BYTES = 50 * 1024 * 1024
_SHARED_MAX_ITEMS = 200
_SHARED_MAX_PAGES = 5
STATE_FILE = Path("/app/data/ms365_context.json")

# Parse token data
try:
    token_info = json.loads(TOKEN_DATA)
except json.JSONDecodeError:
    token_info = {}


class SharedReadError(Exception):
    """Actionable, user-facing error from a shared-item read."""


def _graph_error_message(err: Exception, action: str) -> str:
    """Map a _graph_request exception to an actionable message.

    NOTE: depends on _graph_request's message format
    "Graph API error ({status}): {msg}". If that format changes, update this.
    """
    msg = str(err)
    if "(403)" in msg:
        return (
            "access denied — either this account lacks Files.Read.All "
            "(re-authenticate it with the broader scope), or the item lives in "
            "another tenant where the access is a guest grant, which a token "
            "issued by this account's own tenant cannot use"
        )
    if "(404)" in msg:
        return "not found — link expired/revoked, or not shared to this account"
    return f"{action} failed: {msg}"


class MS365Server:
    """MCP server for Microsoft 365 operations."""

    def __init__(self):
        self.mcp = _McpServer("ms365-server")
        self.http_client: httpx.AsyncClient | None = None
        self.access_token: str | None = token_info.get("access_token")
        self.refresh_token: str | None = token_info.get("refresh_token")
        self.token_expires_at: datetime | None = None
        # Store scopes from token for refresh (default to basic scopes)
        self.token_scopes: list[str] = token_info.get(
            "scopes", ["User.Read", "Files.ReadWrite", "Tasks.ReadWrite"]
        )

        if token_info.get("expires_at"):
            try:
                expires_str = token_info["expires_at"].replace("Z", "+00:00")
                dt = datetime.fromisoformat(expires_str)
                # Ensure timezone-aware (assume UTC if naive)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                self.token_expires_at = dt
            except (ValueError, TypeError):
                pass

        self._msal_app = None
        if CLIENT_ID and CLIENT_SECRET:
            self._msal_app = msal.ConfidentialClientApplication(
                client_id=CLIENT_ID,
                client_credential=CLIENT_SECRET,
                authority=f"https://login.microsoftonline.com/{TENANT_ID}",
            )

        self._setup_tools()

    def _setup_tools(self):
        """Register every tool with the SDK.

        Schemas come from the signatures below, so the declared contract and
        the arguments the implementation reads cannot drift apart.
        """
        self.mcp.add_tool(
            self.m365_list_sites,
            name="m365_list_sites",
            description="List accessible SharePoint sites",
            structured_output=False,
        )
        self.mcp.add_tool(
            self.m365_get_site_by_url,
            name="m365_get_site_by_url",
            description="Get SharePoint site info by URL. Use this for guest tenant access.",
            structured_output=False,
        )
        self.mcp.add_tool(
            self.m365_list_files,
            name="m365_list_files",
            description="List files in a SharePoint folder",
            structured_output=False,
        )
        self.mcp.add_tool(
            self.m365_read_file,
            name="m365_read_file",
            description="Read file content from SharePoint",
            structured_output=False,
        )
        self.mcp.add_tool(
            self.m365_read_shared,
            name="m365_read_shared",
            description="Read a document SHARED WITH the user — by share link (from email) or by drive_id+item_id from m365_list_shared. Returns extracted text for Word/Excel/PDF.",
            structured_output=False,
        )
        self.mcp.add_tool(
            self.m365_list_shared,
            name="m365_list_shared",
            description="List documents shared WITH the user (Shared with me). Returns items with drive_id/item_id to pass to m365_read_shared.",
            structured_output=False,
        )
        self.mcp.add_tool(
            self.m365_write_file,
            name="m365_write_file",
            description="Write/upload file to SharePoint",
            structured_output=False,
        )
        self.mcp.add_tool(
            self.m365_search_files,
            name="m365_search_files",
            description="Search for files across SharePoint",
            structured_output=False,
        )
        self.mcp.add_tool(
            self.m365_list_groups,
            name="m365_list_groups",
            description="List Microsoft 365 groups the user is a member of",
            structured_output=False,
        )
        self.mcp.add_tool(
            self.m365_list_plans,
            name="m365_list_plans",
            description="List Planner plans. Use list_all=true to get plans from all groups.",
            structured_output=False,
        )
        self.mcp.add_tool(
            self.m365_get_plan_by_id,
            name="m365_get_plan_by_id",
            description="Get Planner plan details by ID. Use this for guest access to shared plans.",
            structured_output=False,
        )
        self.mcp.add_tool(
            self.m365_list_tasks,
            name="m365_list_tasks",
            description="List tasks in a Planner plan",
            structured_output=False,
        )
        self.mcp.add_tool(
            self.m365_get_task,
            name="m365_get_task",
            description="Get task details including description",
            structured_output=False,
        )
        self.mcp.add_tool(
            self.m365_create_task,
            name="m365_create_task",
            description="Create a new Planner task",
            structured_output=False,
        )
        self.mcp.add_tool(
            self.m365_complete_task,
            name="m365_complete_task",
            description="Mark a task as complete",
            structured_output=False,
        )
        self.mcp.add_tool(
            self.m365_list_drive_files,
            name="m365_list_drive_files",
            description="List files in OneDrive",
            structured_output=False,
        )
        self.mcp.add_tool(
            self.m365_read_drive_file,
            name="m365_read_drive_file",
            description="Read file from OneDrive",
            structured_output=False,
        )
        self.mcp.add_tool(
            self.m365_write_drive_file,
            name="m365_write_drive_file",
            description="Write file to OneDrive",
            structured_output=False,
        )

    async def _run_tool(self, name: str, args: dict) -> dict:
        """Dispatch a tool, record the operation, and report failures as data.

        Shared by every tool so tracking and error shape stay in one place.
        """
        try:
            result = await self._call_tool_impl(name, args)
            self._track_operation(name, args, result)
            return result
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def m365_list_sites(
        self,
        search: Annotated[
            str | None, Field(description="Optional search query")
        ] = None,
    ) -> dict:
        """List accessible SharePoint sites"""
        args = {"search": search}
        return await self._run_tool(
            "m365_list_sites", {k: v for k, v in args.items() if v is not None}
        )

    async def m365_get_site_by_url(
        self,
        site_url: Annotated[
            str,
            Field(
                description="SharePoint site URL, e.g. https://contoso.sharepoint.com/sites/MySite"
            ),
        ],
    ) -> dict:
        """Get SharePoint site info by URL. Use this for guest tenant access."""
        args = {"site_url": site_url}
        return await self._run_tool(
            "m365_get_site_by_url", {k: v for k, v in args.items() if v is not None}
        )

    async def m365_list_files(
        self,
        site_id: Annotated[str, Field(description="SharePoint site ID")],
        folder_path: Annotated[
            str | None, Field(description="Folder path (default: root)")
        ] = None,
    ) -> dict:
        """List files in a SharePoint folder"""
        args = {"site_id": site_id, "folder_path": folder_path}
        return await self._run_tool(
            "m365_list_files", {k: v for k, v in args.items() if v is not None}
        )

    async def m365_read_file(
        self,
        site_id: Annotated[str, Field(description="SharePoint site ID")],
        file_path: Annotated[str, Field(description="Path to the file")],
    ) -> dict:
        """Read file content from SharePoint"""
        args = {"site_id": site_id, "file_path": file_path}
        return await self._run_tool(
            "m365_read_file", {k: v for k, v in args.items() if v is not None}
        )

    async def m365_read_shared(
        self,
        sharing_url: Annotated[
            str | None, Field(description="Share link URL (from an email/message)")
        ] = None,
        drive_id: Annotated[
            str | None, Field(description="Drive ID (from m365_list_shared)")
        ] = None,
        item_id: Annotated[
            str | None, Field(description="Item ID (from m365_list_shared)")
        ] = None,
    ) -> dict:
        """Read a document SHARED WITH the user — by share link (from email) or by drive_id+item_id from m365_list_shared. Returns extracted text for Word/Excel/PDF."""
        args = {"sharing_url": sharing_url, "drive_id": drive_id, "item_id": item_id}
        return await self._run_tool(
            "m365_read_shared", {k: v for k, v in args.items() if v is not None}
        )

    async def m365_list_shared(self) -> dict:
        """List documents shared WITH the user (Shared with me). Returns items with drive_id/item_id to pass to m365_read_shared."""
        return await self._run_tool("m365_list_shared", {})

    async def m365_write_file(
        self,
        site_id: Annotated[str, Field(description="SharePoint site ID")],
        file_path: Annotated[str, Field(description="Path for the file")],
        content: Annotated[str, Field(description="File content")],
    ) -> dict:
        """Write/upload file to SharePoint"""
        args = {"site_id": site_id, "file_path": file_path, "content": content}
        return await self._run_tool(
            "m365_write_file", {k: v for k, v in args.items() if v is not None}
        )

    async def m365_search_files(
        self,
        query: Annotated[str, Field(description="Search query")],
        site_id: Annotated[
            str | None, Field(description="Optional: limit to specific site")
        ] = None,
    ) -> dict:
        """Search for files across SharePoint"""
        args = {"query": query, "site_id": site_id}
        return await self._run_tool(
            "m365_search_files", {k: v for k, v in args.items() if v is not None}
        )

    async def m365_list_groups(self) -> dict:
        """List Microsoft 365 groups the user is a member of"""
        return await self._run_tool("m365_list_groups", {})

    async def m365_list_plans(
        self,
        group_id: Annotated[
            str | None, Field(description="Optional: filter by specific group ID")
        ] = None,
        list_all: Annotated[
            bool | None,
            Field(
                description="If true, list plans from ALL groups (slower but complete)"
            ),
        ] = None,
    ) -> dict:
        """List Planner plans. Use list_all=true to get plans from all groups."""
        args = {"group_id": group_id, "list_all": list_all}
        return await self._run_tool(
            "m365_list_plans", {k: v for k, v in args.items() if v is not None}
        )

    async def m365_get_plan_by_id(
        self,
        plan_id: Annotated[
            str, Field(description="The Planner plan ID (from URL or shared link)")
        ],
    ) -> dict:
        """Get Planner plan details by ID. Use this for guest access to shared plans."""
        args = {"plan_id": plan_id}
        return await self._run_tool(
            "m365_get_plan_by_id", {k: v for k, v in args.items() if v is not None}
        )

    async def m365_list_tasks(
        self,
        plan_id: Annotated[str, Field(description="Plan ID")],
        bucket_id: Annotated[
            str | None, Field(description="Optional: filter by bucket")
        ] = None,
    ) -> dict:
        """List tasks in a Planner plan"""
        args = {"plan_id": plan_id, "bucket_id": bucket_id}
        return await self._run_tool(
            "m365_list_tasks", {k: v for k, v in args.items() if v is not None}
        )

    async def m365_get_task(
        self,
        task_id: Annotated[str, Field(description="Task ID")],
    ) -> dict:
        """Get task details including description"""
        args = {"task_id": task_id}
        return await self._run_tool(
            "m365_get_task", {k: v for k, v in args.items() if v is not None}
        )

    async def m365_create_task(
        self,
        plan_id: Annotated[str, Field(description="Plan ID")],
        title: Annotated[str, Field(description="Task title")],
        bucket_id: Annotated[
            str | None, Field(description="Optional: bucket ID")
        ] = None,
        due_date: Annotated[
            str | None, Field(description="Optional: due date (ISO format)")
        ] = None,
    ) -> dict:
        """Create a new Planner task"""
        args = {
            "plan_id": plan_id,
            "title": title,
            "bucket_id": bucket_id,
            "due_date": due_date,
        }
        return await self._run_tool(
            "m365_create_task", {k: v for k, v in args.items() if v is not None}
        )

    async def m365_complete_task(
        self,
        task_id: Annotated[str, Field(description="Task ID")],
    ) -> dict:
        """Mark a task as complete"""
        args = {"task_id": task_id}
        return await self._run_tool(
            "m365_complete_task", {k: v for k, v in args.items() if v is not None}
        )

    async def m365_list_drive_files(
        self,
        folder_path: Annotated[
            str | None, Field(description="Folder path (default: root)")
        ] = None,
    ) -> dict:
        """List files in OneDrive"""
        args = {"folder_path": folder_path}
        return await self._run_tool(
            "m365_list_drive_files", {k: v for k, v in args.items() if v is not None}
        )

    async def m365_read_drive_file(
        self,
        file_path: Annotated[str, Field(description="Path to the file")],
    ) -> dict:
        """Read file from OneDrive"""
        args = {"file_path": file_path}
        return await self._run_tool(
            "m365_read_drive_file", {k: v for k, v in args.items() if v is not None}
        )

    async def m365_write_drive_file(
        self,
        file_path: Annotated[str, Field(description="Path for the file")],
        content: Annotated[str, Field(description="File content")],
    ) -> dict:
        """Write file to OneDrive"""
        args = {"file_path": file_path, "content": content}
        return await self._run_tool(
            "m365_write_drive_file", {k: v for k, v in args.items() if v is not None}
        )

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self.http_client is None or self.http_client.is_closed:
            self.http_client = httpx.AsyncClient(
                base_url=GRAPH_API_BASE,
                timeout=30.0,
            )
        return self.http_client

    async def _get_valid_token(self) -> str | None:
        """Get valid access token, refreshing if needed."""
        if not self.access_token:
            return None

        # Check if token needs refresh
        if self.token_expires_at:
            buffer = timedelta(minutes=5)
            if datetime.now(timezone.utc) + buffer >= self.token_expires_at:
                if not await self._refresh_token():
                    return None

        return self.access_token

    async def _refresh_token(self) -> bool:
        """Refresh access token."""
        if not self._msal_app or not self.refresh_token:
            return False

        try:
            result = self._msal_app.acquire_token_by_refresh_token(
                refresh_token=self.refresh_token,
                scopes=self.token_scopes,
            )

            if "error" in result:
                return False

            self.access_token = result["access_token"]
            if result.get("refresh_token"):
                self.refresh_token = result["refresh_token"]

            expires_in = result.get("expires_in", 3600)
            self.token_expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=expires_in
            )
            return True

        except Exception:
            return False

    async def _graph_request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        json_data: dict | None = None,
        data: bytes | None = None,
        content_type: str | None = None,
    ) -> dict | bytes | None:
        """Make authenticated request to Graph API."""
        token = await self._get_valid_token()
        if not token:
            raise Exception("No valid access token")

        client = await self._get_client()

        headers = {"Authorization": f"Bearer {token}"}
        if content_type:
            headers["Content-Type"] = content_type

        response = await client.request(
            method=method,
            url=endpoint,
            headers=headers,
            params=params,
            json=json_data,
            content=data,
        )

        if response.status_code == 204:
            return None

        if response.status_code >= 400:
            error_text = response.text
            try:
                error_data = response.json()
                error_text = error_data.get("error", {}).get("message", response.text)
            except Exception:
                pass
            raise Exception(f"Graph API error ({response.status_code}): {error_text}")

        content_type_header = response.headers.get("content-type", "")
        if "application/json" in content_type_header:
            return response.json()
        return response.content

    async def _download_stream(
        self, url: str, max_bytes: int = MAX_INLINE_READ_BYTES
    ) -> bytes:
        """Download a pre-authenticated URL with NO auth header, streaming with a
        size guard. Aborts as soon as the running byte count exceeds max_bytes."""
        buf = bytearray()
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    raise SharedReadError(f"download failed (HTTP {resp.status_code})")
                clen = resp.headers.get("content-length")
                if clen and clen.isdigit() and int(clen) > max_bytes:
                    raise SharedReadError(
                        f"file too large (>{max_bytes // (1024 * 1024)} MB) "
                        "to read inline"
                    )
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        raise SharedReadError(
                            f"file too large (>{max_bytes // (1024 * 1024)} MB) "
                            "to read inline"
                        )
        return bytes(buf)

    async def _read_item(self, metadata: dict) -> tuple[bytes, str]:
        """From a resolved driveItem's metadata, return (content_bytes, filename).
        Guards folders; downloads via the pre-authenticated downloadUrl."""
        if metadata.get("folder") is not None:
            raise SharedReadError("shared item is a folder, not a readable document")
        name = metadata.get("name", "file")
        download_url = metadata.get("@microsoft.graph.downloadUrl")
        if download_url:
            return await self._download_stream(download_url), name
        # Fallback (rare): a file with no downloadUrl. Fetch /content without
        # following the redirect, read the 302 Location, download it unauthenticated.
        drive_id = (metadata.get("parentReference") or {}).get("driveId")
        item_id = metadata.get("id")
        if drive_id and item_id:
            token = await self._get_valid_token()
            async with httpx.AsyncClient(
                base_url=GRAPH_API_BASE, follow_redirects=False, timeout=30.0
            ) as client:
                resp = await client.get(
                    f"/drives/{drive_id}/items/{item_id}/content",
                    headers={"Authorization": f"Bearer {token}"},
                )
            if resp.status_code >= 400:
                raise SharedReadError(f"cannot read item (HTTP {resp.status_code})")
            loc = resp.headers.get("location")
            if loc:
                return await self._download_stream(loc), name
        raise SharedReadError("cannot read this item (no download URL available)")

    async def _call_tool_impl(self, name: str, args: dict) -> dict:
        """Execute tool and return result."""

        # SharePoint tools
        if name == "m365_list_sites":
            search = args.get("search", "*")
            # search goes through params, not the path: httpx replaces a URL's
            # existing query when params is given, which silently dropped the
            # term and made every listing come back empty. Passing it here also
            # encodes terms containing & or spaces.
            result = await self._graph_request(
                "GET", "/sites", params={"search": search, "$top": "50"}
            )
            if isinstance(result, dict) and "value" in result:
                sites = [
                    {
                        "id": s["id"],
                        "name": s.get("displayName", s.get("name", "")),
                        "web_url": s.get("webUrl", ""),
                    }
                    for s in result["value"]
                ]
                return {"success": True, "sites": sites, "count": len(sites)}
            return {"success": False, "error": "No sites found"}

        elif name == "m365_get_site_by_url":
            # Get SharePoint site by URL - useful for guest tenant access
            # URL format: https://contoso.sharepoint.com/sites/MySite
            from urllib.parse import urlparse

            site_url = args["site_url"]

            parsed = urlparse(site_url)
            hostname = parsed.netloc  # e.g., contoso.sharepoint.com
            site_path = parsed.path.rstrip("/")  # e.g., /sites/MySite

            if not hostname or not site_path:
                return {
                    "success": False,
                    "error": "Invalid URL. Expected format: https://hostname/sites/SiteName",
                }

            # Graph API: /sites/{hostname}:/{site-path}
            endpoint = f"/sites/{hostname}:{site_path}"
            try:
                result = await self._graph_request("GET", endpoint)
                if isinstance(result, dict):
                    return {
                        "success": True,
                        "site": {
                            "id": result.get("id", ""),
                            "name": result.get("displayName", result.get("name", "")),
                            "web_url": result.get("webUrl", ""),
                            "description": result.get("description", ""),
                        },
                    }
            except Exception as e:
                return {"success": False, "error": f"Cannot access site: {str(e)}"}

            return {"success": False, "error": "Site not found or no access"}

        elif name == "m365_list_files":
            site_id = args["site_id"]
            folder_path = args.get("folder_path", "")

            # Get default drive
            drives = await self._graph_request("GET", f"/sites/{site_id}/drives")
            if not isinstance(drives, dict) or not drives.get("value"):
                return {"success": False, "error": "No drives found"}
            drive_id = drives["value"][0]["id"]

            # List files
            if folder_path and folder_path != "/":
                folder_path = folder_path.strip("/")
                endpoint = (
                    f"/sites/{site_id}/drives/{drive_id}/root:/{folder_path}:/children"
                )
            else:
                endpoint = f"/sites/{site_id}/drives/{drive_id}/root/children"

            result = await self._graph_request("GET", endpoint, params={"$top": "100"})
            if isinstance(result, dict) and "value" in result:
                files = [
                    {
                        "id": item["id"],
                        "name": item.get("name", ""),
                        "type": "folder" if "folder" in item else "file",
                        "size": item.get("size", 0),
                        "web_url": item.get("webUrl", ""),
                        "modified": item.get("lastModifiedDateTime", ""),
                    }
                    for item in result["value"]
                ]
                return {"success": True, "files": files, "count": len(files)}
            return {"success": False, "error": "No files found"}

        elif name == "m365_read_file":
            site_id = args["site_id"]
            file_path = args["file_path"].strip("/")

            # Get default drive
            drives = await self._graph_request("GET", f"/sites/{site_id}/drives")
            if not isinstance(drives, dict) or not drives.get("value"):
                return {"success": False, "error": "No drives found"}
            drive_id = drives["value"][0]["id"]

            endpoint = f"/sites/{site_id}/drives/{drive_id}/root:/{file_path}:/content"
            content = await self._graph_request("GET", endpoint)

            if isinstance(content, bytes):
                try:
                    text = content.decode("utf-8")
                    if len(text) > 50_000:
                        text = text[:50_000] + "\n...(truncated)"
                except UnicodeDecodeError:
                    text = extract_text(content, file_path, max_chars=50_000)
                return {"success": True, "content": text, "file_path": file_path}
            return {"success": False, "error": "Could not read file"}

        elif name == "m365_read_shared":
            sharing_url = args.get("sharing_url")
            drive_id = args.get("drive_id")
            item_id = args.get("item_id")
            has_pair = bool(drive_id) and bool(item_id)
            if sharing_url and (drive_id or item_id):
                return {
                    "success": False,
                    "error": "provide only one of: sharing_url, or drive_id+item_id",
                }
            if not sharing_url and (bool(drive_id) != bool(item_id)):
                return {
                    "success": False,
                    "error": "drive_id and item_id must be supplied together",
                }
            if not sharing_url and not has_pair:
                return {
                    "success": False,
                    "error": "provide either sharing_url or both drive_id and item_id",
                }
            try:
                if sharing_url:
                    endpoint = f"/shares/{encode_sharing_url(sharing_url)}/driveItem"
                else:
                    endpoint = f"/drives/{drive_id}/items/{item_id}"
                metadata = await self._graph_request("GET", endpoint)
                if not isinstance(metadata, dict):
                    return {"success": False, "error": "could not resolve shared item"}
                content, fname = await self._read_item(metadata)
                text = extract_text(content, fname, max_chars=50_000)
                return {"success": True, "content": text, "name": fname}
            except SharedReadError as err:
                return {"success": False, "error": str(err)}
            except Exception as err:
                return {"success": False, "error": _graph_error_message(err, "read")}

        elif name == "m365_list_shared":
            items = []
            truncated = False
            endpoint = "/me/drive/sharedWithMe"
            try:
                for _page in range(_SHARED_MAX_PAGES):
                    data = await self._graph_request("GET", endpoint)
                    if not isinstance(data, dict):
                        break
                    for it in data.get("value", []):
                        remote = it.get("remoteItem") or {}
                        parent = remote.get("parentReference") or {}
                        # Graph reports the sharer under shared.sharedBy on
                        # sharedWithMe entries and leaves createdBy null there;
                        # createdBy stays as a fallback for other shapes.
                        shared = remote.get("shared") or {}
                        sharer = (shared.get("sharedBy") or {}).get("user") or {}
                        shared_by = sharer.get("displayName")
                        if not shared_by:
                            created = (
                                remote.get("createdBy") or it.get("createdBy") or {}
                            )
                            shared_by = (created.get("user") or {}).get("displayName")
                        items.append(
                            {
                                "name": it.get("name"),
                                "shared_by": shared_by,
                                "last_modified": it.get("lastModifiedDateTime"),
                                "drive_id": parent.get("driveId"),
                                "item_id": remote.get("id"),
                                "web_url": it.get("webUrl"),
                                "is_folder": remote.get("folder") is not None,
                            }
                        )
                        if len(items) >= _SHARED_MAX_ITEMS:
                            truncated = True
                            break
                    next_link = data.get("@odata.nextLink")
                    if truncated or not next_link:
                        break
                    endpoint = next_link  # absolute URL — httpx uses it as-is
                else:
                    # loop ran the full page cap without a break → more pages exist
                    truncated = True
                return {
                    "success": True,
                    "count": len(items),
                    "items": items,
                    "truncated": truncated,
                }
            except Exception as err:
                return {"success": False, "error": _graph_error_message(err, "list")}

        elif name == "m365_write_file":
            site_id = args["site_id"]
            file_path = args["file_path"].strip("/")
            content = args["content"]

            # Get default drive
            drives = await self._graph_request("GET", f"/sites/{site_id}/drives")
            if not isinstance(drives, dict) or not drives.get("value"):
                return {"success": False, "error": "No drives found"}
            drive_id = drives["value"][0]["id"]

            endpoint = f"/sites/{site_id}/drives/{drive_id}/root:/{file_path}:/content"
            result = await self._graph_request(
                "PUT",
                endpoint,
                data=content.encode("utf-8"),
                content_type="application/octet-stream",
            )

            if isinstance(result, dict):
                return {
                    "success": True,
                    "file_path": file_path,
                    "size": result.get("size", 0),
                    "web_url": result.get("webUrl", ""),
                }
            return {"success": False, "error": "Write failed"}

        elif name == "m365_search_files":
            query = args["query"]
            site_id = args.get("site_id")

            if site_id:
                endpoint = f"/sites/{site_id}/drive/root/search(q='{query}')"
                result = await self._graph_request(
                    "GET", endpoint, params={"$top": "25"}
                )

                if isinstance(result, dict) and "value" in result:
                    files = [
                        {
                            "id": item["id"],
                            "name": item.get("name", ""),
                            "web_url": item.get("webUrl", ""),
                            "type": "folder" if "folder" in item else "file",
                        }
                        for item in result["value"]
                    ]
                    return {"success": True, "files": files, "count": len(files)}
            return {"success": False, "error": "No results"}

        # Planner tools
        elif name == "m365_list_groups":
            # List groups the user is a member of (Microsoft 365 groups only)
            # Note: filter on memberOf is not supported, so we filter client-side
            result = await self._graph_request(
                "GET", "/me/memberOf", params={"$top": "100"}
            )
            if isinstance(result, dict) and "value" in result:
                groups = []
                for g in result["value"]:
                    # Only include Microsoft 365 groups (have 'Unified' in groupTypes)
                    if g.get("@odata.type") == "#microsoft.graph.group":
                        group_types = g.get("groupTypes", [])
                        if "Unified" in group_types:
                            groups.append(
                                {
                                    "id": g["id"],
                                    "name": g.get("displayName", ""),
                                    "description": g.get("description", ""),
                                    "mail": g.get("mail", ""),
                                }
                            )
                return {"success": True, "groups": groups, "count": len(groups)}
            return {"success": False, "error": "No groups found"}

        elif name == "m365_list_plans":
            group_id = args.get("group_id")
            list_all = args.get("list_all", False)

            if list_all:
                # Get all groups first, then get plans for each
                groups_result = await self._graph_request(
                    "GET", "/me/memberOf", params={"$top": "100"}
                )
                all_plans = []
                if isinstance(groups_result, dict) and "value" in groups_result:
                    for g in groups_result["value"]:
                        # Only process Microsoft 365 groups
                        if g.get("@odata.type") != "#microsoft.graph.group":
                            continue
                        if "Unified" not in g.get("groupTypes", []):
                            continue
                        try:
                            plans_result = await self._graph_request(
                                "GET", f"/groups/{g['id']}/planner/plans"
                            )
                            if (
                                isinstance(plans_result, dict)
                                and "value" in plans_result
                            ):
                                for p in plans_result["value"]:
                                    all_plans.append(
                                        {
                                            "id": p["id"],
                                            "title": p.get("title", ""),
                                            "group_name": g.get("displayName", ""),
                                            "group_id": g["id"],
                                        }
                                    )
                        except Exception:
                            continue  # Skip groups without planner access
                return {"success": True, "plans": all_plans, "count": len(all_plans)}

            # Single group or user's direct plans
            endpoint = (
                f"/groups/{group_id}/planner/plans" if group_id else "/me/planner/plans"
            )
            result = await self._graph_request("GET", endpoint)
            if isinstance(result, dict) and "value" in result:
                plans = [
                    {
                        "id": p["id"],
                        "title": p.get("title", ""),
                        "owner": p.get("owner", ""),
                    }
                    for p in result["value"]
                ]
                return {"success": True, "plans": plans, "count": len(plans)}
            return {"success": False, "error": "No plans found"}

        elif name == "m365_get_plan_by_id":
            # Get plan details by ID - useful for guest access to shared plans
            plan_id = args["plan_id"]

            try:
                # Get plan details
                plan = await self._graph_request("GET", f"/planner/plans/{plan_id}")
                if not isinstance(plan, dict):
                    return {"success": False, "error": "Plan not found or no access"}

                # Try to get buckets for context
                buckets = []
                try:
                    buckets_result = await self._graph_request(
                        "GET", f"/planner/plans/{plan_id}/buckets"
                    )
                    if isinstance(buckets_result, dict) and "value" in buckets_result:
                        buckets = [
                            {"id": b["id"], "name": b.get("name", "")}
                            for b in buckets_result["value"]
                        ]
                except Exception:
                    pass  # Buckets are optional

                return {
                    "success": True,
                    "plan": {
                        "id": plan.get("id", ""),
                        "title": plan.get("title", ""),
                        "owner": plan.get("owner", ""),
                        "created_by": plan.get("createdBy", {})
                        .get("user", {})
                        .get("displayName", ""),
                        "buckets": buckets,
                    },
                }
            except Exception as e:
                return {"success": False, "error": f"Cannot access plan: {str(e)}"}

        elif name == "m365_list_tasks":
            plan_id = args["plan_id"]
            bucket_id = args.get("bucket_id")

            result = await self._graph_request("GET", f"/planner/plans/{plan_id}/tasks")
            if isinstance(result, dict) and "value" in result:
                tasks = result["value"]
                if bucket_id:
                    tasks = [t for t in tasks if t.get("bucketId") == bucket_id]

                task_list = [
                    {
                        "id": t["id"],
                        "title": t.get("title", ""),
                        "percent_complete": t.get("percentComplete", 0),
                        "due_date": t.get("dueDateTime", ""),
                        "bucket_id": t.get("bucketId", ""),
                    }
                    for t in tasks
                ]
                return {"success": True, "tasks": task_list, "count": len(task_list)}
            return {"success": False, "error": "No tasks found"}

        elif name == "m365_get_task":
            task_id = args["task_id"]

            task = await self._graph_request("GET", f"/planner/tasks/{task_id}")
            details = await self._graph_request(
                "GET", f"/planner/tasks/{task_id}/details"
            )

            if isinstance(task, dict):
                result = {
                    "id": task.get("id", ""),
                    "title": task.get("title", ""),
                    "percent_complete": task.get("percentComplete", 0),
                    "due_date": task.get("dueDateTime", ""),
                    "plan_id": task.get("planId", ""),
                }
                if isinstance(details, dict):
                    result["description"] = details.get("description", "")
                return {"success": True, "task": result}
            return {"success": False, "error": "Task not found"}

        elif name == "m365_create_task":
            plan_id = args["plan_id"]
            title = args["title"]
            bucket_id = args.get("bucket_id")
            due_date = args.get("due_date")

            body: dict[str, Any] = {"planId": plan_id, "title": title}
            if bucket_id:
                body["bucketId"] = bucket_id
            if due_date:
                body["dueDateTime"] = due_date

            result = await self._graph_request("POST", "/planner/tasks", json_data=body)
            if isinstance(result, dict):
                return {
                    "success": True,
                    "task_id": result.get("id", ""),
                    "title": result.get("title", ""),
                }
            return {"success": False, "error": "Failed to create task"}

        elif name == "m365_complete_task":
            task_id = args["task_id"]

            # Get current task for ETag
            task = await self._graph_request("GET", f"/planner/tasks/{task_id}")
            if not isinstance(task, dict):
                return {"success": False, "error": "Task not found"}

            etag = task.get("@odata.etag", "")

            client = await self._get_client()
            token = await self._get_valid_token()
            response = await client.patch(
                f"/planner/tasks/{task_id}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "If-Match": etag,
                    "Content-Type": "application/json",
                },
                json={"percentComplete": 100},
            )

            if response.status_code in (200, 204):
                return {"success": True, "task_id": task_id, "completed": True}
            return {"success": False, "error": f"Failed: {response.status_code}"}

        # OneDrive tools
        elif name == "m365_list_drive_files":
            folder_path = args.get("folder_path", "")

            if folder_path and folder_path != "/":
                folder_path = folder_path.strip("/")
                endpoint = f"/me/drive/root:/{folder_path}:/children"
            else:
                endpoint = "/me/drive/root/children"

            result = await self._graph_request("GET", endpoint, params={"$top": "100"})
            if isinstance(result, dict) and "value" in result:
                files = [
                    {
                        "id": item["id"],
                        "name": item.get("name", ""),
                        "type": "folder" if "folder" in item else "file",
                        "size": item.get("size", 0),
                        "web_url": item.get("webUrl", ""),
                    }
                    for item in result["value"]
                ]
                return {"success": True, "files": files, "count": len(files)}
            return {"success": False, "error": "No files found"}

        elif name == "m365_read_drive_file":
            file_path = args["file_path"].strip("/")
            endpoint = f"/me/drive/root:/{file_path}:/content"

            content = await self._graph_request("GET", endpoint)
            if isinstance(content, bytes):
                try:
                    text = content.decode("utf-8")
                    if len(text) > 50_000:
                        text = text[:50_000] + "\n...(truncated)"
                except UnicodeDecodeError:
                    text = extract_text(content, file_path, max_chars=50_000)
                return {"success": True, "content": text, "file_path": file_path}
            return {"success": False, "error": "Could not read file"}

        elif name == "m365_write_drive_file":
            file_path = args["file_path"].strip("/")
            content = args["content"]

            endpoint = f"/me/drive/root:/{file_path}:/content"
            result = await self._graph_request(
                "PUT",
                endpoint,
                data=content.encode("utf-8"),
                content_type="application/octet-stream",
            )

            if isinstance(result, dict):
                return {
                    "success": True,
                    "file_path": file_path,
                    "size": result.get("size", 0),
                }
            return {"success": False, "error": "Write failed"}

        return {"success": False, "error": f"Unknown tool: {name}"}

    def _track_operation(self, name: str, args: dict, result: dict) -> None:
        """Track operation for context persistence."""
        if not result.get("success"):
            return

        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

            state = {}
            if STATE_FILE.exists():
                try:
                    with open(STATE_FILE, "r") as f:
                        state = json.load(f)
                except (json.JSONDecodeError, IOError):
                    state = {}

            user_key = f"ms365_{TENANT_NAME}"
            if user_key not in state:
                state[user_key] = {
                    "operations": [],
                    "working_file": None,
                    "working_task": None,
                }

            now = datetime.now(timezone.utc).isoformat()

            # Track operation
            op_record = {
                "operation": name.replace("m365_", ""),
                "tenant": TENANT_NAME,
                "timestamp": now,
            }

            # Add operation-specific data
            if name == "m365_list_sites":
                op_record["count"] = result.get("count", 0)
            elif name in ("m365_list_files", "m365_list_drive_files"):
                op_record["path"] = args.get("folder_path", "/")
                op_record["count"] = result.get("count", 0)
            elif name in ("m365_read_file", "m365_read_drive_file"):
                op_record["file_name"] = args.get("file_path", "").split("/")[-1]
                # Update working file
                state[user_key]["working_file"] = {
                    "name": op_record["file_name"],
                    "path": args.get("file_path", ""),
                    "site_id": args.get("site_id", ""),
                    "tenant": TENANT_NAME,
                    "timestamp": now,
                }
            elif name in ("m365_write_file", "m365_write_drive_file"):
                op_record["file_name"] = args.get("file_path", "").split("/")[-1]
            elif name == "m365_list_tasks":
                op_record["count"] = result.get("count", 0)
            elif name == "m365_create_task":
                op_record["title"] = args.get("title", "")
                op_record["task_id"] = result.get("task_id", "")
            elif name == "m365_get_task":
                task = result.get("task", {})
                state[user_key]["working_task"] = {
                    "task_id": task.get("id", ""),
                    "title": task.get("title", ""),
                    "percent_complete": task.get("percent_complete", 0),
                    "tenant": TENANT_NAME,
                    "timestamp": now,
                }
            elif name == "m365_complete_task":
                op_record["task_id"] = args.get("task_id", "")

            state[user_key]["operations"].insert(0, op_record)
            state[user_key]["operations"] = state[user_key]["operations"][:20]

            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)

        except Exception:
            pass  # Silent fail for tracking

    async def run(self):
        """Run the MCP server."""
        await self.mcp.run_stdio_async()


async def main():
    """Main entry point."""
    server = MS365Server()
    await server.run()


if __name__ == "__main__":
    asyncio.run(main())

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from config.logging_config import logger
from core.api_schemas import ApiResponse, api_error, api_ok
from core.encryption import decrypt, is_encrypted
from ui.config_manager import ConfigManager
from ui.routes.auth import require_login

router = APIRouter()
ADMIN_DIR = Path(__file__).resolve().parent.parent
BASE_DIR = ADMIN_DIR.parent
from ui.jinja_env import templates


async def _get_db():
    """Get database connection (available in both bot and UI containers)."""
    from core.registry import get_database

    return get_database()


async def _get_memory_service():
    """Get full memory service from plugin manager (bot container only).

    Returns None in the UI container — admin routes use direct SQL instead.
    """
    try:
        from core.interfaces.service import BaseMemoryService
        from core.registry import get_plugin_manager

        pm = get_plugin_manager()
        if pm:
            return pm.get_service_by_interface(BaseMemoryService)
    except Exception:
        pass
    return None


# =========================================================================
# MEMORY GROUPS - Configuration (no DB needed)
# =========================================================================


@router.get("/", response_class=HTMLResponse)
async def memory_page(request: Request, _: bool = Depends(require_login)):
    from ui.utils.channels import get_channel_ui_map

    config = ConfigManager()
    plugin_menus = getattr(request.state, "plugin_menus", [])
    return templates.TemplateResponse(
        request,
        "memory.html",
        {
            "request": request,
            "plugin_menus": plugin_menus,
            "memory_groups": config.get_memory_groups(),
            "user_identities": config.get_user_identities(),
            "channel_ui": get_channel_ui_map(),
        },
    )


@router.post("/group/add")
async def add_memory_group(
    request: Request,
    group_name: str = Form(...),
    members: str = Form(default=""),
    _: bool = Depends(require_login),
):
    config = ConfigManager()
    group_name = group_name.strip().lower()
    if group_name:
        member_list = [m.strip().lower() for m in members.split(",") if m.strip()]
        config.add_memory_group(group_name, member_list)
    return RedirectResponse(url="/memory", status_code=303)


@router.post("/group/{group_name}/add-member")
async def add_member_to_group(
    request: Request,
    group_name: str,
    member: str = Form(...),
    _: bool = Depends(require_login),
):
    config = ConfigManager()
    member = member.strip().lower()
    if member:
        config.add_user_to_memory_group(group_name, member)
    return RedirectResponse(url="/memory", status_code=303)


@router.post("/group/{group_name}/remove-member")
async def remove_member_from_group(
    request: Request,
    group_name: str,
    member: str = Form(...),
    _: bool = Depends(require_login),
):
    config = ConfigManager()
    config.remove_user_from_memory_group(group_name, member)
    return RedirectResponse(url="/memory", status_code=303)


@router.post("/group/{group_name}/delete")
async def delete_memory_group(
    request: Request,
    group_name: str,
    _: bool = Depends(require_login),
):
    config = ConfigManager()
    config.delete_memory_group(group_name)
    return RedirectResponse(url="/memory", status_code=303)


# =========================================================================
# MEMORY BROWSE - Direct SQL (no embedding model needed)
# =========================================================================


async def _get_stats(db, user_id=None, platform=None):
    """Get memory counts via direct SQL."""
    if user_id:
        ep = await db.fetch_one(
            "SELECT count(*) AS c FROM memory.episodic WHERE user_id = %s",
            (user_id,),
        )
        dc = await db.fetch_one(
            "SELECT count(*) AS c FROM memory.declarative WHERE user_id = %s",
            (user_id,),
        )
    elif platform:
        ep = await db.fetch_one(
            "SELECT count(*) AS c FROM memory.episodic WHERE platform = %s",
            (platform,),
        )
        dc = await db.fetch_one(
            "SELECT count(*) AS c FROM memory.declarative WHERE platform = %s",
            (platform,),
        )
    else:
        ep = await db.fetch_one("SELECT count(*) AS c FROM memory.episodic")
        dc = await db.fetch_one("SELECT count(*) AS c FROM memory.declarative")

    e = ep["c"] if ep else 0
    d = dc["c"] if dc else 0
    return {"episodic": e, "declarative": d, "total": e + d}


def _decrypt_field(value: str | None) -> str:
    """Decrypt a field value if encrypted, otherwise return as-is."""
    if not value:
        return value or ""
    return decrypt(value) if is_encrypted(value) else value


async def _get_all_memories(db, user_id=None, memory_type=None):
    """Fetch memories via direct SQL with optional filters."""
    results = []

    if memory_type in (None, "episodic"):
        q = (
            "SELECT id, user_id, platform, document, memory_type, "
            "user_message_preview, metadata, created_at "
            "FROM memory.episodic"
        )
        params = []
        if user_id:
            q += " WHERE user_id = %s"
            params.append(user_id)
        q += " ORDER BY created_at DESC"
        rows = await db.fetch_all(q, tuple(params) if params else None)
        for r in rows:
            results.append(
                {
                    "id": r["id"],
                    "user_id": r["user_id"],
                    "platform": r.get("platform", ""),
                    "document": _decrypt_field(r["document"]),
                    "memory_type": r.get("memory_type", "episodic"),
                    "user_message_preview": _decrypt_field(
                        r.get("user_message_preview", "")
                    ),
                    "created_at": str(r["created_at"]) if r.get("created_at") else "",
                }
            )

    if memory_type in (None, "declarative"):
        q = (
            "SELECT id, user_id, document, memory_type, metadata, created_at "
            "FROM memory.declarative"
        )
        params = []
        if user_id:
            q += " WHERE user_id = %s"
            params.append(user_id)
        q += " ORDER BY created_at DESC"
        rows = await db.fetch_all(q, tuple(params) if params else None)
        for r in rows:
            results.append(
                {
                    "id": r["id"],
                    "user_id": r["user_id"],
                    "platform": "",
                    "document": _decrypt_field(r["document"]),
                    "memory_type": r.get("memory_type", "declarative"),
                    "user_message_preview": "",
                    "created_at": str(r["created_at"]) if r.get("created_at") else "",
                }
            )

    return results


@router.get("/browse", response_class=HTMLResponse)
async def memory_browse_page(
    request: Request,
    user_id: str | None = Query(default=None),
    memory_type: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=10, le=100),
    _: bool = Depends(require_login),
):
    """Browse all memories with filtering and pagination."""
    plugin_menus = getattr(request.state, "plugin_menus", [])
    db = await _get_db()

    stats = {"episodic": 0, "declarative": 0, "total": 0}
    memories = []
    users = set()
    error = None

    if db:
        try:
            stats = await _get_stats(db)
            all_memories = await _get_all_memories(db, user_id, memory_type)

            for mem in all_memories:
                u = mem.get("user_id")
                if u:
                    users.add(u)

            total = len(all_memories)
            start = (page - 1) * per_page
            end = start + per_page
            memories = all_memories[start:end]
            total_pages = (total + per_page - 1) // per_page

        except Exception:
            logger.exception("Memory browse failed")
            error = "Memory browse failed"
            total = 0
            total_pages = 1
    else:
        error = "Database not available"
        total = 0
        total_pages = 1

    return templates.TemplateResponse(
        request,
        "memory_browse.html",
        {
            "request": request,
            "plugin_menus": plugin_menus,
            "stats": stats,
            "memories": memories,
            "users": sorted(users),
            "current_user_id": user_id,
            "current_memory_type": memory_type,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "error": error,
        },
    )


@router.get(
    "/api/search",
    response_model=ApiResponse[dict],
    response_model_exclude_none=True,
)
async def memory_search_api(
    request: Request,
    query: str = Query(..., min_length=2),
    user_id: str | None = Query(default=None),
    memory_type: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    _: bool = Depends(require_login),
):
    """Search memories by text (ILIKE). Semantic search only in bot container."""
    # Try full service first (bot container — has embedder for semantic search)
    memory_service = await _get_memory_service()
    if memory_service and memory_service.enabled:
        try:
            results = []
            if user_id:
                if memory_type in (None, "episodic"):
                    results.extend(
                        await memory_service.search_episodic(query, user_id, limit)
                    )
                if memory_type in (None, "declarative"):
                    results.extend(
                        await memory_service.search_declarative(query, user_id, limit)
                    )
            else:
                results = await memory_service.search_all_memories(
                    query, memory_type, limit
                )
            results.sort(key=lambda x: x.get("relevance", 0), reverse=True)
            return api_ok(data={"results": results[:limit]}, count=len(results))
        except Exception:
            logger.exception("Semantic memory search failed")

    # Fallback: decrypt-then-substring search (UI container — no embedder)
    db = await _get_db()
    if not db:
        return api_error(503, "Memory service not available", "unavailable")

    try:
        query_lower = query.lower()
        results = []
        fetch_limit = 500

        if memory_type in (None, "episodic"):
            rows = await db.fetch_all(
                "SELECT id, user_id, platform, document, memory_type, "
                "user_message_preview, created_at "
                "FROM memory.episodic "
                "ORDER BY created_at DESC LIMIT %s",
                (fetch_limit,),
            )
            for r in rows:
                doc = _decrypt_field(r["document"])
                if query_lower in doc.lower():
                    results.append(
                        {
                            "id": r["id"],
                            "user_id": r["user_id"],
                            "document": doc,
                            "memory_type": r.get("memory_type", "episodic"),
                            "created_at": str(r["created_at"])
                            if r.get("created_at")
                            else "",
                        }
                    )

        if memory_type in (None, "declarative"):
            rows = await db.fetch_all(
                "SELECT id, user_id, document, memory_type, created_at "
                "FROM memory.declarative "
                "ORDER BY created_at DESC LIMIT %s",
                (fetch_limit,),
            )
            for r in rows:
                doc = _decrypt_field(r["document"])
                if query_lower in doc.lower():
                    results.append(
                        {
                            "id": r["id"],
                            "user_id": r["user_id"],
                            "document": doc,
                            "memory_type": r.get("memory_type", "declarative"),
                            "created_at": str(r["created_at"])
                            if r.get("created_at")
                            else "",
                        }
                    )

        return JSONResponse({"results": results[:limit], "count": len(results)})
    except Exception:
        logger.exception("Memory text search failed")
        return api_error(500, "Search failed", "internal_error")


@router.post("/{memory_id}/delete")
async def delete_single_memory(
    request: Request,
    memory_id: str,
    memory_type: str = Form(default=None),
    redirect_url: str = Form(default="/memory/browse"),
    _: bool = Depends(require_login),
):
    """Delete a single memory by ID."""
    db = await _get_db()
    if not db:
        return RedirectResponse(
            url=redirect_url + "?error=service_unavailable", status_code=303
        )

    try:
        deleted = False
        if memory_type in (None, "episodic"):
            result = await db.execute(
                "DELETE FROM memory.episodic WHERE id = %s", (memory_id,)
            )
            if result and result > 0:
                deleted = True
        if memory_type in (None, "declarative") and not deleted:
            result = await db.execute(
                "DELETE FROM memory.declarative WHERE id = %s", (memory_id,)
            )
            if result and result > 0:
                deleted = True

        if deleted:
            return RedirectResponse(
                url=redirect_url + "?success=deleted", status_code=303
            )
        else:
            return RedirectResponse(
                url=redirect_url + "?error=not_found", status_code=303
            )
    except Exception:
        logger.exception("Memory delete failed")
        return RedirectResponse(
            url=redirect_url + "?error=delete_failed", status_code=303
        )


@router.get(
    "/api/stats",
    response_model=ApiResponse[dict],
    response_model_exclude_none=True,
)
async def memory_stats_api(
    request: Request,
    user_id: str | None = Query(default=None),
    _: bool = Depends(require_login),
):
    """Get memory statistics."""
    db = await _get_db()
    if not db:
        return api_error(503, "Memory service not available", "unavailable")

    try:
        if user_id:
            if ":" in user_id:
                stats = await _get_stats(db, user_id=user_id)
            else:
                return api_error(
                    400,
                    "user_id must be in 'platform:username' format",
                    "validation_error",
                )
        else:
            stats = await _get_stats(db)

        return api_ok(data=stats)
    except Exception:
        logger.exception("Memory stats failed")
        return api_error(500, "Stats unavailable", "internal_error")

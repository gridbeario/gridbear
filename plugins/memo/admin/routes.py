"""Admin routes for Memo plugin (PostgreSQL)."""

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ui.jinja_env import templates
from ui.plugin_helpers import get_plugin_template_context
from ui.routes.auth import require_login

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
PLUGIN_DIR = Path(__file__).resolve().parent.parent


async def get_user_map() -> dict:
    """Get user_id -> username mapping from chat_history."""
    from plugins.sessions.models import ChatHistory

    user_map = {}
    try:
        rows = await ChatHistory.raw_search(
            "SELECT DISTINCT user_id, platform, username FROM {table} "
            "WHERE username IS NOT NULL AND username != '' ORDER BY id DESC",
        )
        for row in rows:
            key = f"{row['user_id']}_{row['platform']}"
            if key not in user_map:
                user_map[key] = row["username"]
    except Exception:
        pass
    return user_map


async def get_all_memos() -> list[dict]:
    """Get all memos with prompt info."""
    from plugins.memo.models import ScheduledMemo

    rows = await ScheduledMemo.raw_search(
        "SELECT m.*, p.title as prompt_title, p.content as prompt_content "
        "FROM {table} m "
        "JOIN app.memo_prompts p ON m.prompt_id = p.id "
        "ORDER BY m.created_at DESC",
    )
    return [dict(r) for r in rows]


async def get_all_prompts() -> list[dict]:
    """Get all prompts with schedule count."""
    from plugins.memo.models import MemoPrompt

    rows = await MemoPrompt.raw_search(
        "SELECT p.*, "
        "(SELECT COUNT(*) FROM app.scheduled_memos WHERE prompt_id = p.id) as schedule_count "
        "FROM {table} p "
        "ORDER BY p.created_at DESC",
    )
    return [dict(r) for r in rows]


@router.get("", response_class=HTMLResponse)
async def memo_list(request: Request, _=Depends(require_login)):
    """List all memos and prompts."""
    memos = await get_all_memos()
    prompts = await get_all_prompts()
    user_map = await get_user_map()

    # Add username to memos
    for memo in memos:
        key = f"{memo['user_id']}_{memo['platform']}"
        memo["username"] = user_map.get(key, str(memo["user_id"]))

    return templates.TemplateResponse(
        request,
        "memo.html",
        get_plugin_template_context(
            request,
            PLUGIN_DIR,
            memos=memos,
            prompts=prompts,
        ),
    )


@router.get("/memo/{memo_id}", response_class=HTMLResponse)
async def memo_detail(request: Request, memo_id: int, _=Depends(require_login)):
    """Show memo detail for editing."""
    from plugins.memo.models import ScheduledMemo

    results = await ScheduledMemo.raw_search(
        "SELECT m.*, p.title as prompt_title, p.content as prompt_content "
        "FROM {table} m "
        "JOIN app.memo_prompts p ON m.prompt_id = p.id "
        "WHERE m.id = %s",
        (memo_id,),
    )
    if not results:
        raise HTTPException(status_code=404, detail="Memo not found")
    memo = dict(results[0])

    prompts = await get_all_prompts()
    user_map = await get_user_map()
    key = f"{memo['user_id']}_{memo['platform']}"
    memo["username"] = user_map.get(key, str(memo["user_id"]))

    return templates.TemplateResponse(
        request,
        "memo_edit.html",
        get_plugin_template_context(
            request,
            PLUGIN_DIR,
            memo=memo,
            prompts=prompts,
        ),
    )


@router.post("/memo/{memo_id}")
async def update_memo(
    request: Request,
    memo_id: int,
    prompt_id: int = Form(...),
    schedule_type: str = Form(...),
    cron: str = Form(None),
    run_at: str = Form(None),
    enabled: bool = Form(False),
    _=Depends(require_login),
):
    """Update a memo."""
    from plugins.memo.models import ScheduledMemo

    result = await ScheduledMemo.write(
        memo_id,
        prompt_id=prompt_id,
        schedule_type=schedule_type,
        cron=cron or None,
        run_at=run_at or None,
        enabled=enabled,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Memo not found")

    return RedirectResponse(url="/plugin/memo?saved=1", status_code=303)


@router.post("/memo/{memo_id}/toggle")
async def toggle_memo(request: Request, memo_id: int, _=Depends(require_login)):
    """Toggle memo enabled/disabled."""
    from plugins.memo.models import ScheduledMemo

    rows = await ScheduledMemo.search([("id", "=", memo_id)], limit=1)
    if not rows:
        raise HTTPException(status_code=404, detail="Memo not found")

    new_state = not rows[0]["enabled"]
    await ScheduledMemo.write(memo_id, enabled=new_state)

    return RedirectResponse(url="/plugin/memo?toggled=1", status_code=303)


@router.post("/memo/{memo_id}/delete")
async def delete_memo(request: Request, memo_id: int, _=Depends(require_login)):
    """Delete a memo."""
    from plugins.memo.models import ScheduledMemo

    deleted = await ScheduledMemo.delete(memo_id)
    if deleted == 0:
        raise HTTPException(status_code=404, detail="Memo not found")

    return RedirectResponse(url="/plugin/memo?deleted=1", status_code=303)

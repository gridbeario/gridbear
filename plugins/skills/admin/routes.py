"""Admin routes for Skills plugin (PostgreSQL)."""

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ui.jinja_env import templates
from ui.plugin_helpers import get_plugin_template_context
from ui.routes.auth import require_login

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
PLUGIN_DIR = Path(__file__).resolve().parent.parent

CATEGORIES = ["work", "personal", "automation", "report", "context", "other"]


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


async def _get_app_user_map() -> dict[int, str]:
    """Map app.users id → username for skill author display."""
    try:
        from core.models.user import User

        rows = await User.search([])
        return {r["id"]: r["username"] for r in rows}
    except Exception:
        return {}


async def get_all_skills() -> list[dict]:
    """Get all skills from database."""
    from plugins.skills.models import Skill

    rows = await Skill.search([], order="category, title")
    return [dict(r) for r in rows]


@router.get("", response_class=HTMLResponse)
async def skills_list(request: Request, _=Depends(require_login)):
    """List all skills."""
    skills = await get_all_skills()
    user_map = await get_user_map()

    # Add username to skills
    # Resolve from chat history (legacy) or app.users (portal users)
    app_user_map = await _get_app_user_map()
    for skill in skills:
        if skill.get("created_by"):
            uid = skill["created_by"]
            key = f"{uid}_{skill.get('created_by_platform', '')}"
            skill["username"] = user_map.get(key) or app_user_map.get(uid) or str(uid)
        else:
            skill["username"] = "System"

    # Split into user and context skills
    user_skills = [s for s in skills if s.get("skill_type") != "context"]
    context_skills = [s for s in skills if s.get("skill_type") == "context"]

    # Group user skills by category
    by_category = {}
    for skill in user_skills:
        cat = skill.get("category", "other")
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(skill)

    # Group context skills by plugin
    by_plugin = {}
    for skill in context_skills:
        plugin = skill.get("plugin_name") or "unknown"
        if plugin not in by_plugin:
            by_plugin[plugin] = []
        by_plugin[plugin].append(skill)

    return templates.TemplateResponse(
        request,
        "skills.html",
        get_plugin_template_context(
            request,
            PLUGIN_DIR,
            skills=user_skills,
            by_category=by_category,
            categories=CATEGORIES,
            context_skills=context_skills,
            by_plugin=by_plugin,
        ),
    )


@router.get("/new", response_class=HTMLResponse)
async def skill_new(request: Request, _=Depends(require_login)):
    """Show form to create a new skill."""
    return templates.TemplateResponse(
        request,
        "skill_edit.html",
        get_plugin_template_context(
            request,
            PLUGIN_DIR,
            skill=None,
            categories=CATEGORIES,
            is_new=True,
        ),
    )


@router.post("/new")
async def skill_create(
    request: Request,
    name: str = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    prompt: str = Form(...),
    category: str = Form("other"),
    shared: bool = Form(True),
    _=Depends(require_login),
):
    """Create a new skill."""
    from plugins.skills.models import Skill

    name = name.lower().replace(" ", "_").replace("-", "_")

    if await Skill.exists(name=name):
        raise HTTPException(status_code=400, detail=f"Skill '{name}' already exists")

    await Skill.create(
        name=name,
        title=title,
        description=description or None,
        prompt=prompt,
        category=category,
        shared=shared,
    )

    return RedirectResponse(url="/plugin/skills?saved=1", status_code=303)


@router.get("/{skill_id}", response_class=HTMLResponse)
async def skill_detail(request: Request, skill_id: int, _=Depends(require_login)):
    """Show skill detail for editing."""
    from plugins.skills.models import Skill

    results = await Skill.search([("id", "=", skill_id)], limit=1)
    if not results:
        raise HTTPException(status_code=404, detail="Skill not found")
    skill = dict(results[0])

    user_map = await get_user_map()
    app_user_map = await _get_app_user_map()
    if skill.get("created_by"):
        uid = skill["created_by"]
        key = f"{uid}_{skill.get('created_by_platform', '')}"
        skill["username"] = user_map.get(key) or app_user_map.get(uid) or str(uid)
    else:
        skill["username"] = "System"

    return templates.TemplateResponse(
        request,
        "skill_edit.html",
        get_plugin_template_context(
            request,
            PLUGIN_DIR,
            skill=skill,
            categories=CATEGORIES,
            is_new=False,
        ),
    )


@router.post("/{skill_id}")
async def skill_update(
    request: Request,
    skill_id: int,
    name: str = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    prompt: str = Form(...),
    category: str = Form("other"),
    shared: bool = Form(True),
    _=Depends(require_login),
):
    """Update a skill."""
    from plugins.skills.models import Skill

    name = name.lower().replace(" ", "_").replace("-", "_")

    # auto_now on updated_at handles the timestamp
    result = await Skill.write(
        skill_id,
        name=name,
        title=title,
        description=description or None,
        prompt=prompt,
        category=category,
        shared=shared,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Skill not found")

    return RedirectResponse(url="/plugin/skills?saved=1", status_code=303)


@router.post("/{skill_id}/delete")
async def skill_delete(request: Request, skill_id: int, _=Depends(require_login)):
    """Delete a skill."""
    from plugins.skills.models import Skill

    deleted = await Skill.delete(skill_id)
    if deleted == 0:
        raise HTTPException(status_code=404, detail="Skill not found")

    return RedirectResponse(url="/plugin/skills?deleted=1", status_code=303)


@router.post("/{skill_id}/reset")
async def skill_reset(request: Request, skill_id: int, _=Depends(require_login)):
    """Reset a context skill to its default prompt from the plugin .md file."""
    from core.registry import get_plugin_manager
    from plugins.skills.models import Skill

    results = await Skill.search([("id", "=", skill_id)], limit=1)
    if not results:
        raise HTTPException(status_code=404, detail="Skill not found")
    skill = dict(results[0])

    if skill.get("skill_type") != "context" or not skill.get("plugin_name"):
        raise HTTPException(status_code=400, detail="Not a context skill")

    pm = get_plugin_manager()
    if not pm:
        raise HTTPException(status_code=500, detail="Plugin manager not available")

    manifest = pm.get_plugin_manifest(skill["plugin_name"])
    if not manifest:
        raise HTTPException(status_code=404, detail="Plugin not found")

    # Find the matching context_skill definition
    context_skills = manifest.get("context_skills", [])
    default_prompt = None
    for skill_def in context_skills:
        normalized = skill_def["name"].lower().replace(" ", "_").replace("-", "_")
        if normalized == skill["name"]:
            md_path = pm.plugins_dir / skill["plugin_name"] / skill_def["file"]
            if md_path.exists():
                default_prompt = md_path.read_text(encoding="utf-8")
            break

    if default_prompt is None:
        raise HTTPException(status_code=404, detail="Default prompt file not found")

    # auto_now on updated_at handles the timestamp
    await Skill.write(skill_id, prompt=default_prompt)

    return RedirectResponse(url=f"/plugin/skills/{skill_id}?saved=1", status_code=303)

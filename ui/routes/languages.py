"""Admin routes for language management."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from config.logging_config import logger
from core.i18n import invalidate_language_cache
from ui.jinja_env import templates
from ui.routes.auth import require_login

router = APIRouter(prefix="/admin/languages", tags=["languages"])


async def _get_all_languages() -> list[dict]:
    """Fetch all languages from DB (active and inactive)."""
    try:
        from core.registry import get_database

        db = get_database()
        if not db:
            return []
        rows = await db.fetch_all(
            "SELECT code, name, active, direction, date_format, is_default "
            "FROM i18n.languages ORDER BY is_default DESC, code"
        )
        return [dict(r) for r in rows]
    except Exception as e:
        logger.debug("Could not fetch languages: %s", e)
        return []


@router.get("", response_class=HTMLResponse)
async def languages_page(request: Request, _=Depends(require_login)):
    """Language management page."""
    languages = await _get_all_languages()
    saved = request.query_params.get("saved")
    error = request.query_params.get("error")
    return templates.TemplateResponse(
        request,
        "languages.html",
        {
            "request": request,
            "languages": languages,
            "saved": saved,
            "error": error,
        },
    )


@router.post("/add")
async def add_language(
    request: Request,
    code: str = Form(...),
    name: str = Form(...),
    direction: str = Form("ltr"),
    _=Depends(require_login),
):
    """Add a new language."""
    code = code.strip().lower()
    name = name.strip()
    if not code or not name or len(code) > 10:
        return RedirectResponse("/admin/languages?error=invalid", status_code=303)

    try:
        from core.registry import get_database

        db = get_database()
        await db.execute(
            "INSERT INTO i18n.languages (code, name, direction) "
            "VALUES (%s, %s, %s) ON CONFLICT (code) DO NOTHING",
            (code, name, direction),
        )
        invalidate_language_cache()
    except Exception as e:
        logger.warning("Failed to add language %s: %s", code, e)
        return RedirectResponse("/admin/languages?error=db", status_code=303)

    return RedirectResponse("/admin/languages?saved=add", status_code=303)


@router.post("/{code}/toggle")
async def toggle_language(
    request: Request,
    code: str,
    _=Depends(require_login),
):
    """Toggle active state of a language."""
    from core.registry import get_database

    db = get_database()

    # Cannot deactivate the default language
    row = await db.fetch_one(
        "SELECT is_default, active FROM i18n.languages WHERE code = %s", (code,)
    )
    if not row:
        return RedirectResponse("/admin/languages?error=notfound", status_code=303)
    if row["is_default"] and row["active"]:
        return RedirectResponse(
            "/admin/languages?error=cannot_deactivate_default", status_code=303
        )

    await db.execute(
        "UPDATE i18n.languages SET active = NOT active WHERE code = %s", (code,)
    )
    invalidate_language_cache()
    return RedirectResponse("/admin/languages?saved=toggle", status_code=303)


@router.post("/{code}/default")
async def set_default_language(
    request: Request,
    code: str,
    _=Depends(require_login),
):
    """Set a language as the default."""
    from core.registry import get_database

    db = get_database()

    # Ensure the language exists and is active
    row = await db.fetch_one(
        "SELECT active FROM i18n.languages WHERE code = %s", (code,)
    )
    if not row or not row["active"]:
        return RedirectResponse("/admin/languages?error=notactive", status_code=303)

    # Unset previous default, set new one
    await db.execute("UPDATE i18n.languages SET is_default = FALSE")
    await db.execute(
        "UPDATE i18n.languages SET is_default = TRUE, active = TRUE WHERE code = %s",
        (code,),
    )
    invalidate_language_cache()
    return RedirectResponse("/admin/languages?saved=default", status_code=303)


@router.post("/{code}/delete")
async def delete_language(
    request: Request,
    code: str,
    _=Depends(require_login),
):
    """Delete a language (cannot delete default)."""
    from core.registry import get_database

    db = get_database()

    row = await db.fetch_one(
        "SELECT is_default FROM i18n.languages WHERE code = %s", (code,)
    )
    if not row:
        return RedirectResponse("/admin/languages?error=notfound", status_code=303)
    if row["is_default"]:
        return RedirectResponse(
            "/admin/languages?error=cannot_delete_default", status_code=303
        )

    await db.execute("DELETE FROM i18n.languages WHERE code = %s", (code,))
    invalidate_language_cache()
    return RedirectResponse("/admin/languages?saved=delete", status_code=303)

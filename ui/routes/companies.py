"""Admin routes for company (tenant) management."""

import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from config.logging_config import logger
from ui.auth.database import auth_db
from ui.jinja_env import templates
from ui.routes.auth import require_login

router = APIRouter()


def get_template_context(request: Request, **kwargs) -> dict:
    """Get base template context with enabled plugins and menus."""
    plugins = getattr(request.state, "plugins", {})
    plugin_menus = getattr(request.state, "plugin_menus", [])
    return {
        "request": request,
        "enabled_channels": plugins.get("channels", []),
        "enabled_services": plugins.get("services", []),
        "enabled_mcp": plugins.get("mcp", []),
        "enabled_runners": plugins.get("runners", []),
        "plugin_menus": plugin_menus,
        **kwargs,
    }


def _slugify(name: str) -> str:
    """Generate a URL-safe slug from a company name."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


# ── List all companies ─────────────────────────────────────────────


@router.get("/", response_class=HTMLResponse)
async def companies_page(request: Request, _: dict = Depends(require_login)):
    """List all companies."""
    from core.models.company import Company
    from core.models.company_user import CompanyUser

    companies = Company.search_sync([], order="name")
    for company in companies:
        company["member_count"] = CompanyUser.count_sync(
            [("company_id", "=", company["id"])]
        )

    return templates.TemplateResponse(
        request,
        "companies.html",
        get_template_context(
            request,
            companies=companies,
            success=request.query_params.get("success"),
            error=request.query_params.get("error"),
        ),
    )


# ── Switch active company (session) ───────────────────────────────


@router.post("/switch")
async def switch_company(
    request: Request,
    company_id: int = Form(...),
    _: dict = Depends(require_login),
):
    """Switch the active company in the admin session."""
    request.session["active_company_id"] = company_id
    referer = request.headers.get("referer", "/")
    return RedirectResponse(url=referer, status_code=303)


# ── Create company ────────────────────────────────────────────────


@router.post("/create")
async def create_company(
    request: Request,
    name: str = Form(...),
    slug: str = Form(default=""),
    _: dict = Depends(require_login),
):
    """Create a new company."""
    from core.models.company import Company

    name = name.strip()
    if not name:
        return RedirectResponse(url="/companies/?error=name_required", status_code=303)

    slug = slug.strip() or _slugify(name)
    if not slug:
        return RedirectResponse(url="/companies/?error=slug_required", status_code=303)

    try:
        Company.create_sync(name=name, slug=slug)
    except Exception as exc:
        logger.debug("Company creation failed: %s", exc)
        return RedirectResponse(
            url="/companies/?error=creation_failed", status_code=303
        )

    return RedirectResponse(url="/companies/?success=created", status_code=303)


# ── Company detail page ───────────────────────────────────────────


@router.get("/{id}", response_class=HTMLResponse)
async def company_detail(
    request: Request,
    id: int,
    _: dict = Depends(require_login),
):
    """Company detail page with member management."""
    from core.models.company import Company
    from core.models.company_user import CompanyUser

    company = Company.get_sync(id=id)
    if not company:
        return RedirectResponse(url="/companies/?error=not_found", status_code=303)

    from ui.config_manager import ConfigManager

    # Get members with user info
    cu_rows = CompanyUser.search_sync([("company_id", "=", id)])
    members = []
    member_user_ids = set()
    for row in cu_rows:
        user = auth_db.get_user_by_id(row["user_id"])
        members.append(
            {
                "company_user": row,
                "user": user or {"id": row["user_id"], "display_name": "Unknown"},
            }
        )
        member_user_ids.add(row["user_id"])

    # All users for the "Add Member" dropdown (exclude already-assigned)
    all_users = auth_db.get_all_users()
    available_users = [u for u in all_users if u["id"] not in member_user_ids]

    config = ConfigManager()
    return templates.TemplateResponse(
        request,
        "company_detail.html",
        get_template_context(
            request,
            company=company,
            members=members,
            available_users=available_users,
            available_locales=config.get_available_locales(),
            success=request.query_params.get("success"),
            error=request.query_params.get("error"),
        ),
    )


# ── Update company ────────────────────────────────────────────────


@router.post("/{id}/update")
async def update_company(
    request: Request,
    id: int,
    name: str = Form(...),
    slug: str = Form(default=""),
    locale: str = Form(default="en"),
    timezone: str = Form(default="UTC"),
    _: dict = Depends(require_login),
):
    """Update company settings."""
    from core.models.company import Company

    try:
        Company.write_sync(
            id,
            name=name.strip(),
            slug=slug.strip() or _slugify(name.strip()),
            locale=locale.strip(),
            timezone=timezone.strip(),
        )
    except Exception as exc:
        logger.debug("Company update failed: %s", exc)
        return RedirectResponse(
            url=f"/companies/{id}?error=update_failed", status_code=303
        )

    return RedirectResponse(url=f"/companies/{id}?success=updated", status_code=303)


# ── Delete (soft-delete) company ──────────────────────────────────


@router.post("/{id}/delete")
async def delete_company(
    request: Request,
    id: int,
    _: dict = Depends(require_login),
):
    """Soft-delete a company (set active=False)."""
    from core.models.company import Company

    # Prevent deleting the default company
    if id == 1:
        return RedirectResponse(
            url="/companies/?error=cannot_delete_default", status_code=303
        )

    try:
        Company.write_sync(id, active=False)
    except Exception as exc:
        logger.debug("Company deletion failed: %s", exc)
        return RedirectResponse(url="/companies/?error=delete_failed", status_code=303)

    return RedirectResponse(url="/companies/?success=deleted", status_code=303)


# ── Add member to company ─────────────────────────────────────────


@router.post("/{id}/members/add")
async def add_member(
    request: Request,
    id: int,
    user_id: int = Form(...),
    role: str = Form(default="member"),
    _: dict = Depends(require_login),
):
    """Add a user to a company."""
    from core.models.company_user import CompanyUser

    try:
        CompanyUser.create_sync(
            company_id=id,
            user_id=user_id,
            role=role.strip() or "member",
        )
    except Exception as exc:
        logger.debug("Add member failed: %s", exc)
        return RedirectResponse(
            url=f"/companies/{id}?error=add_member_failed", status_code=303
        )

    return RedirectResponse(
        url=f"/companies/{id}?success=member_added", status_code=303
    )


# ── Remove member from company ────────────────────────────────────


@router.post("/{id}/members/{uid}/remove")
async def remove_member(
    request: Request,
    id: int,
    uid: int,
    _: dict = Depends(require_login),
):
    """Remove a user from a company."""
    from core.models.company_user import CompanyUser

    try:
        CompanyUser.delete_multi_sync([("company_id", "=", id), ("user_id", "=", uid)])
    except Exception as exc:
        logger.debug("Remove member failed: %s", exc)
        return RedirectResponse(
            url=f"/companies/{id}?error=remove_member_failed", status_code=303
        )

    return RedirectResponse(
        url=f"/companies/{id}?success=member_removed", status_code=303
    )


# ── Change member role ────────────────────────────────────────────


@router.post("/{id}/members/{uid}/role")
async def change_member_role(
    request: Request,
    id: int,
    uid: int,
    role: str = Form(...),
    _: dict = Depends(require_login),
):
    """Change a member's role in a company."""
    from core.models.company_user import CompanyUser

    try:
        CompanyUser.write_multi_sync(
            [("company_id", "=", id), ("user_id", "=", uid)],
            role=role.strip(),
        )
    except Exception as exc:
        logger.debug("Change role failed: %s", exc)
        return RedirectResponse(
            url=f"/companies/{id}?error=role_update_failed", status_code=303
        )

    return RedirectResponse(
        url=f"/companies/{id}?success=role_updated", status_code=303
    )

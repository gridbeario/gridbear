"""User portal routes for Credential Vault management."""

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.credential_vault import (
    VaultCredential,
    VaultEntry,
    delete_service,
    get_service,
    list_services,
    save_service,
    validate_service_id,
)
from ui.jinja_env import templates
from ui.routes.auth import require_user

ADMIN_DIR = Path(__file__).resolve().parent.parent

router = APIRouter(prefix="/me/vault", tags=["vault"])


def _uid(user: dict) -> str:
    return user["username"]


@router.get("", response_class=HTMLResponse)
async def vault_list(request: Request, user: dict = Depends(require_user)):
    """List all vault services for this user."""
    uid = _uid(user)
    services = list_services(uid)
    return templates.TemplateResponse(
        request,
        "me/vault.html",
        {"request": request, "user": user, "services": services},
    )


@router.get("/add", response_class=HTMLResponse)
async def vault_add_form(request: Request, user: dict = Depends(require_user)):
    """Show the add-service form."""
    return templates.TemplateResponse(
        request,
        "me/vault_form.html",
        {
            "request": request,
            "user": user,
            "edit_mode": False,
            "entry": None,
            "credentials_json": [],
        },
    )


@router.post("/add")
async def vault_add(request: Request, user: dict = Depends(require_user)):
    """Create a new vault service."""
    uid = _uid(user)
    form = await request.form()
    service_id = form.get("service_id", "").strip()

    if not validate_service_id(service_id):
        services = list_services(uid)
        return templates.TemplateResponse(
            request,
            "me/vault.html",
            {
                "request": request,
                "user": user,
                "services": services,
                "error": f"Invalid service ID: '{service_id}'. Use lowercase letters, numbers, hyphens, underscores.",
            },
        )

    if get_service(uid, service_id) is not None:
        services = list_services(uid)
        return templates.TemplateResponse(
            request,
            "me/vault.html",
            {
                "request": request,
                "user": user,
                "services": services,
                "error": f"Service '{service_id}' already exists.",
            },
        )

    credentials = _parse_credentials_from_form(form)
    entry = VaultEntry(
        service_id=service_id,
        name=form.get("name", "").strip(),
        url=form.get("url", "").strip(),
        notes=form.get("notes", "").strip(),
        credentials=credentials,
    )
    save_service(uid, entry)
    return RedirectResponse(url="/me/vault", status_code=303)


@router.get("/{service_id}/edit", response_class=HTMLResponse)
async def vault_edit_form(
    request: Request,
    service_id: str,
    user: dict = Depends(require_user),
):
    """Show the edit-service form."""
    uid = _uid(user)
    entry = get_service(uid, service_id)
    if entry is None:
        return RedirectResponse(url="/me/vault", status_code=303)
    creds_for_js = [{"key": c.key, "secret": c.secret} for c in entry.credentials]
    return templates.TemplateResponse(
        request,
        "me/vault_form.html",
        {
            "request": request,
            "user": user,
            "edit_mode": True,
            "entry": entry,
            "credentials_json": creds_for_js,
        },
    )


@router.post("/{service_id}/edit")
async def vault_edit(
    request: Request,
    service_id: str,
    user: dict = Depends(require_user),
):
    """Update an existing vault service."""
    uid = _uid(user)
    existing = get_service(uid, service_id)
    if existing is None:
        return RedirectResponse(url="/me/vault", status_code=303)

    form = await request.form()
    credentials = _parse_credentials_from_form(form, existing=existing)

    entry = VaultEntry(
        service_id=service_id,
        name=form.get("name", "").strip(),
        url=form.get("url", "").strip(),
        notes=form.get("notes", "").strip(),
        credentials=credentials,
    )
    save_service(uid, entry)
    return RedirectResponse(url="/me/vault", status_code=303)


@router.post("/{service_id}/delete")
async def vault_delete(
    request: Request,
    service_id: str,
    user: dict = Depends(require_user),
):
    """Delete a vault service."""
    uid = _uid(user)
    delete_service(uid, service_id)
    return RedirectResponse(url="/me/vault", status_code=303)


def _parse_credentials_from_form(
    form,
    existing: VaultEntry | None = None,
) -> list[VaultCredential]:
    """Parse dynamic credential fields from the form.

    Fields are submitted as indexed arrays:
    - cred_key_0, cred_value_0, cred_secret_0
    - cred_key_1, cred_value_1, cred_secret_1
    - ...

    On edit, an empty value means "keep the existing value" for that key.
    """
    existing_map = {}
    if existing:
        existing_map = {c.key: c.value for c in existing.credentials}

    credentials = []
    idx = 0
    while True:
        key = form.get(f"cred_key_{idx}")
        if key is None:
            break
        key = key.strip()
        if not key:
            idx += 1
            continue
        value = form.get(f"cred_value_{idx}", "").strip()
        secret = form.get(f"cred_secret_{idx}") == "on"

        # On edit: empty value means keep existing
        if not value and key in existing_map:
            value = existing_map[key]

        credentials.append(VaultCredential(key=key, value=value, secret=secret))
        idx += 1
    return credentials

"""OAuth2 Client Management routes for Admin UI."""

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.oauth2.server import get_db
from ui.routes.auth import require_login

BASE_DIR = Path(__file__).resolve().parent.parent.parent

router = APIRouter(prefix="/oauth2")

_templates = None


def get_templates():
    global _templates
    if _templates is None:
        from ui.app import get_template_context, templates

        _templates = (templates, get_template_context)
    return _templates


@router.get("/clients", response_class=HTMLResponse)
async def list_clients(request: Request, _: dict = Depends(require_login)):
    """List all OAuth2 clients."""
    templates, get_ctx = get_templates()
    db = get_db()
    clients = db.list_clients(include_inactive=True)
    stats = db.get_stats()

    return templates.TemplateResponse(
        request,
        "oauth2/clients.html",
        get_ctx(request, clients=clients, stats=stats),
    )


@router.get("/clients/new", response_class=HTMLResponse)
async def new_client_form(request: Request, _: dict = Depends(require_login)):
    """Show form to create new OAuth2 client."""
    templates, get_ctx = get_templates()

    return templates.TemplateResponse(
        request,
        "oauth2/client_edit.html",
        get_ctx(request, client=None, is_new=True, secret=None),
    )


@router.post("/clients/new")
async def create_client(
    request: Request,
    _: dict = Depends(require_login),
    name: str = Form(...),
    client_type: str = Form(default="confidential"),
    redirect_uris: str = Form(default=""),
    allowed_scopes: str = Form(default="openid profile email mcp"),
    require_pkce: bool = Form(default=True),
    description: str = Form(default=""),
    access_token_expiry: int = Form(default=3600),
    refresh_token_expiry: int = Form(default=2592000),
):
    """Create a new OAuth2 client."""
    db = get_db()

    mcp_perms = None  # TODO: parse from form if provided

    client, plain_secret = db.create_client(
        name=name,
        client_type=client_type,
        redirect_uris=redirect_uris,
        allowed_scopes=allowed_scopes,
        require_pkce=require_pkce,
        description=description or None,
        access_token_expiry=access_token_expiry,
        refresh_token_expiry=refresh_token_expiry,
        mcp_permissions=mcp_perms,
    )

    # Redirect to client detail with secret shown once
    templates, get_ctx = get_templates()
    return templates.TemplateResponse(
        request,
        "oauth2/client_edit.html",
        get_ctx(
            request,
            client=client,
            is_new=False,
            secret=plain_secret,
            success="Client created successfully. Copy the secret now - it won't be shown again.",
        ),
    )


@router.get("/clients/{client_pk}", response_class=HTMLResponse)
async def view_client(
    request: Request, client_pk: int, _: dict = Depends(require_login)
):
    """View/edit an OAuth2 client."""
    db = get_db()
    client = db.get_client_by_id(client_pk)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    tokens = db.list_tokens_for_client(client_pk)

    templates, get_ctx = get_templates()
    return templates.TemplateResponse(
        request,
        "oauth2/client_edit.html",
        get_ctx(request, client=client, is_new=False, secret=None, tokens=tokens),
    )


@router.post("/clients/{client_pk}/update")
async def update_client(
    request: Request,
    client_pk: int,
    _: dict = Depends(require_login),
    name: str = Form(...),
    redirect_uris: str = Form(default=""),
    allowed_scopes: str = Form(default="openid profile email mcp"),
    require_pkce: bool = Form(default=True),
    description: str = Form(default=""),
    active: bool = Form(default=True),
    access_token_expiry: int = Form(default=3600),
    refresh_token_expiry: int = Form(default=2592000),
):
    """Update an OAuth2 client."""
    db = get_db()

    db.update_client(
        client_pk,
        name=name,
        redirect_uris=redirect_uris,
        allowed_scopes=allowed_scopes,
        require_pkce=require_pkce,
        description=description or None,
        active=active,
        access_token_expiry=access_token_expiry,
        refresh_token_expiry=refresh_token_expiry,
    )

    return RedirectResponse(
        url=f"/oauth2/clients/{client_pk}",
        status_code=303,
    )


@router.post("/clients/{client_pk}/regenerate-secret")
async def regenerate_secret(
    request: Request, client_pk: int, _: dict = Depends(require_login)
):
    """Regenerate client secret."""
    db = get_db()
    new_secret = db.regenerate_secret(client_pk)

    if not new_secret:
        raise HTTPException(
            status_code=400, detail="Cannot regenerate secret for this client"
        )

    client = db.get_client_by_id(client_pk)
    templates, get_ctx = get_templates()
    return templates.TemplateResponse(
        request,
        "oauth2/client_edit.html",
        get_ctx(
            request,
            client=client,
            is_new=False,
            secret=new_secret,
            success="Secret regenerated. Copy it now - it won't be shown again.",
        ),
    )


@router.post("/clients/{client_pk}/deactivate")
async def deactivate_client(
    request: Request, client_pk: int, _: dict = Depends(require_login)
):
    """Deactivate an OAuth2 client."""
    db = get_db()
    db.deactivate_client(client_pk)
    return RedirectResponse(url="/oauth2/clients", status_code=303)


@router.post("/tokens/{token_id}/revoke")
async def revoke_token(
    request: Request, token_id: int, _: dict = Depends(require_login)
):
    """Revoke a specific access token."""
    db = get_db()
    db.revoke_token(token_id)
    # Redirect back to the referring page
    referer = request.headers.get("referer", "/oauth2/clients")
    return RedirectResponse(url=referer, status_code=303)

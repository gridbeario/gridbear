"""GitHub MCP plugin admin routes."""

import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ui.csrf import validate_csrf_token
from ui.jinja_env import templates
from ui.plugin_helpers import load_plugin_config, save_plugin_config
from ui.routes.auth import require_login
from ui.routes.plugins import get_plugin_info, get_template_context
from ui.secrets_manager import secrets_manager


def parse_github_url(url: str) -> tuple[str, str]:
    """Parse GitHub URL/pattern into (owner, repo).

    Supports:
    - git@github.com:owner/repo.git
    - https://github.com/owner/repo
    - https://github.com/owner/repo.git
    - owner/repo
    - owner/*  (wildcard)

    Returns (owner, name) tuple.
    """
    url = url.strip()

    ssh_match = re.match(r"git@github\.com:([^/]+)/(.+?)(?:\.git)?$", url)
    if ssh_match:
        return ssh_match.group(1), ssh_match.group(2)

    https_match = re.match(r"https?://github\.com/([^/]+)/(.+?)(?:\.git)?/?$", url)
    if https_match:
        return https_match.group(1), https_match.group(2)

    simple_match = re.match(r"^([^/]+)/(.+)$", url)
    if simple_match:
        return simple_match.group(1), simple_match.group(2)

    raise ValueError(f"Invalid GitHub URL format: {url}")


router = APIRouter(prefix="/plugins/github", tags=["github"])

_GITHUB_DEFAULTS = {"toolsets": "all", "read_only": False, "repos": []}


def get_github_config() -> dict:
    """Get GitHub MCP configuration."""
    return {**_GITHUB_DEFAULTS, **load_plugin_config("github")}


def save_github_config(github_config: dict) -> None:
    """Save GitHub MCP configuration."""
    save_plugin_config("github", github_config)


@router.get("", response_class=HTMLResponse)
async def github_index(request: Request, user: dict = Depends(require_login)):
    """GitHub MCP plugin configuration page."""
    config = get_github_config()

    has_token = secrets_manager.get("GITHUB_TOKEN") is not None

    plugin_info = get_plugin_info("github")

    return templates.TemplateResponse(
        request,
        "plugins/github.html",
        get_template_context(
            request,
            plugin=plugin_info,
            plugin_name="github",
            encryption_available=secrets_manager.is_available(),
            plugin_dependencies=plugin_info.get("dependencies", {})
            if plugin_info
            else {},
            plugin_dependents=[],
            config=config,
            repos=config.get("repos", []),
            has_token=has_token,
        ),
    )


@router.post("/settings")
async def save_settings(
    request: Request,
    toolsets: str = Form("all"),
    read_only: bool = Form(False),
    github_token: str = Form(""),
    csrf_token: str = Form(...),
    user: dict = Depends(require_login),
):
    """Save global settings."""
    validate_csrf_token(request, csrf_token)

    config = get_github_config()
    config["toolsets"] = toolsets
    config["read_only"] = read_only
    save_github_config(config)

    if github_token.strip():
        secrets_manager.set("GITHUB_TOKEN", github_token.strip())

    return RedirectResponse(url="/plugins/github?saved=settings", status_code=303)


@router.get("/repo/add", response_class=HTMLResponse)
async def add_repo_form(request: Request, user: dict = Depends(require_login)):
    """Show add repository form."""
    return templates.TemplateResponse(
        request,
        "plugins/github_repo.html",
        get_template_context(
            request,
            plugin_name="github",
            parent_title="GitHub MCP",
            repo=None,
            is_new=True,
        ),
    )


@router.get("/repo/{repo_id}", response_class=HTMLResponse)
async def edit_repo_form(
    request: Request,
    repo_id: str,
    user: dict = Depends(require_login),
):
    """Show edit repository form."""
    config = get_github_config()

    repo = None
    for r in config.get("repos", []):
        if r.get("id") == repo_id:
            repo = r
            break

    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")

    return templates.TemplateResponse(
        request,
        "plugins/github_repo.html",
        get_template_context(
            request,
            plugin_name="github",
            parent_title="GitHub MCP",
            repo=repo,
            is_new=False,
        ),
    )


@router.post("/repo")
async def save_repo(
    request: Request,
    repo_url: str = Form(...),
    repo_id: str = Form(""),
    protected_branches: str = Form("main,master"),
    allow_direct_push: bool = Form(False),
    allowed_agents: str = Form("*"),
    is_new: str = Form("false"),
    csrf_token: str = Form(...),
    user: dict = Depends(require_login),
):
    """Save repository configuration."""
    validate_csrf_token(request, csrf_token)

    try:
        owner, name = parse_github_url(repo_url)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid GitHub URL format")

    pattern = f"{owner}/{name}"

    if not repo_id.strip():
        repo_id = name.replace("_", "-").replace(".", "-").lower()

    config = get_github_config()
    repos = config.get("repos", [])

    protected_list = [b.strip() for b in protected_branches.split(",") if b.strip()]

    if allowed_agents.strip() == "*":
        allowed_agents_list = ["*"]
    elif allowed_agents.strip() == "":
        allowed_agents_list = []
    else:
        allowed_agents_list = [
            a.strip() for a in allowed_agents.split(",") if a.strip()
        ]

    repo_config = {
        "id": repo_id,
        "pattern": pattern,
        "owner": owner,
        "name": name,
        "protected_branches": protected_list,
        "allow_direct_push": allow_direct_push,
        "allowed_agents": allowed_agents_list,
    }

    if is_new == "true":
        for r in repos:
            if r.get("id") == repo_id:
                raise HTTPException(
                    status_code=400, detail="Repository ID already exists"
                )
        repos.append(repo_config)
    else:
        for i, r in enumerate(repos):
            if r.get("id") == repo_id:
                repos[i] = repo_config
                break

    config["repos"] = repos
    save_github_config(config)

    return RedirectResponse(url="/plugins/github?saved=repo", status_code=303)


@router.post("/repo/{repo_id}/delete")
async def delete_repo(
    request: Request,
    repo_id: str,
    csrf_token: str = Form(...),
    user: dict = Depends(require_login),
):
    """Delete a repository configuration."""
    validate_csrf_token(request, csrf_token)

    config = get_github_config()
    repos = config.get("repos", [])

    config["repos"] = [r for r in repos if r.get("id") != repo_id]
    save_github_config(config)

    return RedirectResponse(url="/plugins/github?deleted=repo", status_code=303)

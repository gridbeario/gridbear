"""Admin routes for LiveKit plugin."""

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from config.logging_config import logger
from ui.csrf import validate_csrf_token
from ui.jinja_env import templates
from ui.plugin_helpers import (
    get_plugin_template_context,
    load_plugin_config,
    save_plugin_config,
)
from ui.routes.auth import require_login
from ui.secrets_manager import secrets_manager

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
PLUGIN_DIR = Path(__file__).resolve().parent.parent

# PostgreSQL database
_db = None


def _ensure_db():
    """Get the DatabaseManager from the registry."""
    global _db
    if _db is None:
        from core.registry import get_database

        _db = get_database()
        if _db is None:
            raise RuntimeError("DatabaseManager not available")
    return _db


def _get_session_from_db(room_name: str) -> dict | None:
    """Get session from PostgreSQL database."""
    try:
        db = _ensure_db()
        with db.acquire_sync() as conn:
            row = conn.execute(
                "SELECT room_name, user_id, user_name, user_token, agent_token, "
                "ws_url, created_at, cleanup_token "
                "FROM app.livekit_sessions WHERE room_name = %s AND ended_at IS NULL",
                (room_name,),
            ).fetchone()
            if row:
                result = dict(row)
                result["cleanup_token"] = result.get("cleanup_token") or ""
                return result
    except Exception as e:
        logger.warning(f"Failed to get session from DB: {e}")
    return None


# Service reference
_service = None

# Rate limiting - track calls per user
_call_rate_limit: dict[str, list[float]] = {}
RATE_LIMIT_CALLS = 5
RATE_LIMIT_WINDOW = 60  # seconds


def set_service(service) -> None:
    """Set the LiveKit service reference."""
    global _service
    _service = service


def _check_rate_limit(user_id: str) -> bool:
    """Check if user is within rate limit."""
    import time

    now = time.time()
    if user_id not in _call_rate_limit:
        _call_rate_limit[user_id] = []

    # Remove old entries
    _call_rate_limit[user_id] = [
        t for t in _call_rate_limit[user_id] if now - t < RATE_LIMIT_WINDOW
    ]

    if len(_call_rate_limit[user_id]) >= RATE_LIMIT_CALLS:
        return False

    _call_rate_limit[user_id].append(now)
    return True


@router.get("", response_class=HTMLResponse)
async def livekit_page(request: Request, _=Depends(require_login)):
    """LiveKit admin page."""
    active_calls = []
    service_enabled = False

    # Check secrets status
    has_api_key = secrets_manager.get("LIVEKIT_API_KEY", fallback_env=True) is not None
    has_api_secret = (
        secrets_manager.get("LIVEKIT_API_SECRET", fallback_env=True) is not None
    )

    # Load config
    config = load_plugin_config("livekit-agent")
    ws_url = config.get("ws_url", "")

    if _service:
        service_enabled = await _service.health_check()
        active_calls = await _service.list_active_calls()

    return templates.TemplateResponse(
        request,
        "livekit.html",
        get_plugin_template_context(
            request,
            PLUGIN_DIR,
            active_calls=active_calls,
            service_enabled=service_enabled,
            has_api_key=has_api_key,
            has_api_secret=has_api_secret,
            ws_url=ws_url,
            config=config,
        ),
    )


@router.post("/settings")
async def save_settings(
    request: Request,
    livekit_api_key: str = Form(""),
    livekit_api_secret: str = Form(""),
    ws_url: str = Form(""),
    csrf_token: str = Form(...),
    _=Depends(require_login),
):
    """Save LiveKit settings."""
    validate_csrf_token(request, csrf_token)

    # Save secrets if provided
    if livekit_api_key.strip():
        secrets_manager.set("LIVEKIT_API_KEY", livekit_api_key.strip())
        logger.info("LIVEKIT_API_KEY saved")

    if livekit_api_secret.strip():
        secrets_manager.set("LIVEKIT_API_SECRET", livekit_api_secret.strip())
        logger.info("LIVEKIT_API_SECRET saved")

    # Save config if provided
    if ws_url.strip():
        cfg = load_plugin_config("livekit-agent")
        cfg["ws_url"] = ws_url.strip()
        save_plugin_config("livekit-agent", cfg)
        logger.info(f"LiveKit ws_url saved: {ws_url.strip()}")

    return RedirectResponse(url="/plugin/livekit-agent?saved=1", status_code=303)


@router.post("/reload")
async def reload_plugin(
    request: Request,
    csrf_token: str = Form(...),
    _=Depends(require_login),
):
    """Request plugin reload."""
    validate_csrf_token(request, csrf_token)
    logger.info("LiveKit plugin reload requested - restart gridbear container to apply")
    return RedirectResponse(
        url="/plugin/livekit-agent?reload_requested=1", status_code=303
    )


@router.post("/call/start")
async def start_call(
    request: Request,
    user_id: str = None,
    agent_id: str = "myagent",
    _=Depends(require_login),
):
    """Avvia una chiamata con l'agente."""
    if not _service:
        raise HTTPException(status_code=503, detail="LiveKit service not available")

    # Get user from session or parameter
    if not user_id:
        user_id = getattr(request.state, "user_id", "anonymous")

    # Rate limiting
    if not _check_rate_limit(user_id):
        raise HTTPException(
            status_code=429,
            detail="Troppe richieste. Riprova tra un minuto.",
        )

    # Check for existing call
    existing = await _service.get_active_call_for_user(user_id)
    if existing:
        raise HTTPException(
            status_code=409,
            detail="Hai già una chiamata attiva",
        )

    try:
        session = await _service.create_call(user_id, agent_id)

        return {
            "room": session.room_name,
            "token": session.participant_token,
            "ws_url": session.ws_url,
            "call_url": f"/plugin/livekit-agent/call/{session.room_name}",
        }
    except Exception as e:
        logger.error(f"Failed to create call: {e}")
        raise HTTPException(status_code=500, detail="Failed to create call")


@router.post("/call/{room_name}/end")
async def end_call(room_name: str, _=Depends(require_login)):
    """Termina una chiamata."""
    if not _service:
        raise HTTPException(status_code=503, detail="LiveKit service not available")

    success = await _service.end_call(room_name)
    if not success:
        raise HTTPException(status_code=404, detail="Call not found")

    return {"status": "ended", "room": room_name}


@router.post("/call/{room_name}/heartbeat")
async def heartbeat(room_name: str, _=Depends(require_login)):
    """Estende la sessione della chiamata."""
    if not _service:
        raise HTTPException(status_code=503, detail="LiveKit service not available")

    success = await _service.heartbeat(room_name)
    return {"status": "ok" if success else "not_found"}


@router.get("/call/active")
async def list_active_calls(_=Depends(require_login)):
    """Lista chiamate attive."""
    if not _service:
        raise HTTPException(status_code=503, detail="LiveKit service not available")

    calls = await _service.list_active_calls()
    return {
        "calls": [
            {
                "room_name": c.room_name,
                "user_id": c.user_id,
                "agent_id": c.agent_id,
                "created_at": c.created_at,
            }
            for c in calls
        ]
    }


def _get_agent_info(agent_id: str) -> dict:
    """Get agent display name and avatar URL from DB config."""
    try:
        from core.models.agent_config import AgentConfigRecord

        record = AgentConfigRecord.get_sync(id=agent_id)
        if record:
            avatar = record.get("avatar", "")
            return {
                "name": record.get("name", agent_id.capitalize()),
                "avatar": f"/static/avatars/{avatar}" if avatar else "",
            }
    except Exception:
        pass
    return {"name": agent_id.capitalize(), "avatar": ""}


@router.get("/call/{room_name}", response_class=HTMLResponse)
async def call_page(room_name: str, request: Request):
    """Pagina per partecipare a una chiamata (no auth required)."""
    # Get session from PostgreSQL database (shared between containers)
    session = _get_session_from_db(room_name)
    if session:
        # DB doesn't store agent_id; find first configured agent
        agent_info = {"name": "GridBear", "avatar": ""}
        try:
            from core.models.agent_config import AgentConfigRecord

            records = AgentConfigRecord.search_sync([("is_active", "=", True)])
            if records:
                agent_info = _get_agent_info(records[0].get("id", ""))
        except Exception:
            pass

        return templates.TemplateResponse(
            request,
            "call_standalone.html",
            {
                "request": request,
                "room_name": room_name,
                "token": session["user_token"],
                "ws_url": session["ws_url"],
                "agent_name": agent_info["name"],
                "agent_avatar": agent_info["avatar"],
                "cleanup_token": session.get("cleanup_token", ""),
            },
        )

    # Fall back to service if available (same container scenario)
    if _service:
        sessions = await _service.list_active_calls()
        svc_session = next((s for s in sessions if s.room_name == room_name), None)
        if svc_session:
            agent_info = _get_agent_info(svc_session.agent_id)
            return templates.TemplateResponse(
                request,
                "call_standalone.html",
                {
                    "request": request,
                    "room_name": room_name,
                    "token": svc_session.participant_token,
                    "ws_url": svc_session.ws_url,
                    "agent_name": agent_info["name"],
                    "agent_avatar": agent_info["avatar"],
                },
            )

    return templates.TemplateResponse(
        request,
        "call_standalone.html",
        {
            "request": request,
            "room_name": room_name,
            "token": "",
            "ws_url": "",
            "agent_name": "GridBear",
            "agent_avatar": "",
            "expired": True,
        },
    )


@router.post("/call/{room_name}/cleanup")
async def cleanup_call(room_name: str, request: Request):
    """Cleanup session when user leaves. Requires cleanup_token."""
    # Accept token from query param (sendBeacon) or JSON body
    token = request.query_params.get("t", "")
    if not token:
        try:
            body = await request.json()
            token = body.get("cleanup_token", "")
        except Exception:
            pass

    if not token:
        return JSONResponse({"error": "cleanup_token required"}, status_code=403)

    # Validate token and mark session as ended
    try:
        db = _ensure_db()
        with db.acquire_sync() as conn:
            row = conn.execute(
                "SELECT cleanup_token FROM app.livekit_sessions "
                "WHERE room_name = %s AND ended_at IS NULL",
                (room_name,),
            ).fetchone()
            if not row or row["cleanup_token"] != token:
                return JSONResponse({"error": "invalid token"}, status_code=403)

            conn.execute(
                "UPDATE app.livekit_sessions SET ended_at = %s, end_reason = %s "
                "WHERE room_name = %s",
                (datetime.now(), "user_left", room_name),
            )
            logger.info(f"Session ended: {room_name}")
    except Exception as e:
        logger.warning(f"Failed to cleanup session {room_name}: {e}")

    # Also try via service if available
    if _service:
        try:
            await _service.end_call(room_name)
        except Exception:
            pass

    return {"status": "ok"}

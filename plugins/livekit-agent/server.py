"""LiveKit Agent MCP Server.

Provides tools for starting and managing voice calls via LiveKit.
Runs as a standalone stdio subprocess — uses psycopg directly (no app registry).
"""

import asyncio
import os
import secrets
import time
from datetime import datetime, timedelta
from typing import Annotated

import psycopg

# LiveKit SDK
from livekit import api

# MCP SDK
from mcp.server.fastmcp import FastMCP
from psycopg.rows import dict_row
from pydantic import Field

# Configuration from environment
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")
LIVEKIT_WS_URL = os.getenv("LIVEKIT_WS_URL", "")
AGENT_NAME = os.getenv("AGENT_NAME", "My Agent")
STT_LANGUAGE = os.getenv("STT_LANGUAGE", "it")
TTS_VOICE = os.getenv("TTS_VOICE", "nova")
BASE_URL = os.getenv("BASE_URL", "") or os.getenv("GRIDBEAR_BASE_URL", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")


def _get_conn():
    """Get a psycopg connection with dict rows and autocommit."""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not configured")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True)


def _save_session(room_name: str, data: dict):
    """Save session to database."""
    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO app.livekit_sessions
            (room_name, user_id, user_name, user_token, agent_token, ws_url,
             cleanup_token, caller_identity, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (room_name) DO UPDATE SET
                user_id = EXCLUDED.user_id,
                user_name = EXCLUDED.user_name,
                user_token = EXCLUDED.user_token,
                agent_token = EXCLUDED.agent_token,
                ws_url = EXCLUDED.ws_url,
                cleanup_token = EXCLUDED.cleanup_token,
                caller_identity = EXCLUDED.caller_identity
            """,
            (
                room_name,
                data["user_id"],
                data.get("user_name", ""),
                data["user_token"],
                data["agent_token"],
                data["ws_url"],
                data.get("cleanup_token", ""),
                data.get("caller_identity"),
                data["created_at"],
            ),
        )
        row = conn.execute(
            "SELECT room_name FROM app.livekit_sessions WHERE room_name = %s",
            (room_name,),
        ).fetchone()
        if not row:
            raise RuntimeError(f"Session {room_name} not found after save")


def _get_session(room_name: str) -> dict | None:
    """Get session from database."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM app.livekit_sessions WHERE room_name = %s",
            (room_name,),
        ).fetchone()
    return row


def _get_session_by_user(user_id: str) -> dict | None:
    """Get session by user_id."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM app.livekit_sessions WHERE user_id = %s",
            (user_id,),
        ).fetchone()
    return row


def _delete_session(room_name: str):
    """Delete session from database."""
    with _get_conn() as conn:
        conn.execute(
            "DELETE FROM app.livekit_sessions WHERE room_name = %s",
            (room_name,),
        )


def _get_all_sessions() -> list[dict]:
    """Get all sessions."""
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM app.livekit_sessions").fetchall()
    return rows


mcp = FastMCP("livekit-agent")


def _create_token(identity: str, name: str, room: str, is_admin: bool = False) -> str:
    """Create a LiveKit access token."""
    token = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name(name)
        .with_ttl(timedelta(hours=1))
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room,
                room_admin=is_admin,
            )
        )
    )
    return token.to_jwt()


def _config_error() -> str | None:
    """Every tool here needs LiveKit credentials; report the same message once."""
    if not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET or not LIVEKIT_WS_URL:
        return "Errore: LiveKit non configurato. Mancano API_KEY, API_SECRET o WS_URL."
    return None


@mcp.tool(
    description=(
        "Avvia una chiamata vocale real-time con l'utente corrente. "
        "Restituisce l'URL della room LiveKit. "
        "IMPORTANTE: Mostra l'URL esattamente come restituito, senza formattazione markdown, "
        "senza grassetto, senza parentesi quadre. L'utente deve poterlo copiare e incollare."
    ),
    structured_output=False,
)
async def start_voice_call(
    user_id: Annotated[
        str,
        Field(description="ID dell'utente (es: telegram_123456, discord_username)"),
    ],
    user_name: Annotated[
        str | None,
        Field(description="Nome visualizzato dell'utente nella chiamata"),
    ] = None,
    caller_identity: Annotated[
        str | None,
        Field(
            description=(
                "Identità del chiamante nel formato platform:username "
                "(es: telegram:johndoe, discord:janedoe). "
                "Necessario per i permessi MCP durante la chiamata."
            )
        ),
    ] = None,
) -> str:
    """Start a voice call."""
    if err := _config_error():
        return err
    if not user_id:
        return "Errore: user_id richiesto"
    user_name = user_name or user_id

    # Check for existing call — reuse if token still valid (< 50 min)
    existing = _get_session_by_user(user_id)
    if existing:
        try:
            created = datetime.fromisoformat(str(existing["created_at"]))
            age_minutes = (datetime.now() - created).total_seconds() / 60
        except Exception:
            age_minutes = 999
        if age_minutes < 50:
            return f"{BASE_URL}/plugin/livekit-agent/call/{existing['room_name']}"
        # Token expired — clean up old session
        _delete_session(existing["room_name"])

    # Create new room
    room_name = f"call-{user_id.replace('@', '').replace(' ', '-')}-{int(time.time())}"

    # Generate tokens
    user_token = _create_token(user_id, user_name, room_name, is_admin=False)
    agent_token = _create_token(
        f"agent-{AGENT_NAME.lower()}", AGENT_NAME, room_name, is_admin=True
    )

    # Store session in PostgreSQL
    cleanup_token = secrets.token_urlsafe(16)
    try:
        _save_session(
            room_name,
            {
                "user_id": user_id,
                "user_name": user_name,
                "user_token": user_token,
                "agent_token": agent_token,
                "ws_url": LIVEKIT_WS_URL,
                "cleanup_token": cleanup_token,
                "caller_identity": caller_identity,
                "created_at": datetime.now().isoformat(),
            },
        )
    except Exception as e:
        return f"Errore nel salvare la sessione: {e}"

    # Agent joins automatically via auto-dispatch when user connects to the room
    # Simple URL - admin will look up token from database
    return f"{BASE_URL}/plugin/livekit-agent/call/{room_name}"


@mcp.tool(description="Termina una chiamata vocale attiva.", structured_output=False)
async def end_voice_call(
    room_name: Annotated[str, Field(description="Nome della room da terminare")],
) -> str:
    """End a voice call."""
    if err := _config_error():
        return err
    if not room_name:
        return "Errore: room_name richiesto"

    session = _get_session(room_name)
    if not session:
        return f"Chiamata non trovata: {room_name}"

    # Try to delete room via LiveKit API
    try:
        room_service = api.RoomService(
            LIVEKIT_WS_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET
        )
        await room_service.delete_room(api.DeleteRoomRequest(room=room_name))
    except Exception:
        pass  # Room might not exist on server

    # Remove from database
    _delete_session(room_name)

    return f"Chiamata terminata: {room_name}"


@mcp.tool(
    description="Elenca tutte le chiamate vocali attive.", structured_output=False
)
async def list_active_calls() -> str:
    """List active calls."""
    if err := _config_error():
        return err
    sessions = _get_all_sessions()
    if not sessions:
        return "Nessuna chiamata attiva."

    lines = ["Chiamate attive:\n"]
    for session in sessions:
        lines.append(
            f"- Room: {session['room_name']}\n"
            f"  Utente: {session.get('user_name') or session.get('user_id')}\n"
            f"  Creata: {session.get('created_at', 'N/A')}\n"
        )

    return "\n".join(lines)


@mcp.tool(
    description="Ottiene il link per una chiamata esistente.", structured_output=False
)
async def get_call_link(
    user_id: Annotated[
        str, Field(description="ID dell'utente per cui cercare la chiamata")
    ],
) -> str:
    """Get call link for a user."""
    if err := _config_error():
        return err
    if not user_id:
        return "Errore: user_id richiesto"

    session = _get_session_by_user(user_id)
    if session:
        return f"{BASE_URL}/plugin/livekit-agent/call/{session['room_name']}"

    return f"Nessuna chiamata attiva per {user_id}"


if __name__ == "__main__":
    asyncio.run(mcp.run_stdio_async())

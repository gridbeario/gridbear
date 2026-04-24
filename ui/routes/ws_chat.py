"""WebSocket chat handler for user portal.

Provides real-time chat with GridBear agents via WebSocket.
Routes messages through GridBear's internal API for full pipeline processing
(sessions, memory, hooks, context builder, runner, MCP tools).
"""

import asyncio
import json
import os

import aiohttp
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from config.logging_config import logger

router = APIRouter()

GRIDBEAR_URL = os.getenv("GRIDBEAR_INTERNAL_URL", "http://gridbear:8000")
GRIDBEAR_SECRET = os.getenv("INTERNAL_API_SECRET", "")

# Active WebSocket connections: {uid: websocket}
_active_connections: dict[str, WebSocket] = {}

# Per-user: which conversation they're currently viewing
_active_conversations: dict[str, str] = {}

# Per-conversation: set of uids currently viewing
_conversation_viewers: dict[str, set[str]] = {}

# Per-conversation: asyncio lock for serializing agent calls
_conversation_locks: dict[str, asyncio.Lock] = {}

# Background tasks — prevent garbage collection
_background_tasks: set[asyncio.Task] = set()

# Per-user active processing task (for stop/cancel)
_active_tasks: dict[str, asyncio.Task] = {}


def _get_conversation_lock(conversation_id: str) -> asyncio.Lock:
    if conversation_id not in _conversation_locks:
        _conversation_locks[conversation_id] = asyncio.Lock()
    return _conversation_locks[conversation_id]


def _register_viewer(uid: str, conversation_id: str) -> None:
    """Register a user as viewing a conversation."""
    # Deregister from previous conversation
    old_conv = _active_conversations.get(uid)
    if old_conv and old_conv != conversation_id:
        viewers = _conversation_viewers.get(old_conv, set())
        viewers.discard(uid)
        if not viewers:
            _conversation_viewers.pop(old_conv, None)
            _conversation_locks.pop(old_conv, None)

    _active_conversations[uid] = conversation_id
    _conversation_viewers.setdefault(conversation_id, set()).add(uid)


def _deregister_viewer(uid: str) -> None:
    """Remove a user from the viewer registry."""
    conv_id = _active_conversations.pop(uid, None)
    if conv_id:
        viewers = _conversation_viewers.get(conv_id, set())
        viewers.discard(uid)
        if not viewers:
            _conversation_viewers.pop(conv_id, None)
            _conversation_locks.pop(conv_id, None)


async def broadcast_to_conversation(
    conversation_id: str, event: dict, exclude_uid: str | None = None
) -> None:
    """Send an event to all users currently viewing a conversation."""
    viewers = _conversation_viewers.get(conversation_id, set())
    logger.info(
        f"broadcast conv={conversation_id[:8]}... viewers={viewers} "
        f"exclude={exclude_uid} event_type={event.get('type')}"
    )
    for uid in list(viewers):
        if uid == exclude_uid:
            continue
        ws = _active_connections.get(uid)
        if ws:
            try:
                await ws.send_json(event)
            except Exception:
                pass


async def push_to_webchat(uid: str, event: dict) -> bool:
    """Push an event to a user's active WebSocket connection.

    Returns True if delivered, False if user not connected.
    """
    ws = _active_connections.get(uid)
    if not ws:
        logger.debug(f"WebChat push: user {uid} not connected")
        return False
    try:
        await ws.send_json(event)
        return True
    except Exception as exc:
        logger.debug(f"WebChat push failed for {uid}: {exc}")
        _active_connections.pop(uid, None)
        return False


def _load_agent_yaml(agent_name: str) -> dict | None:
    """Load agent config from DB."""
    from core.models.agent_config import AgentConfigRecord

    record = AgentConfigRecord.get_sync(id=agent_name)
    if not record:
        return None
    return dict(record)


async def _authenticate_ws(websocket: WebSocket) -> dict | None:
    """Authenticate WebSocket connection from session cookie."""
    cookies = websocket.cookies
    token = cookies.get("gridbear_session_token")
    if not token:
        return None

    from ui.auth.database import auth_db

    session = auth_db.get_session(token)
    if not session:
        return None

    from datetime import datetime

    from ui.auth.session import _ensure_naive_dt

    if _ensure_naive_dt(session["expires_at"]) < datetime.now():
        return None

    user = auth_db.get_user_by_id(session["user_id"])
    if not user or not user.get("is_active"):
        return None

    return user


async def _stream_from_gridbear(
    text: str,
    user: dict,
    agent_name: str,
    websocket: WebSocket | None,
    conversation_id: str | None = None,
    attachments: list[str] | None = None,
    context_prompt: str | None = None,
) -> str:
    """Send message to GridBear internal API and stream NDJSON events to WebSocket.

    If the WebSocket disconnects mid-stream, processing continues and the
    response is still returned (and saved by the caller).
    """
    uid = user["username"]

    payload = {
        "text": text,
        "user_id": uid,
        "username": user.get("username", ""),
        "display_name": user.get("display_name", ""),
        "agent_name": agent_name,
    }
    if attachments:
        payload["attachments"] = attachments
    if context_prompt:
        payload["context_prompt"] = context_prompt
    if conversation_id:
        payload["channel_metadata"] = {
            "channel": "webchat",
            "conversation_id": conversation_id,
        }
        # Add conversation title
        try:
            from ui.routes.chat_api import get_conversation_title

            title = get_conversation_title(conversation_id)
            if title:
                payload["channel_metadata"]["conversation_title"] = title
        except Exception:
            pass
        if context_prompt:
            payload["channel_metadata"]["conversation_context"] = context_prompt

    ws_alive = True

    async def _try_send(event):
        """Best-effort send to all viewers of the conversation."""
        nonlocal ws_alive
        if conversation_id:
            # Broadcast to all viewers
            await broadcast_to_conversation(conversation_id, event)
        elif ws_alive and websocket:
            try:
                await websocket.send_json(event)
            except Exception:
                ws_alive = False

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                f"{GRIDBEAR_URL}/api/chat",
                json=payload,
                headers={"Authorization": f"Bearer {GRIDBEAR_SECRET}"},
                timeout=aiohttp.ClientTimeout(total=1800),
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(
                        f"WebChat: GridBear API error {resp.status}: {error_text}"
                    )
                    await _try_send(
                        {
                            "type": "error",
                            "text": f"GridBear API error: {resp.status}",
                        }
                    )
                    return ""

                result_text = ""
                stream_buffer = ""
                async for line in resp.content:
                    line = line.decode().strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # Forward event to browser (best-effort)
                    await _try_send(event)

                    event_type = event.get("type")
                    if event_type == "message":
                        result_text = event.get("text", "")
                    elif event_type == "stream":
                        stream_buffer += event.get("text", "")
                    elif event_type == "stream_end":
                        if stream_buffer and not result_text:
                            result_text = stream_buffer

                return result_text

        except asyncio.TimeoutError:
            logger.error("WebChat: GridBear API timed out")
            await _try_send(
                {
                    "type": "error",
                    "text": "La richiesta ha impiegato troppo tempo.",
                }
            )
            return ""
        except aiohttp.ClientError as e:
            logger.error(f"WebChat: connection to GridBear failed: {e}")
            await _try_send(
                {
                    "type": "error",
                    "text": "Impossibile contattare il servizio GridBear",
                }
            )
            return ""


def _save_message(
    conversation_id: str | None,
    role: str,
    content: str,
    sender_id: str | None = None,
) -> int | None:
    """Persist a message if conversation tracking is active.

    Returns the new ``chat.webchat_messages.id`` (or ``None`` if no save
    happened) so the caller can broadcast a ``message_persisted`` event
    that lets clients backfill ``msg.dbId`` on the locally-pushed copy
    of the same message — required for the per-message DELETE endpoint.
    """
    if not conversation_id or not content:
        return None
    try:
        from ui.routes.chat_api import save_message

        return save_message(conversation_id, role, content, sender_id=sender_id)
    except Exception as e:
        logger.warning(f"WebChat: failed to save message: {e}")
        return None


async def _broadcast_persisted(
    conversation_id: str | None, role: str, db_id: int | None
) -> None:
    """Tell every viewer of the conversation the DB id of the last save.

    Frontend uses this to attach ``dbId`` to the most recent message of
    matching role that doesn't have one yet (i.e. the one just rendered
    locally from the WS event stream).
    """
    if not conversation_id or db_id is None:
        return
    try:
        await broadcast_to_conversation(
            conversation_id,
            {"type": "message_persisted", "id": db_id, "role": role},
        )
    except Exception as exc:
        logger.warning(f"WebChat: persisted broadcast failed: {exc}")


MAX_AUTO_CONTINUE = 20


def _build_auto_continue_message(conversation_id: str) -> str | None:
    """Build system message for plan auto-continuation. None if not needed."""
    try:
        from ui.routes.chat_api import get_active_plan

        plan = get_active_plan(conversation_id)
        if not plan or plan["status"] != "active":
            return None

        if plan["auto_continue_count"] >= MAX_AUTO_CONTINUE:
            from core.registry import get_database

            db = get_database()
            with db.acquire_sync() as conn:
                conn.execute(
                    "UPDATE chat.webchat_plans SET status = 'paused', "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                    (plan["id"],),
                )
                conn.commit()
            return None

        tasks = plan.get("tasks", [])
        pending = [t for t in tasks if t["status"] == "pending"]
        if not pending:
            return None

        completed = [t for t in tasks if t["status"] == "completed"]
        recent = completed[-3:]

        parts = ["[SYSTEM] Continue with the next task in the plan."]
        if recent:
            parts.append("Recently completed:")
            for t in recent:
                result_text = (t.get("result") or "")[:200]
                parts.append(f'  - {t["title"]} -> "{result_text}"')

        next_task = pending[0]
        parts.append("Next task:")
        parts.append(f"  - {next_task['title']}")
        if next_task.get("description"):
            parts.append(f"    {next_task['description']}")
        parts.append(f"Remaining: {len(pending)} tasks pending")

        from core.registry import get_database

        db = get_database()
        with db.acquire_sync() as conn:
            conn.execute(
                "UPDATE chat.webchat_plans "
                "SET auto_continue_count = auto_continue_count + 1, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                (plan["id"],),
            )
            conn.commit()

        return "\n".join(parts)
    except Exception as exc:
        logger.debug("Auto-continue check failed: %s", exc)
        return None


@router.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    """WebSocket endpoint for user chat."""
    user = await _authenticate_ws(websocket)
    if not user:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()

    agent_name = websocket.query_params.get("agent", "")
    conversation_id = websocket.query_params.get("conversation_id", "") or None
    uid = user["username"]

    # Validate agent access: non-superadmins must be in allowed_users
    if agent_name and not user.get("is_superadmin"):
        from ui.routes.me import _get_allowed_users

        agent_cfg = _load_agent_yaml(agent_name)
        if agent_cfg:
            allowed = _get_allowed_users(agent_cfg)
            if not allowed or uid.lower() not in allowed:
                await websocket.send_json(
                    {"type": "error", "text": "Non hai accesso a questo agente"}
                )
                await websocket.close(code=4003, reason="Forbidden")
                return

    # Validate conversation access (owner or member)
    if conversation_id:
        from ui.routes.chat_api import validate_conversation_access

        if not validate_conversation_access(conversation_id, uid):
            await websocket.send_json(
                {"type": "error", "text": "Conversazione non valida"}
            )
            await websocket.close(code=4003, reason="Forbidden")
            return

    # Check if shared conversation
    is_shared = False
    if conversation_id:
        try:
            from ui.routes.chat_api import _db, _ensure_db

            _ensure_db()
            with _db.acquire_sync() as conn:
                row = conn.execute(
                    "SELECT type FROM chat.webchat_conversations WHERE id = %s",
                    (conversation_id,),
                ).fetchone()
                is_shared = row and row["type"] == "shared"
        except Exception:
            pass

    logger.info(
        f"WebChat: user {uid} connected to agent {agent_name or 'default'}"
        + (f" conv={conversation_id[:8]}..." if conversation_id else "")
        + (" (shared)" if is_shared else "")
    )

    _active_connections[uid] = websocket
    if conversation_id:
        _register_viewer(uid, conversation_id)

    try:
        while True:
            try:
                data = await websocket.receive_json()
            except WebSocketDisconnect:
                break
            except Exception:
                break

            msg_type = data.get("type", "")

            if msg_type == "user_typing":
                # Broadcast typing indicator to other viewers
                if conversation_id:
                    await broadcast_to_conversation(
                        conversation_id,
                        {
                            "type": "user_typing",
                            "sender": uid,
                            "display_name": user.get("display_name", uid),
                        },
                        exclude_uid=uid,
                    )
                continue

            if msg_type == "stop":
                # Abort the agent processing on the bot container
                try:
                    async with aiohttp.ClientSession() as session:
                        await session.post(
                            f"{GRIDBEAR_URL}/api/chat/abort",
                            json={
                                "username": uid,
                                "conversation_id": conversation_id or "default",
                            },
                            headers={"Authorization": f"Bearer {GRIDBEAR_SECRET}"},
                            timeout=aiohttp.ClientTimeout(total=5),
                        )
                    logger.info(f"WebChat: user {uid} aborted agent processing")
                except Exception as exc:
                    logger.debug(f"WebChat: abort request failed: {exc}")
                # Also cancel the local streaming task
                active = _active_tasks.pop(uid, None)
                if active and not active.done():
                    active.cancel()
                try:
                    await websocket.send_json({"type": "stopped"})
                except Exception:
                    pass
                continue

            if msg_type == "message":
                text = data.get("text", "").strip()
                attachments = data.get("attachments") or []
                context_prompt = data.get("context_prompt") or None
                if not text and not attachments:
                    continue

                # Persist user message
                save_text = text
                if attachments and not text:
                    save_text = "[allegato]"
                user_db_id = _save_message(
                    conversation_id, "user", save_text, sender_id=uid
                )
                await _broadcast_persisted(conversation_id, "user", user_db_id)

                # Broadcast user_message to other viewers
                logger.info(
                    f"WebChat: about to broadcast user_message conv={conversation_id}"
                )
                if conversation_id:
                    user_msg_event = {
                        "type": "user_message",
                        "conversation_id": conversation_id,
                        "sender": uid,
                        "display_name": user.get("display_name", uid),
                        "text": text,
                        "time": __import__("datetime").datetime.now().strftime("%H:%M"),
                        "attachments": None,
                    }
                    await broadcast_to_conversation(
                        conversation_id,
                        user_msg_event,
                        exclude_uid=uid,
                    )
                    # Notify participants not currently viewing (unread only)
                    try:
                        from ui.routes.chat_api import list_conversation_participants

                        all_parts = list_conversation_participants(conversation_id)
                        viewers = _conversation_viewers.get(conversation_id, set())
                        for p_uid in all_parts:
                            if p_uid != uid and p_uid not in viewers:
                                await push_to_webchat(
                                    p_uid,
                                    {
                                        "type": "unread",
                                        "conversation_id": conversation_id,
                                    },
                                )
                    except Exception:
                        pass

                # In shared conversations, only invoke the agent if @mentioned
                agent_mentioned = True
                if is_shared and text:
                    mention = f"@{agent_name}" if agent_name else None
                    agent_mentioned = bool(mention and mention.lower() in text.lower())
                    logger.info(
                        f"WebChat: shared mention check: "
                        f"is_shared={is_shared} mention={mention} "
                        f"agent_mentioned={agent_mentioned} text={text[:50]}"
                    )

                if not agent_mentioned:
                    # User-only message, no agent response needed
                    continue

                # Send typing indicator to all viewers
                if conversation_id:
                    await broadcast_to_conversation(conversation_id, {"type": "typing"})
                else:
                    try:
                        await websocket.send_json({"type": "typing"})
                    except Exception:
                        pass

                # Run in background
                async def _process_message(
                    _text, _user, _agent, _ws, _conv_id, _att, _ctx
                ):
                    try:
                        # Acquire conversation lock for shared conversations
                        lock = _get_conversation_lock(_conv_id) if _conv_id else None
                        if lock:
                            await lock.acquire()
                        try:
                            result = await _stream_from_gridbear(
                                _text,
                                _user,
                                _agent,
                                _ws,
                                _conv_id,
                                attachments=_att,
                                context_prompt=_ctx,
                            )
                        finally:
                            if lock and lock.locked():
                                lock.release()

                        if result:
                            asst_db_id = _save_message(_conv_id, "assistant", result)
                            await _broadcast_persisted(_conv_id, "agent", asst_db_id)
                            # Send unread to all participants not viewing
                            if _conv_id:
                                try:
                                    from ui.routes.chat_api import (
                                        list_conversation_participants,
                                    )

                                    parts = list_conversation_participants(_conv_id)
                                    viewers = _conversation_viewers.get(_conv_id, set())
                                    for p_uid in parts:
                                        if p_uid not in viewers:
                                            await push_to_webchat(
                                                p_uid,
                                                {
                                                    "type": "unread",
                                                    "conversation_id": _conv_id,
                                                },
                                            )
                                except Exception:
                                    pass

                        if _conv_id:
                            from ui.routes.chat_api import get_conversation_title

                            title = get_conversation_title(_conv_id)
                            if title:
                                await broadcast_to_conversation(
                                    _conv_id,
                                    {
                                        "type": "conversation_update",
                                        "conversation_id": _conv_id,
                                        "title": title,
                                    },
                                )

                        # Auto-continue loop for active plans
                        if _conv_id and result:
                            while True:
                                auto_msg = _build_auto_continue_message(_conv_id)
                                if not auto_msg:
                                    break

                                # Brief lock to verify plan is still active
                                lock = (
                                    _get_conversation_lock(_conv_id)
                                    if _conv_id
                                    else None
                                )
                                if lock:
                                    await lock.acquire()
                                try:
                                    from ui.routes.chat_api import get_active_plan

                                    plan = get_active_plan(_conv_id)
                                    if not plan or plan["status"] != "active":
                                        break
                                finally:
                                    if lock and lock.locked():
                                        lock.release()

                                # Stream WITHOUT lock
                                logger.info(
                                    "WebChat: auto-continue plan "
                                    f"conv={_conv_id[:8]}..."
                                )
                                auto_db_id = _save_message(
                                    _conv_id,
                                    "user",
                                    auto_msg,
                                    sender_id="system",
                                )
                                await _broadcast_persisted(_conv_id, "user", auto_db_id)
                                await broadcast_to_conversation(
                                    _conv_id, {"type": "typing"}
                                )
                                cont_result = await _stream_from_gridbear(
                                    auto_msg,
                                    _user,
                                    _agent,
                                    _ws,
                                    _conv_id,
                                )
                                if cont_result:
                                    cont_db_id = _save_message(
                                        _conv_id, "assistant", cont_result
                                    )
                                    await _broadcast_persisted(
                                        _conv_id, "agent", cont_db_id
                                    )
                                else:
                                    break

                    except Exception:
                        logger.exception("WebChat: background processing error")

                task = asyncio.create_task(
                    _process_message(
                        text,
                        user,
                        agent_name,
                        websocket,
                        conversation_id,
                        attachments if attachments else None,
                        context_prompt,
                    )
                )
                _background_tasks.add(task)
                _active_tasks[uid] = task

                def _on_task_done(t, _uid=uid):
                    _background_tasks.discard(t)
                    _active_tasks.pop(_uid, None)

                task.add_done_callback(_on_task_done)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WebChat: connection error: {e}")
    finally:
        _active_connections.pop(uid, None)
        _deregister_viewer(uid)
        logger.info(f"WebChat: user {uid} disconnected")

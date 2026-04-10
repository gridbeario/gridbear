"""Internal API server for GridBear.

Exposes MessageProcessor.process_message() via HTTP with NDJSON streaming.
Used by the admin container's WebChat to route messages through the full pipeline.

Plugin-specific endpoints are discovered from plugins/{name}/api/routes.py.
"""

import asyncio
import importlib.machinery
import importlib.util
import json
import sys
import time
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

# Active processing tasks: key -> asyncio.Task
# Key format: "{username}:{conversation_id}" or "{username}:default"
_active_processing: dict[str, asyncio.Task] = {}

from config.logging_config import logger
from core.api_schemas import ApiResponse, api_error, api_ok
from core.internal_api.auth import verify_internal_auth
from core.registry import get_agent_manager, get_plugin_manager

router = APIRouter(prefix="/api")


class ChatRequest(BaseModel):
    text: str
    user_id: str
    username: str
    display_name: str | None = ""
    platform: str = "webchat"
    agent_name: str
    attachments: list[str] = []
    context_prompt: str | None = None
    channel_metadata: dict = {}


@router.post("/chat")
async def chat(
    request: ChatRequest,
    _auth: None = Depends(verify_internal_auth),
):
    """Process a chat message through the full GridBear pipeline with NDJSON streaming."""
    from core.interfaces.channel import Message, UserInfo

    plugin_manager = get_plugin_manager()
    agent_manager = get_agent_manager()

    if not plugin_manager or not agent_manager:
        return StreamingResponse(
            _single_event({"type": "error", "text": "GridBear not initialized"}),
            media_type="application/x-ndjson",
        )

    agent = agent_manager.get_agent(request.agent_name)
    if not agent:
        return StreamingResponse(
            _single_event(
                {"type": "error", "text": f"Agent '{request.agent_name}' not found"}
            ),
            media_type="application/x-ndjson",
        )

    # Build agent_context (same structure as agent_manager.py:282-301)
    agent_context = {
        "name": agent.name,
        "display_name": agent.display_name,
        "system_prompt": agent.system_prompt,
        "model": agent.config.model,
        "runner": agent.config.runner,
        "fallback_runner": agent.config.fallback_runner,
        "voice": {
            "provider": agent.config.voice.provider,
            "voice_id": agent.config.voice.voice_id,
            "language": agent.config.voice.language,
        },
        "mcp_permissions": agent.config.mcp_permissions,
        "max_tools": agent.config.max_tools,
        "tool_loading": agent.config.tool_loading,
        "locale": agent.config.locale,
        "email": agent.email_settings,
        "context_options": agent.config.context_options,
    }

    # Import here to avoid circular imports at module level
    from main import AgentAwareMessageProcessor

    processor = AgentAwareMessageProcessor(plugin_manager, agent_context)

    # Fetch participant list for shared conversations
    ch_meta = dict(request.channel_metadata) if request.channel_metadata else {}
    conv_id = ch_meta.get("conversation_id")
    if conv_id:
        try:
            from core.registry import get_database

            db = get_database()
            if db:
                with db.acquire_sync() as conn:
                    rows = conn.execute(
                        "SELECT p.unified_id, u.display_name "
                        "FROM chat.webchat_participants p "
                        "LEFT JOIN app.users u ON u.username = p.unified_id "
                        "WHERE p.conversation_id = %s",
                        (conv_id,),
                    ).fetchall()
                    if len(rows) > 1:
                        ch_meta["participants"] = [
                            {
                                "uid": r["unified_id"],
                                "name": r["display_name"] or r["unified_id"],
                            }
                            for r in rows
                        ]
                    # Fetch conversation documents for context injection
                    doc_rows = conn.execute(
                        "SELECT original_filename, content_text "
                        "FROM chat.webchat_documents "
                        "WHERE conversation_id = %s ORDER BY uploaded_at",
                        (conv_id,),
                    ).fetchall()
                    if doc_rows:
                        ch_meta["conversation_documents"] = [
                            {
                                "original_filename": r["original_filename"],
                                "content_text": r["content_text"] or "",
                            }
                            for r in doc_rows
                        ]
        except Exception:
            pass

    message = Message(
        user_id=0,
        username=request.username,
        text=request.text,
        attachments=request.attachments,
        platform=request.platform,
        context_prompt=request.context_prompt,
        channel_metadata=ch_meta,
    )

    user_info = UserInfo(
        user_id=0,
        username=request.username,
        display_name=request.display_name or request.username,
        platform=request.platform,
        unified_id=request.user_id,
    )

    event_queue: asyncio.Queue = asyncio.Queue()

    async def progress_cb(msg):
        await event_queue.put({"type": "typing", "message": msg})

    async def tool_cb(tool_name, tool_input):
        await event_queue.put({"type": "tool_call", "tool": tool_name})

    async def error_cb(error_type, details):
        await event_queue.put(
            {"type": "error", "error_type": error_type, "details": str(details)}
        )

    async def process():
        try:
            result = await processor.process_message(
                message,
                user_info,
                progress_callback=progress_cb,
                error_callback=error_cb,
                tool_callback=tool_cb,
            )
            await event_queue.put(
                {
                    "type": "message",
                    "text": result,
                    "agent": agent_context["display_name"],
                }
            )
        except Exception as e:
            logger.error(f"Internal API: process_message error: {e}", exc_info=True)
            await event_queue.put(
                {"type": "error", "text": "Errore interno durante l'elaborazione"}
            )
        finally:
            await event_queue.put(None)

    task = asyncio.create_task(process())

    # Track task for abort support
    conv_id = ch_meta.get("conversation_id", "default")
    task_key = f"{request.username}:{conv_id}"
    _active_processing[task_key] = task

    def _cleanup(t):
        _active_processing.pop(task_key, None)

    task.add_done_callback(_cleanup)

    async def event_stream():
        while True:
            event = await event_queue.get()
            if event is None:
                break
            yield json.dumps(event) + "\n"

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")


@router.post("/chat/abort")
async def abort_chat(
    request: Request,
    _auth: None = Depends(verify_internal_auth),
):
    """Abort an active chat processing task."""
    body = await request.json()
    username = body.get("username", "")
    conversation_id = body.get("conversation_id", "default")

    task_key = f"{username}:{conversation_id}"
    task = _active_processing.pop(task_key, None)
    if task and not task.done():
        task.cancel()
        logger.info(f"Internal API: aborted chat task {task_key}")
        return JSONResponse({"ok": True, "aborted": True})

    return JSONResponse({"ok": True, "aborted": False})


class SendFileRequest(BaseModel):
    agent_name: str
    platform: str
    chat_id: str
    file_path: str
    caption: str | None = None


class SendMessageRequest(BaseModel):
    agent_name: str
    platform: str
    chat_id: str
    text: str


@router.post(
    "/send-file",
    response_model=ApiResponse,
    response_model_exclude_none=True,
)
async def send_file(
    request: SendFileRequest,
    _auth: None = Depends(verify_internal_auth),
):
    """Send a file to a user via a channel adapter."""
    from pathlib import Path

    agent_manager = get_agent_manager()
    if not agent_manager:
        return api_error(503, "Not initialized", "unavailable")

    agent = agent_manager.get_agent(request.agent_name)
    if not agent:
        return api_error(404, f"Agent '{request.agent_name}' not found", "not_found")

    channel = agent.get_channel(request.platform)
    if not channel:
        return api_error(
            404, f"Channel '{request.platform}' not available", "not_found"
        )

    # Security: resolve path to prevent traversal attacks
    resolved_path = Path(request.file_path).resolve()
    if not str(resolved_path).startswith("/app/data/"):
        return api_error(403, "File path not allowed", "forbidden")

    if not resolved_path.exists():
        return api_error(404, f"File not found: {resolved_path}", "not_found")

    # Warn if file exceeds channel's max_file_size (non-blocking)
    max_file_size = getattr(channel, "max_file_size", None)
    if max_file_size and resolved_path.stat().st_size > max_file_size:
        logger.warning(
            f"File {resolved_path} ({resolved_path.stat().st_size} bytes) "
            f"exceeds {request.platform} limit ({max_file_size} bytes)"
        )

    try:
        chat_id = int(request.chat_id) if request.chat_id.isdigit() else request.chat_id
        sent = await channel.send_file(
            chat_id,
            str(resolved_path),
            request.caption,
        )
        if not sent:
            return api_error(500, "Channel returned failure", "send_failed")
        return api_ok()
    except Exception as e:
        logger.error(
            f"send_file failed: agent={request.agent_name} "
            f"platform={request.platform} error={e}"
        )
        return api_error(500, str(e), "internal_error")


@router.post(
    "/send-message",
    response_model=ApiResponse,
    response_model_exclude_none=True,
)
async def send_message(
    request: SendMessageRequest,
    _auth: None = Depends(verify_internal_auth),
):
    """Send a text message to a user via a channel adapter."""
    agent_manager = get_agent_manager()
    if not agent_manager:
        return api_error(503, "Not initialized", "unavailable")

    agent = agent_manager.get_agent(request.agent_name)
    if not agent:
        return api_error(404, f"Agent '{request.agent_name}' not found", "not_found")

    channel = agent.get_channel(request.platform)
    if not channel:
        return api_error(
            404, f"Channel '{request.platform}' not available", "not_found"
        )

    try:
        chat_id = int(request.chat_id) if request.chat_id.isdigit() else request.chat_id
        await channel.send_message(chat_id, request.text)
        return api_ok()
    except Exception as e:
        logger.error(
            "send_message failed: agent=%s platform=%s error=%s",
            request.agent_name,
            request.platform,
            e,
        )
        return api_error(500, str(e), "internal_error")


class TaskResultRequest(BaseModel):
    agent_name: str
    platform: str
    chat_id: str
    tool_name: str
    task_id: str
    status: str  # "completed" or "failed"
    duration: str = ""


@router.post(
    "/process-task-result",
    response_model=ApiResponse,
    response_model_exclude_none=True,
)
async def process_task_result(
    request: TaskResultRequest,
    _auth: None = Depends(verify_internal_auth),
):
    """Re-engage the agent after an async task completes or fails.

    Injects a synthetic message into the agent's conversation pipeline so that
    the agent can use its MCP tools (async_task_status) to inspect the result
    and continue the workflow or explain errors to the user.
    """
    from core.interfaces.channel import Message, UserInfo

    agent_manager = get_agent_manager()
    if not agent_manager:
        return api_error(503, "Not initialized", "unavailable")

    agent = agent_manager.get_agent(request.agent_name)
    if not agent:
        return api_error(404, f"Agent '{request.agent_name}' not found", "not_found")

    channel = agent.get_channel(request.platform)
    if not channel:
        return api_error(
            404, f"Channel '{request.platform}' not available", "not_found"
        )

    if not channel._message_handler:
        return api_error(500, "Channel message handler not set", "internal_error")

    chat_id = int(request.chat_id) if request.chat_id.isdigit() else request.chat_id

    # Resolve username from channel's cache (Telegram/Discord keep this)
    username = None
    if hasattr(channel, "_user_usernames"):
        username = channel._user_usernames.get(chat_id)

    status_verb = (
        "completed successfully" if request.status == "completed" else "failed"
    )
    duration_info = (
        f" in {request.duration.strip()}" if request.duration.strip() else ""
    )
    inject_text = (
        f"[ASYNC TASK RESULT] The background task {request.tool_name} has "
        f"{status_verb}{duration_info} (task ID: {request.task_id}). "
        f"Use async_task_status to get the full result, then continue with "
        f"ALL remaining steps from your original plan. Do not stop after "
        f"handling this task — complete the entire workflow the user requested."
    )

    message = Message(
        user_id=chat_id if isinstance(chat_id, int) else 0,
        username=username,
        text=inject_text,
        platform=request.platform,
    )
    user_info = UserInfo(
        user_id=chat_id if isinstance(chat_id, int) else 0,
        username=username,
        display_name=username or "System",
        platform=request.platform,
    )

    async def _background_process():
        try:
            response_text = await channel._message_handler(
                message,
                user_info,
                progress_callback=None,
                error_callback=None,
            )
            if response_text:
                await channel.send_message(chat_id, response_text)
        except Exception as e:
            logger.error(
                "process-task-result background error: agent=%s platform=%s chat=%s error=%s",
                request.agent_name,
                request.platform,
                request.chat_id,
                e,
            )

    asyncio.create_task(_background_process())
    return api_ok()


# ── Vault (on-demand secret reading) ──────────────────────────────


@router.get(
    "/vault/get",
    response_model=ApiResponse[dict],
    response_model_exclude_none=True,
)
async def vault_get(
    key: str,
    _auth: None = Depends(verify_internal_auth),
):
    """Read a single secret from the vault by exact key name."""
    from ui.secrets_manager import secrets_manager

    value = secrets_manager.get_plain(key, fallback_env=False)
    if value is None:
        return api_error(404, f"Secret '{key}' not found", "not_found")
    return api_ok(data={"key": key, "value": value})


@router.get(
    "/vault/list",
    response_model=ApiResponse[dict],
    response_model_exclude_none=True,
)
async def vault_list_by_prefix(
    prefix: str,
    _auth: None = Depends(verify_internal_auth),
):
    """List secret key names (not values) matching a prefix."""
    from ui.secrets_manager import secrets_manager

    entries = secrets_manager.list_keys_by_prefix(prefix)
    keys = [e["key_name"] for e in entries]
    return api_ok(data={"prefix": prefix, "keys": keys})


@router.get(
    "/health",
    response_model=ApiResponse[dict],
    response_model_exclude_none=True,
)
async def health(
    request: Request,
    _auth: None = Depends(verify_internal_auth),
):
    """Return bot health status including uptime."""
    bot_start_time = getattr(request.app.state, "bot_start_time", None)
    elapsed = time.time() - bot_start_time if bot_start_time else 0
    return api_ok(
        data={
            "status": "ok",
            "uptime_seconds": elapsed,
        }
    )


@router.post("/agents/{agent_id}/reload")
async def reload_agent_endpoint(
    agent_id: str,
    _auth: None = Depends(verify_internal_auth),
):
    """Reload a single agent from DB config. Called by UI after config save."""
    from core.registry import get_agent_manager

    manager = get_agent_manager()
    if not manager:
        return JSONResponse(
            {"ok": False, "error": "Agent manager not available"},
            status_code=500,
        )

    success = await manager.reload_agent(agent_id)
    if success:
        return JSONResponse({"ok": True})
    return JSONResponse(
        {"ok": False, "error": f"Failed to reload {agent_id}"},
        status_code=500,
    )


async def _single_event(event: dict):
    """Yield a single NDJSON event (for error responses)."""
    yield json.dumps(event) + "\n"


def _import_plugin_module(plugin_path: Path, routes_path: Path):
    """Import a plugin sub-module with proper package context.

    Ensures parent packages exist in sys.modules so that relative imports
    (e.g. ``from ..models import Foo``) work correctly.
    """
    try:
        rel = routes_path.relative_to(plugin_path.parent.parent)
    except ValueError:
        rel = routes_path.relative_to(Path.cwd())
    module_name = str(rel.with_suffix("")).replace("/", ".").replace("\\", ".")

    parts = module_name.split(".")
    for i in range(1, len(parts)):
        parent = ".".join(parts[:i])
        if parent not in sys.modules:
            parent_path = Path.cwd() / Path(*parts[:i])
            init_file = parent_path / "__init__.py"
            if init_file.exists():
                spec = importlib.util.spec_from_file_location(
                    parent,
                    str(init_file),
                    submodule_search_locations=[str(parent_path)],
                )
                mod = importlib.util.module_from_spec(spec)
                sys.modules[parent] = mod
                spec.loader.exec_module(mod)
            else:
                mod = importlib.util.module_from_spec(
                    importlib.machinery.ModuleSpec(
                        parent,
                        None,
                        is_package=True,
                    )
                )
                mod.__path__ = [str(parent_path)]
                sys.modules[parent] = mod

    return importlib.import_module(module_name)


def _discover_plugin_api_routes(app: FastAPI, plugin_manager) -> None:
    """Discover and mount api/routes.py from enabled plugins.

    Each plugin that provides an api/routes.py with a `router` attribute
    gets mounted at /api/{plugin_name}/.
    """

    path_resolver = plugin_manager._path_resolver

    # _manifests stores all loaded plugin names (including skip_instantiate channels)
    enabled_plugins = list(plugin_manager._manifests.keys())

    for plugin_name in enabled_plugins:
        plugin_dir = path_resolver.resolve(plugin_name)
        if plugin_dir is None:
            continue
        routes_path = plugin_dir / "api" / "routes.py"

        if not routes_path.exists():
            continue

        try:
            module = _import_plugin_module(plugin_dir, routes_path)

            if hasattr(module, "router"):
                # Call init() if the plugin provides it
                if hasattr(module, "init"):
                    module.init(plugin_manager)

                app.include_router(
                    module.router,
                    prefix=f"/api/{plugin_name}",
                    tags=[f"api-{plugin_name}"],
                )
                logger.info(f"Internal API: mounted plugin routes for {plugin_name}")
        except Exception as e:
            logger.error(
                f"Internal API: failed to load api/routes.py for {plugin_name}: {e}"
            )


def create_app(plugin_manager=None) -> FastAPI:
    """Create the internal API FastAPI application.

    Args:
        plugin_manager: If provided, discovers and mounts plugin API routes
            from plugins/{name}/api/routes.py.
    """
    app = FastAPI(title="GridBear Internal API", docs_url=None, redoc_url=None)
    app.include_router(router)

    if plugin_manager:
        _discover_plugin_api_routes(app, plugin_manager)

    return app

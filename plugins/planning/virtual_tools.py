"""Planning virtual tools — task plan management for webchat agents."""

import uuid
from datetime import datetime, timezone

from config.logging_config import logger
from core.interfaces.local_tools import LocalToolProvider

_SERVER_NAME = "planning"

_VALID_TRANSITIONS = {
    "pending": {"in_progress", "skipped"},
    "in_progress": {"completed", "failed", "skipped"},
    "failed": {"pending"},
}

_TERMINAL_PLAN = {"completed", "cancelled"}

_TOOLS = [
    {
        "name": f"{_SERVER_NAME}__plan_create",
        "description": (
            "Create a new task plan for the current conversation. "
            "The plan starts in 'draft' status. Wait for user approval before starting."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {
                    "type": "string",
                    "description": (
                        "Current conversation ID "
                        "(from [Message Source] in your context)"
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "Short title for the plan",
                },
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["title"],
                    },
                    "description": "Ordered list of tasks",
                },
            },
            "required": ["conversation_id", "title", "tasks"],
        },
    },
    {
        "name": f"{_SERVER_NAME}__plan_task_update",
        "description": (
            "Update a task's status. Use 'in_progress' when starting, "
            "'completed' when done (include result), 'failed' on error."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": [
                        "in_progress",
                        "completed",
                        "failed",
                        "skipped",
                        "pending",
                    ],
                },
                "result": {
                    "type": "string",
                    "description": "Brief result or error description",
                },
            },
            "required": ["task_id", "status"],
        },
    },
    {
        "name": f"{_SERVER_NAME}__plan_update",
        "description": (
            "Update a plan: start/pause/cancel execution, add/remove/edit tasks."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": ["start", "pause", "cancel"],
                },
                "add_tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["title"],
                    },
                },
                "remove_tasks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Task IDs to remove (only pending tasks)",
                },
                "update_tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "string"},
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["task_id"],
                    },
                    "description": (
                        "Edit existing tasks (title/description). "
                        "Only pending tasks can be edited."
                    ),
                },
            },
            "required": ["plan_id"],
        },
    },
]


def _now():
    return datetime.now(timezone.utc)


def _get_db():
    from core.registry import get_database

    return get_database()


async def _broadcast(conversation_id: str, event: dict):
    """Broadcast plan event to all WebSocket viewers of the conversation."""
    try:
        from ui.routes.ws_chat import broadcast_to_conversation

        await broadcast_to_conversation(conversation_id, event)
    except Exception as exc:
        logger.debug("Planning broadcast failed: %s", exc)


class PlanningToolProvider(LocalToolProvider):
    """Exposes plan_create, plan_task_update, plan_update tools."""

    def get_server_name(self) -> str:
        return _SERVER_NAME

    def get_tools(self) -> list[dict]:
        return _TOOLS

    async def handle_tool_call(
        self, tool_name: str, arguments: dict, **kwargs
    ) -> list[dict]:
        action = tool_name.replace(f"{_SERVER_NAME}__", "")

        if action == "plan_create":
            conversation_id = arguments.get("conversation_id") or kwargs.get(
                "conversation_id"
            )
            if not conversation_id:
                return [{"type": "text", "text": "Error: conversation_id is required."}]
            return await self._handle_create(arguments, conversation_id, kwargs)

        if action == "plan_task_update":
            return await self._handle_task_update(arguments)

        if action == "plan_update":
            return await self._handle_plan_update(arguments)

        return [{"type": "text", "text": f"Unknown planning action: {action}"}]

    async def _handle_create(
        self, args: dict, conversation_id: str, kwargs: dict
    ) -> list[dict]:
        title = args.get("title", "").strip()
        tasks = args.get("tasks", [])
        if not title or not tasks:
            return [{"type": "text", "text": "Error: title and tasks are required."}]

        db = _get_db()
        with db.acquire_sync() as conn:
            existing = conn.execute(
                "SELECT id, status FROM chat.webchat_plans "
                "WHERE conversation_id = %s AND status NOT IN ('completed', 'cancelled') "
                "LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if existing:
                return [
                    {
                        "type": "text",
                        "text": (
                            f"Error: a plan already exists "
                            f"(status: {existing['status']}). "
                            "Complete or cancel it before creating a new one."
                        ),
                    }
                ]

            plan_id = str(uuid.uuid4())
            agent_name = kwargs.get("agent_name", "unknown")
            now = _now()
            conn.execute(
                "INSERT INTO chat.webchat_plans "
                "(id, conversation_id, title, status, created_by, "
                "created_at, updated_at) "
                "VALUES (%s, %s, %s, 'draft', %s, %s, %s)",
                (plan_id, conversation_id, title, agent_name, now, now),
            )

            task_rows = []
            for i, t in enumerate(tasks):
                t_title = t.get("title", "").strip()
                if not t_title:
                    continue
                task_id = str(uuid.uuid4())
                t_desc = t.get("description")
                conn.execute(
                    "INSERT INTO chat.webchat_plan_tasks "
                    "(id, plan_id, position, title, description, status) "
                    "VALUES (%s, %s, %s, %s, %s, 'pending')",
                    (task_id, plan_id, i, t_title, t_desc),
                )
                task_rows.append(
                    {
                        "id": task_id,
                        "position": i,
                        "title": t_title,
                        "description": t_desc,
                        "status": "pending",
                    }
                )
            conn.commit()

        plan_data = {
            "id": plan_id,
            "title": title,
            "status": "draft",
            "created_by": agent_name,
            "tasks": task_rows,
        }
        await _broadcast(conversation_id, {"type": "plan_created", "plan": plan_data})

        task_summary = "\n".join(
            f"  {i + 1}. {t['title']}" for i, t in enumerate(task_rows)
        )
        return [
            {
                "type": "text",
                "text": (
                    f"Plan created (draft): {title}\n"
                    f"Tasks:\n{task_summary}\n\n"
                    f"Plan ID: {plan_id}\n"
                    "Waiting for user approval. "
                    "When approved, call plan_update(action: 'start')."
                ),
            }
        ]

    async def _handle_task_update(self, args: dict) -> list[dict]:
        task_id = args.get("task_id", "").strip()
        new_status = args.get("status", "").strip()
        result = args.get("result")

        if not task_id or not new_status:
            return [{"type": "text", "text": "Error: task_id and status required."}]

        db = _get_db()
        with db.acquire_sync() as conn:
            row = conn.execute(
                "SELECT t.status, t.plan_id, p.status as plan_status, "
                "p.conversation_id "
                "FROM chat.webchat_plan_tasks t "
                "JOIN chat.webchat_plans p ON p.id = t.plan_id "
                "WHERE t.id = %s",
                (task_id,),
            ).fetchone()
            if not row:
                return [{"type": "text", "text": f"Error: task {task_id} not found."}]

            conversation_id = row["conversation_id"]
            current = row["status"]
            allowed = _VALID_TRANSITIONS.get(current, set())
            if new_status not in allowed:
                return [
                    {
                        "type": "text",
                        "text": (
                            f"Error: invalid transition {current} -> {new_status}. "
                            f"Allowed: "
                            f"{', '.join(sorted(allowed)) if allowed else 'none'}."
                        ),
                    }
                ]

            now = _now()
            updates = ["status = %s", "result = %s"]
            params: list = [new_status, result]

            if new_status == "in_progress":
                updates.append("started_at = %s")
                params.append(now)
            elif new_status in ("completed", "failed", "skipped"):
                updates.append("completed_at = %s")
                params.append(now)
            elif new_status == "pending":
                updates.extend(
                    ["started_at = NULL", "completed_at = NULL", "result = NULL"]
                )

            params.append(task_id)
            conn.execute(
                f"UPDATE chat.webchat_plan_tasks "
                f"SET {', '.join(updates)} WHERE id = %s",
                params,
            )

            plan_id = row["plan_id"]
            conn.execute(
                "UPDATE chat.webchat_plans SET updated_at = %s WHERE id = %s",
                (now, plan_id),
            )

            plan_status_change = None
            if new_status == "failed":
                conn.execute(
                    "UPDATE chat.webchat_plans SET status = 'paused', "
                    "updated_at = %s WHERE id = %s AND status = 'active'",
                    (now, plan_id),
                )
                plan_status_change = "paused"

            if new_status in ("completed", "skipped"):
                pending = conn.execute(
                    "SELECT count(*) as cnt FROM chat.webchat_plan_tasks "
                    "WHERE plan_id = %s AND status IN ('pending', 'in_progress')",
                    (plan_id,),
                ).fetchone()
                if pending and pending["cnt"] == 0:
                    conn.execute(
                        "UPDATE chat.webchat_plans SET status = 'completed', "
                        "updated_at = %s WHERE id = %s",
                        (now, plan_id),
                    )
                    plan_status_change = "completed"

            conn.commit()

        event = {
            "type": "plan_task_update",
            "task_id": task_id,
            "status": new_status,
        }
        if result:
            event["result"] = result
        if plan_status_change:
            event["plan_status"] = plan_status_change
        await _broadcast(conversation_id, event)

        msg = f"Task updated: {new_status}"
        if plan_status_change:
            msg += f" (plan -> {plan_status_change})"
        return [{"type": "text", "text": msg}]

    async def _handle_plan_update(self, args: dict) -> list[dict]:
        plan_id = args.get("plan_id", "").strip()
        action = args.get("action")
        add_tasks = args.get("add_tasks", [])
        remove_tasks = args.get("remove_tasks", [])
        update_tasks = args.get("update_tasks", [])

        if not plan_id:
            return [{"type": "text", "text": "Error: plan_id is required."}]

        db = _get_db()
        changes: dict = {}
        warnings: list[str] = []

        with db.acquire_sync() as conn:
            row = conn.execute(
                "SELECT status, conversation_id FROM chat.webchat_plans WHERE id = %s",
                (plan_id,),
            ).fetchone()
            if not row:
                return [{"type": "text", "text": f"Error: plan {plan_id} not found."}]

            conversation_id = row["conversation_id"]
            current = row["status"]
            if current in _TERMINAL_PLAN:
                return [
                    {
                        "type": "text",
                        "text": f"Error: plan is {current}, cannot modify.",
                    }
                ]

            now = _now()

            if action:
                if action == "start" and current in ("draft", "paused"):
                    conn.execute(
                        "UPDATE chat.webchat_plans SET status = 'active', "
                        "auto_continue_count = 0, updated_at = %s WHERE id = %s",
                        (now, plan_id),
                    )
                    changes["status"] = "active"
                elif action == "pause" and current == "active":
                    conn.execute(
                        "UPDATE chat.webchat_plans SET status = 'paused', "
                        "updated_at = %s WHERE id = %s",
                        (now, plan_id),
                    )
                    changes["status"] = "paused"
                elif action == "cancel":
                    conn.execute(
                        "UPDATE chat.webchat_plans SET status = 'cancelled', "
                        "updated_at = %s WHERE id = %s",
                        (now, plan_id),
                    )
                    changes["status"] = "cancelled"
                else:
                    return [
                        {
                            "type": "text",
                            "text": (
                                f"Error: cannot {action} a plan in status {current}."
                            ),
                        }
                    ]

            if changes.get("status") == "cancelled":
                if add_tasks or remove_tasks:
                    warnings.append("add/remove ignored: plan was cancelled")
            else:
                if add_tasks:
                    max_pos = conn.execute(
                        "SELECT COALESCE(MAX(position), -1) as mp "
                        "FROM chat.webchat_plan_tasks WHERE plan_id = %s",
                        (plan_id,),
                    ).fetchone()["mp"]
                    added = []
                    for i, t in enumerate(add_tasks):
                        t_title = t.get("title", "").strip()
                        if not t_title:
                            continue
                        tid = str(uuid.uuid4())
                        pos = max_pos + 1 + i
                        conn.execute(
                            "INSERT INTO chat.webchat_plan_tasks "
                            "(id, plan_id, position, title, description, status) "
                            "VALUES (%s, %s, %s, %s, %s, 'pending')",
                            (tid, plan_id, pos, t_title, t.get("description")),
                        )
                        added.append(
                            {
                                "id": tid,
                                "position": pos,
                                "title": t_title,
                                "status": "pending",
                            }
                        )
                    if added:
                        changes["added_tasks"] = added
                    conn.execute(
                        "UPDATE chat.webchat_plans SET updated_at = %s WHERE id = %s",
                        (now, plan_id),
                    )

                if remove_tasks:
                    removed = []
                    for tid in remove_tasks:
                        r = conn.execute(
                            "DELETE FROM chat.webchat_plan_tasks "
                            "WHERE id = %s AND plan_id = %s "
                            "AND status = 'pending' RETURNING id",
                            (tid, plan_id),
                        ).fetchone()
                        if r:
                            removed.append(tid)
                    if removed:
                        changes["removed_tasks"] = removed
                    conn.execute(
                        "UPDATE chat.webchat_plans SET updated_at = %s WHERE id = %s",
                        (now, plan_id),
                    )

                if update_tasks:
                    updated = []
                    for ut in update_tasks:
                        tid = ut.get("task_id", "").strip()
                        if not tid:
                            continue
                        new_title = ut.get("title")
                        new_desc = ut.get("description")
                        sets = []
                        params: list = []
                        if new_title is not None:
                            sets.append("title = %s")
                            params.append(new_title.strip())
                        if new_desc is not None:
                            sets.append("description = %s")
                            params.append(new_desc)
                        if not sets:
                            continue
                        params.extend([tid, plan_id])
                        conn.execute(
                            f"UPDATE chat.webchat_plan_tasks "
                            f"SET {', '.join(sets)} "
                            f"WHERE id = %s AND plan_id = %s "
                            f"AND status = 'pending'",
                            params,
                        )
                        updated.append(tid)
                    if updated:
                        changes["updated_tasks"] = updated
                    conn.execute(
                        "UPDATE chat.webchat_plans SET updated_at = %s WHERE id = %s",
                        (now, plan_id),
                    )

            conn.commit()

        if changes:
            await _broadcast(
                conversation_id,
                {
                    "type": "plan_updated",
                    "plan_id": plan_id,
                    "changes": changes,
                },
            )

        parts = [f"Plan updated: {changes}"]
        if warnings:
            parts.append(f"Warnings: {', '.join(warnings)}")
        return [{"type": "text", "text": " ".join(parts)}]

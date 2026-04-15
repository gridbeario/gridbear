"""Plan execution helper — auto-advance tasks without model cooperation.

Called by the message processor before/after runner invocation
to manage plan task lifecycle. The model doesn't need to call
planning tools — the code handles status transitions.
"""

from __future__ import annotations

from config.logging_config import logger


def get_active_plan_task(conversation_id: str) -> dict | None:
    """Find the active plan's next actionable task.

    Returns dict with plan + task info, or None if no active plan.
    """
    try:
        from core.registry import get_database

        db = get_database()
        if not db:
            return None

        with db.acquire_sync() as conn:
            plan = conn.execute(
                "SELECT id, title, status FROM chat.webchat_plans "
                "WHERE conversation_id = %s AND status = 'active' "
                "LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if not plan:
                return None

            # Check for a task already in_progress (resume after timeout/crash)
            current = conn.execute(
                "SELECT id, title, description, position FROM chat.webchat_plan_tasks "
                "WHERE plan_id = %s AND status = 'in_progress' "
                "ORDER BY position LIMIT 1",
                (plan["id"],),
            ).fetchone()

            if not current:
                # Find next pending task
                current = conn.execute(
                    "SELECT id, title, description, position FROM chat.webchat_plan_tasks "
                    "WHERE plan_id = %s AND status = 'pending' "
                    "ORDER BY position LIMIT 1",
                    (plan["id"],),
                ).fetchone()

            if not current:
                return None

            total = conn.execute(
                "SELECT count(*) as cnt FROM chat.webchat_plan_tasks "
                "WHERE plan_id = %s",
                (plan["id"],),
            ).fetchone()["cnt"]

            return {
                "plan_id": plan["id"],
                "plan_title": plan["title"],
                "task_id": current["id"],
                "task_title": current["title"],
                "task_description": current["description"] or "",
                "task_position": current["position"] + 1,  # 1-based
                "total_tasks": total,
            }
    except Exception as exc:
        logger.debug("get_active_plan_task failed: %s", exc)
        return None


async def mark_task_in_progress(task_id: str, conversation_id: str) -> None:
    """Set task to in_progress and broadcast to webchat."""
    try:
        from core.registry import get_database

        db = get_database()
        if not db:
            return

        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)

        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE chat.webchat_plan_tasks "
                "SET status = 'in_progress', started_at = %s "
                "WHERE id = %s AND status IN ('pending', 'in_progress')",
                (now, task_id),
            )
            await conn.execute("COMMIT")

        await _broadcast(
            conversation_id,
            {
                "type": "plan_task_update",
                "task_id": task_id,
                "status": "in_progress",
            },
        )
    except Exception as exc:
        logger.debug("mark_task_in_progress failed: %s", exc)


async def mark_task_completed(
    task_id: str,
    conversation_id: str,
    result: str,
    plan_id: str,
) -> None:
    """Set task to completed, check if plan is done, broadcast."""
    try:
        from core.registry import get_database

        db = get_database()
        if not db:
            return

        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)

        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE chat.webchat_plan_tasks "
                "SET status = 'completed', completed_at = %s, result = %s "
                "WHERE id = %s AND status = 'in_progress'",
                (now, result, task_id),
            )
            await conn.execute(
                "UPDATE chat.webchat_plans SET updated_at = %s WHERE id = %s",
                (now, plan_id),
            )

            # Check if all tasks are done
            pending = await (
                await conn.execute(
                    "SELECT count(*) as cnt FROM chat.webchat_plan_tasks "
                    "WHERE plan_id = %s AND status IN ('pending', 'in_progress')",
                    (plan_id,),
                )
            ).fetchone()

            plan_status_change = None
            if pending and pending["cnt"] == 0:
                await conn.execute(
                    "UPDATE chat.webchat_plans SET status = 'completed', "
                    "updated_at = %s WHERE id = %s",
                    (now, plan_id),
                )
                plan_status_change = "completed"

            await conn.execute("COMMIT")

        event = {
            "type": "plan_task_update",
            "task_id": task_id,
            "status": "completed",
            "result": result,
        }
        if plan_status_change:
            event["plan_status"] = plan_status_change
        await _broadcast(conversation_id, event)

        if plan_status_change:
            logger.info("Plan %s completed (all tasks done)", plan_id)
    except Exception as exc:
        logger.debug("mark_task_completed failed: %s", exc)


def build_task_prompt_injection(task_info: dict) -> str:
    """Build prompt text to inject for the active task."""
    desc = task_info["task_description"]
    desc_block = f"\nDescription: {desc}" if desc else ""

    return (
        f'\n\n[Active Plan: "{task_info["plan_title"]}"]\n'
        f"You are currently working on task {task_info['task_position']} "
        f"of {task_info['total_tasks']}:\n"
        f"Title: {task_info['task_title']}{desc_block}\n\n"
        f"Focus ONLY on this task. When done, report what you did.\n"
        f"Do NOT proceed to the next task — stop and wait for instructions."
    )


def truncate_result(text: str, max_len: int = 200) -> str:
    """Truncate response text for task result summary."""
    if not text:
        return "Completed"
    # Take first line or first max_len chars
    first_line = text.strip().split("\n")[0]
    if len(first_line) <= max_len:
        return first_line
    return first_line[:max_len] + "..."


async def _broadcast(conversation_id: str, event: dict) -> None:
    """Broadcast event to webchat viewers."""
    try:
        from ui.routes.ws_chat import broadcast_to_conversation

        await broadcast_to_conversation(conversation_id, event)
    except Exception as exc:
        logger.debug("Plan executor broadcast failed: %s", exc)

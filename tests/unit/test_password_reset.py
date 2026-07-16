from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_send_invite_email_custom_subject_and_ttl():
    from ui.auth import invite

    user = {"email": "x@example.com", "display_name": "X", "username": "x"}
    captured = {}

    async def fake_call_tool(tool_name, tool_args):
        captured["subject"] = tool_args["subject"]
        captured["body"] = tool_args["body"]
        return "ok"

    with (
        patch("ui.auth.invite.get_client_manager") as mock_cm,
        patch("ui.auth.invite.SystemConfig") as mock_sc,
        patch("ui.auth.invite.get_agent_email_config") as mock_cfg,
    ):
        mock_cm.return_value = AsyncMock(call_tool=fake_call_tool)
        mock_sc.get_param_sync.return_value = "peggy"
        mock_cfg.return_value = {
            "account": "hello@dubhe.it",
            "sender_name": "P",
            "signature": "",
        }

        sent = await invite.send_invite_email(
            user, "https://x/setup?token=t", subject="Reset your password", ttl_hours=1
        )

    assert sent is True
    assert captured["subject"] == "Reset your password"
    assert "1 hour" in captured["body"]


@pytest.mark.asyncio
async def test_send_invite_email_defaults_unchanged():
    from ui.auth import invite

    user = {"email": "x@example.com", "display_name": "X", "username": "x"}
    captured = {}

    async def fake_call_tool(tool_name, tool_args):
        captured["subject"] = tool_args["subject"]
        captured["body"] = tool_args["body"]
        return "ok"

    with (
        patch("ui.auth.invite.get_client_manager") as mock_cm,
        patch("ui.auth.invite.SystemConfig") as mock_sc,
        patch("ui.auth.invite.get_agent_email_config") as mock_cfg,
    ):
        mock_cm.return_value = AsyncMock(call_tool=fake_call_tool)
        mock_sc.get_param_sync.return_value = "peggy"
        mock_cfg.return_value = {
            "account": "hello@dubhe.it",
            "sender_name": "P",
            "signature": "",
        }

        await invite.send_invite_email(user, "https://x/setup?token=t")

    assert captured["subject"] == "GridBear — Set up your password"
    assert "48 hours" in captured["body"]

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


def _user(**over):
    base = {
        "id": 42,
        "username": "dcorio",
        "email": "davide.corio@dubhe.it",
        "display_name": "Davide",
        "is_active": True,
        "password_hash": "$2b$xx",
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_reset_unknown_email_does_nothing():
    with (
        patch("ui.auth.password_reset.User") as mock_user,
        patch("ui.auth.password_reset.generate_token") as mock_gen,
        patch(
            "ui.auth.password_reset.send_invite_email", new_callable=AsyncMock
        ) as mock_send,
    ):
        mock_user.raw_search_sync.return_value = []
        from ui.auth.password_reset import request_password_reset

        result = await request_password_reset("nobody@example.com", "https://x")
    assert result is None
    mock_gen.assert_not_called()
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_reset_eligible_user_issues_token_and_sends():
    with (
        patch("ui.auth.password_reset.User") as mock_user,
        patch("ui.auth.password_reset.generate_token") as mock_gen,
        patch(
            "ui.auth.password_reset.send_invite_email", new_callable=AsyncMock
        ) as mock_send,
    ):
        mock_user.raw_search_sync.return_value = [_user()]
        mock_gen.return_value = "raw-token-xyz"
        from ui.auth.password_reset import request_password_reset

        await request_password_reset("Davide.Corio@Dubhe.it", "https://x")
    mock_gen.assert_called_once_with(42, purpose="reset")
    args, kwargs = mock_send.call_args
    assert "raw-token-xyz" in args[1]
    assert kwargs["ttl_hours"] == 1


@pytest.mark.asyncio
async def test_reset_bot_only_user_skipped():
    with (
        patch("ui.auth.password_reset.User") as mock_user,
        patch("ui.auth.password_reset.generate_token") as mock_gen,
        patch(
            "ui.auth.password_reset.send_invite_email", new_callable=AsyncMock
        ) as mock_send,
    ):
        mock_user.raw_search_sync.return_value = [_user(password_hash=None)]
        from ui.auth.password_reset import request_password_reset

        await request_password_reset("davide.corio@dubhe.it", "https://x")
    mock_gen.assert_not_called()
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_reset_inactive_user_skipped():
    with (
        patch("ui.auth.password_reset.User") as mock_user,
        patch("ui.auth.password_reset.generate_token") as mock_gen,
        patch(
            "ui.auth.password_reset.send_invite_email", new_callable=AsyncMock
        ) as mock_send,
    ):
        mock_user.raw_search_sync.return_value = [_user(is_active=False)]
        from ui.auth.password_reset import request_password_reset

        await request_password_reset("davide.corio@dubhe.it", "https://x")
    mock_gen.assert_not_called()
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_reset_send_failure_swallowed():
    with (
        patch("ui.auth.password_reset.User") as mock_user,
        patch("ui.auth.password_reset.generate_token") as mock_gen,
        patch(
            "ui.auth.password_reset.send_invite_email", new_callable=AsyncMock
        ) as mock_send,
    ):
        mock_user.raw_search_sync.return_value = [_user()]
        mock_gen.return_value = "t"
        mock_send.side_effect = RuntimeError("mcp down")
        from ui.auth.password_reset import request_password_reset

        result = await request_password_reset("davide.corio@dubhe.it", "https://x")
    assert result is None

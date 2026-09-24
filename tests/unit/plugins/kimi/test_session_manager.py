"""Kimi's session manager is the shared one with its own log prefix."""


class TestSessionManager:
    def test_it_carries_the_kimi_log_prefix(self):
        from plugins.kimi.session_manager import SessionManager

        assert SessionManager._log_prefix == "Kimi"

    def test_a_session_round_trips_a_turn(self):
        from plugins.kimi.session_manager import SessionManager

        manager = SessionManager()
        session = manager.get_or_create(None, agent_id="agent-a")
        manager.append_turn(session.session_id, "user", "hello")
        manager.append_turn(session.session_id, "assistant", "hi")
        history = manager.get_history(session.session_id)
        assert [turn["role"] for turn in history] == ["user", "assistant"]

    def test_the_session_type_is_re_exported_under_a_kimi_name(self):
        from core.runners.session_manager import RunnerSession
        from plugins.kimi.session_manager import KimiSession

        assert KimiSession is RunnerSession

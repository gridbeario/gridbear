"""Default OAuth redirect URI for the MS365 consent flow.

Azure matches the redirect URI verbatim against the app registration, so a
default pointing at a route this router does not serve can never complete a
login. The callback is served at /plugins/ms365/callback.
"""

import plugins.ms365.admin.routes as routes


def test_default_redirect_uri_targets_the_real_callback_route():
    assert routes._default_redirect_uri().endswith("/plugins/ms365/callback")


def test_default_redirect_uri_uses_configured_base_url(monkeypatch):
    monkeypatch.setenv("GRIDBEAR_BASE_URL", "https://gridbear.example.com")
    assert (
        routes._default_redirect_uri()
        == "https://gridbear.example.com/plugins/ms365/callback"
    )


def test_default_redirect_uri_tolerates_trailing_slash(monkeypatch):
    monkeypatch.setenv("GRIDBEAR_BASE_URL", "https://gridbear.example.com/")
    assert (
        routes._default_redirect_uri()
        == "https://gridbear.example.com/plugins/ms365/callback"
    )


def test_default_redirect_uri_falls_back_when_base_url_unset(monkeypatch):
    monkeypatch.delenv("GRIDBEAR_BASE_URL", raising=False)
    assert routes._default_redirect_uri().startswith("http://localhost:")


def test_config_fills_redirect_uri_when_never_configured(monkeypatch):
    monkeypatch.setenv("GRIDBEAR_BASE_URL", "https://gridbear.example.com")
    monkeypatch.setattr(routes, "load_plugin_config", lambda _name: {})
    assert (
        routes.get_ms365_config()["redirect_uri"]
        == "https://gridbear.example.com/plugins/ms365/callback"
    )


def test_config_fills_redirect_uri_when_stored_value_is_blank(monkeypatch):
    monkeypatch.setenv("GRIDBEAR_BASE_URL", "https://gridbear.example.com")
    monkeypatch.setattr(
        routes, "load_plugin_config", lambda _name: {"redirect_uri": ""}
    )
    assert (
        routes.get_ms365_config()["redirect_uri"]
        == "https://gridbear.example.com/plugins/ms365/callback"
    )


def test_config_preserves_an_explicitly_configured_redirect_uri(monkeypatch):
    monkeypatch.setenv("GRIDBEAR_BASE_URL", "https://gridbear.example.com")
    monkeypatch.setattr(
        routes,
        "load_plugin_config",
        lambda _name: {"redirect_uri": "https://custom.example.org/cb"},
    )
    assert routes.get_ms365_config()["redirect_uri"] == "https://custom.example.org/cb"

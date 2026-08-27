"""OAuth delegated scopes requested during the MS365 consent flow.

Reading documents shared BY OTHERS goes through the Graph Shares API, which
requires a scope covering files the user can access but does not own. Plain
``Files.ReadWrite`` only covers the user's own drive, so a token minted without
``.All`` coverage gets a 403 on every shared item.
"""

import plugins.ms365.admin.routes as routes


def test_owner_scopes_allow_reading_shared_documents():
    assert "Files.Read.All" in routes._scopes_for_role("owner")


def test_guest_scopes_allow_reading_shared_documents():
    # Files.ReadWrite.All subsumes Files.Read.All.
    assert "Files.ReadWrite.All" in routes._scopes_for_role("guest")


def test_owner_scopes_keep_existing_grants():
    scopes = routes._scopes_for_role("owner").split()
    for required in (
        "User.Read",
        "Files.ReadWrite",
        "Tasks.ReadWrite",
        "Sites.ReadWrite.All",
        "Group.Read.All",
    ):
        assert required in scopes


def test_both_roles_request_offline_access_for_silent_refresh():
    assert "offline_access" in routes._scopes_for_role("owner")
    assert "offline_access" in routes._scopes_for_role("guest")


def test_unknown_role_falls_back_to_guest_scopes():
    assert routes._scopes_for_role("nonsense") == routes._scopes_for_role("guest")

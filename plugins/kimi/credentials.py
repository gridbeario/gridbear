"""Kimi credential resolution: the Moonshot API key, and nothing else.

The Moonshot HTTP API accepts exactly one credential shape:
``Authorization: Bearer sk-...``, an API key entered on the plugin's admin
page (``/plugins/kimi``) and stored in the vault under
``API_KEY_VAULT_KEY``. ``read_api_key()`` reads that key back for a turn —
the only accessor this module has, and the only one ``api_backend.py``
calls.

There is no refresh accessor, no access/grant digests, no degraded mark, no
OAuth vault block. The design's original Credentials section anticipated a
Kimi Code CLI backend authenticating via device-code OAuth, with a
vault-held access/refresh token pair this module would have refreshed and
demoted on rejection. That backend was dropped this iteration: the real
Kimi CLI lacks the flags the design assumed, and the Moonshot HTTP API —
the only backend this plugin ships — authenticates with a bearer API key
and nothing else. There is no credential held for the Kimi Code
subscription either: ``kimi login`` authenticates via device code and the
CLI stores that credential itself, so there is nothing here to hold,
refresh, or revoke on its behalf.

None of this is an omission: the design itself declares the branch ("the
precedence rule collapses to the API key" when the OAuth path does not
ship), and this module implements exactly that collapsed state.
"""

# The vault key the user's Moonshot API key is stored under. Load-bearing:
# ui/routes/plugins.py writes the key entered on /plugins/kimi under the
# manifest's config_schema.moonshot_api_key.env name, which is this string.
# Getting it wrong means reading a key nobody wrote.
API_KEY_VAULT_KEY = "MOONSHOT_API_KEY"

# Originally copied verbatim from the design doc's message table, whose
# wording named a "connect the Kimi Code subscription" login button — the
# {"type": "login", "label": "OAuth Login"} entry `cli_meta.auth_actions`
# carried before this same task removed it, since that button always posted
# to a route that fails closed (no OAuth in this build). Rewritten below to
# name only the control the plugin page actually draws: the API Key field.
# The other three rows of the design doc's table are still deliberately not
# defined here: a too-short OAuth token and a rejected OAuth credential
# describe states that cannot occur in this build, and "OAuth is not
# supported by the installed Kimi CLI" is not just unreachable but false for
# this architecture — it implies a CLI is present but limited, when no CLI
# backend ships here at all. A message for an unreachable state is a string
# that can only ever be wrong; that one would also send an operator to
# inspect a CLI installation that does not exist. A future OAuth-capable
# iteration writes its own message against whatever gate condition it
# actually adds.
MSG_NO_CREDENTIAL = (
    "No Kimi credential configured: enter your Moonshot API Key on the "
    "plugin page (stored as MOONSHOT_API_KEY)."
)


def read_api_key() -> str:
    """Read the Moonshot API key from the vault, "" when absent.

    `secrets_manager.get_plain()` already returns "" (never None, never an
    exception) when the vault or its encryption is unavailable, so an
    absent key and an unreachable vault look identical here by design —
    both mean "no key to use this turn".

    A whitespace-only stored value is treated as absent too: it cannot
    possibly be a valid `sk-...` key, and passing it through would send a
    bearer token guaranteed to 401 instead of the clear "not configured"
    message.
    """
    from ui.secrets_manager import secrets_manager

    raw = secrets_manager.get_plain(API_KEY_VAULT_KEY)
    return (raw or "").strip()

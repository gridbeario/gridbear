"""Kimi credential resolution: the Moonshot API key, and nothing else.

The Moonshot HTTP API accepts exactly one credential shape:
``Authorization: Bearer sk-...``, an API key entered on the plugin's admin
page (``/plugins/kimi``) and stored in the vault under
``API_KEY_VAULT_KEY``. This module reads that key back for a turn.

What this module deliberately does NOT do, and why:

* No four-outcome refresh accessor, no access/grant digests, no degraded
  mark, no OAuth vault block. The design's original Credentials section
  anticipated a Kimi Code CLI backend authenticating via device-code OAuth,
  with a vault-held access/refresh token pair this module would refresh and
  demote on rejection. That backend was dropped this iteration: the real
  Kimi CLI lacks the flags the design assumed, and the Moonshot HTTP API —
  the only backend this plugin ships — authenticates with a bearer API key
  and nothing else.
* No credential held for the Kimi Code subscription at all. ``kimi login``
  authenticates via device code and the CLI stores that credential itself;
  there is no token of ours to write, refresh, or revoke, so there is
  nothing here to hold it.
* No ``expires_at_ms`` / ``degraded_now`` on ``ResolvedCredential``. An API
  key does not expire and there is no path that can degrade it — those
  fields described OAuth-path states that have no subject here.

None of this is an omission: the design itself declares the branch ("the
precedence rule collapses to the API key" when the OAuth path does not
ship), and this module implements exactly that collapsed state.
"""

from dataclasses import dataclass
from enum import Enum

# The vault key the user's Moonshot API key is stored under. Load-bearing:
# ui/routes/plugins.py writes the key entered on /plugins/kimi under the
# manifest's config_schema.moonshot_api_key.env name, which is this string.
# Getting it wrong means reading a key nobody wrote.
API_KEY_VAULT_KEY = "MOONSHOT_API_KEY"

# Copied verbatim from the design doc's message table. The other three rows
# of that table are deliberately not defined here: a too-short OAuth token
# and a rejected OAuth credential describe states that cannot occur in this
# build, and "OAuth is not supported by the installed Kimi CLI" is not just
# unreachable but false for this architecture — it implies a CLI is present
# but limited, when no CLI backend ships here at all. A message for an
# unreachable state is a string that can only ever be wrong; that one would
# also send an operator to inspect a CLI installation that does not exist.
# A future OAuth-capable iteration writes its own message against whatever
# gate condition it actually adds.
MSG_NO_CREDENTIAL = (
    "No Kimi credential configured: connect the Kimi Code subscription from "
    "the plugin page, or set MOONSHOT_API_KEY"
)


class CredentialKind(str, Enum):
    """What kind of credential `resolve_for_turn` produced.

    Kept as an enum rather than a bare string because `resolve_for_turn`
    returns it, and a later CLI iteration — if an OAuth path ever ships —
    would add a member here rather than replace this one.
    """

    API_KEY = "api_key"


@dataclass
class ResolvedCredential:
    """The credential a turn should use, or the message to fail with.

    No `expires_at_ms` / `degraded_now`: both described OAuth-path states
    (a token's recorded expiry, a subscription demoted after a rejected
    refresh) that have no subject when the only credential kind is an API
    key, which does not expire and cannot be degraded.
    """

    kind: CredentialKind
    secret: str
    message: str | None


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


def resolve_for_turn() -> ResolvedCredential:
    """Produce the credential this turn will use, or the message to fail with.

    There is exactly one source: the vault-held API key. No OAuth block is
    ever consulted — see the module docstring for why that path does not
    exist in this build.
    """
    api_key = read_api_key()
    if api_key:
        return ResolvedCredential(CredentialKind.API_KEY, api_key, None)
    return ResolvedCredential(CredentialKind.API_KEY, "", MSG_NO_CREDENTIAL)

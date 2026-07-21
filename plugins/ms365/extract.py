"""Pure helpers for the ms365 MCP server: Office/PDF text extraction and
Microsoft Graph sharing-URL encoding. No plugin state, no I/O."""

import base64


def encode_sharing_url(url: str) -> str:
    """Encode a sharing URL into a Graph shareId (already `u!`-prefixed).

    Callers use it as `/shares/{enc}/driveItem` — NOT `/shares/u!{enc}` (that
    would double the prefix to `u!u!…` and 400).
    """
    b64 = base64.b64encode(url.encode("utf-8")).decode("ascii")
    return "u!" + b64.rstrip("=").replace("/", "_").replace("+", "-")

from plugins.ms365.extract import encode_sharing_url


def test_encode_sharing_url_microsoft_example():
    # From Microsoft Graph docs: this exact URL encodes to this exact shareId.
    url = "https://onedrive.live.com/redir?resid=1231244193912!12&authKey=Foo"
    assert (
        encode_sharing_url(url)
        == "u!aHR0cHM6Ly9vbmVkcml2ZS5saXZlLmNvbS9yZWRpcj9yZXNpZD0xMjMxMjQ0MTkzOTEyITEyJmF1dGhLZXk9Rm9v"
    )


def test_encode_sharing_url_is_url_safe_and_unpadded():
    # A URL whose base64 contains '+' and '/' must come back with '-'/'_' and no '='.
    enc = encode_sharing_url("https://x/??>>>")  # base64 of this contains + and /
    assert enc.startswith("u!")
    body = enc[2:]
    assert "+" not in body and "/" not in body and "=" not in body

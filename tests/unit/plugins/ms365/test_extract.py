import io

from plugins.ms365.extract import encode_sharing_url, extract_text


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


def _docx_bytes(paragraphs):
    from docx import Document

    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _xlsx_bytes(rows):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_extract_docx():
    out = extract_text(_docx_bytes(["Hello world", "Second line"]), "shared.docx")
    assert "Hello world" in out and "Second line" in out


def test_extract_xlsx():
    out = extract_text(_xlsx_bytes([["Name", "Qty"], ["Widget", 5]]), "sheet.xlsx")
    assert "Name" in out and "Widget" in out and "5" in out


def test_extract_txt():
    assert extract_text(b"plain text here", "notes.txt") == "plain text here"


def test_extract_uppercase_extension_g5():
    out = extract_text(_docx_bytes(["Case test"]), "SHARED.DOCX")
    assert "Case test" in out


def test_extract_unsupported_format():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    out = extract_text(png, "image.png")
    assert "unsupported format" in out.lower() and ".png" in out


def test_extract_empty_docx_g4():
    out = extract_text(_docx_bytes([]), "empty.docx")
    assert out != ""
    assert "no extractable text" in out.lower()


def test_extract_corrupt_docx_returns_note():
    out = extract_text(b"not a real docx", "broken.docx")
    assert out != ""
    assert "broken.docx" in out


def test_extract_no_extension_and_dotfile():
    assert "no extension" in extract_text(b"data", "README").lower()
    assert "no extension" in extract_text(b"data", ".bashrc").lower()


def test_extract_truncation_marker():
    big = "x" * 300
    out = extract_text(big.encode(), "big.txt", max_chars=100)
    assert out.endswith("...(truncated)")
    assert len(out) <= 100 + len("\n...(truncated)")

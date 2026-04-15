"""Tests for whatsapp_api formatting module."""

from plugins.whatsapp_api.formatting import markdown_to_whatsapp, split_message


def test_bold_conversion():
    assert "*hello*" in markdown_to_whatsapp("**hello**")


def test_bold_no_double_asterisks():
    result = markdown_to_whatsapp("**bold text**")
    assert "**" not in result
    assert "*bold text*" in result


def test_strikethrough_conversion():
    result = markdown_to_whatsapp("~~deleted~~")
    assert "~~" not in result
    assert "~deleted~" in result


def test_header_conversion():
    result = markdown_to_whatsapp("## My Header")
    assert "*MY HEADER*" in result
    assert "##" not in result


def test_link_conversion():
    result = markdown_to_whatsapp("[click here](https://example.com)")
    assert "click here (https://example.com)" in result
    assert "[" not in result


def test_bullet_list_conversion():
    result = markdown_to_whatsapp("- item one\n- item two")
    assert "• item one" in result
    assert "• item two" in result


def test_code_block_preserved():
    text = "```\nsome code\n```"
    result = markdown_to_whatsapp(text)
    assert "```" in result
    assert "some code" in result


def test_inline_code_preserved():
    result = markdown_to_whatsapp("use `foo()` here")
    assert "`foo()`" in result


def test_italic_unchanged():
    result = markdown_to_whatsapp("_italic text_")
    assert "_italic text_" in result


def test_split_respects_limit():
    long_text = "word " * 1000
    chunks = split_message(long_text, max_len=4096)
    assert all(len(c) <= 4096 for c in chunks)
    assert len(chunks) >= 2


def test_split_short_message():
    chunks = split_message("short message")
    assert chunks == ["short message"]


def test_split_preserves_content():
    long_text = "word " * 1000
    chunks = split_message(long_text, max_len=4096)
    rejoined = " ".join(c.strip() for c in chunks)
    assert rejoined == long_text.strip()


def test_split_prefers_paragraph_break():
    text = "A" * 3000 + "\n\n" + "B" * 3000
    chunks = split_message(text, max_len=4096)
    assert len(chunks) == 2
    assert chunks[0].strip() == "A" * 3000
    assert chunks[1].strip() == "B" * 3000


def test_split_empty_string():
    chunks = split_message("")
    assert chunks == [""]

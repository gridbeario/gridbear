"""WhatsApp message formatting utilities.

Converts standard Markdown to WhatsApp-compatible formatting and handles
message splitting for the 4096 character limit.
"""

import re


def markdown_to_whatsapp(text: str) -> str:
    """Convert standard Markdown to WhatsApp formatting.

    WhatsApp uses:
    - *bold* (not **bold**)
    - _italic_ (same)
    - ~strikethrough~ (not ~~strike~~)
    - ```monospace``` (same)
    """
    # Preserve code blocks first (don't process their content)
    code_blocks = []

    def save_code_block(m):
        code_blocks.append(m.group(0))
        return f"\x00CODE{len(code_blocks) - 1}\x00"

    text = re.sub(r"```[\s\S]*?```", save_code_block, text)

    # Preserve inline code
    inline_codes = []

    def save_inline_code(m):
        inline_codes.append(m.group(0))
        return f"\x00INLINE{len(inline_codes) - 1}\x00"

    text = re.sub(r"`[^`]+`", save_inline_code, text)

    # Headers → *HEADER* (bold uppercase)
    text = re.sub(
        r"^#{1,6}\s+(.+)$",
        lambda m: f"*{m.group(1).upper()}*",
        text,
        flags=re.MULTILINE,
    )

    # Bold: **text** → *text*
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)

    # Strikethrough: ~~text~~ → ~text~
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)

    # Links: [text](url) → text (url)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)

    # Bullet lists: - item → • item
    text = re.sub(r"^[-*]\s+", "• ", text, flags=re.MULTILINE)

    # Numbered list formatting: keep as-is

    # Tables → code block
    table_pattern = re.compile(
        r"^(\|.+\|)\n(\|[-| :]+\|)\n((?:\|.+\|\n?)+)", re.MULTILINE
    )
    text = table_pattern.sub(lambda m: f"```\n{m.group(0).strip()}\n```", text)

    # Restore code blocks and inline code
    for i, block in enumerate(code_blocks):
        text = text.replace(f"\x00CODE{i}\x00", block)
    for i, code in enumerate(inline_codes):
        text = text.replace(f"\x00INLINE{i}\x00", code)

    return text


def split_message(text: str, max_len: int = 4096) -> list[str]:
    """Split message into chunks respecting WhatsApp's character limit.

    Splits on paragraph breaks > line breaks > sentence ends > spaces.
    """
    if len(text) <= max_len:
        return [text]

    chunks = []
    remaining = text

    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break

        # Find best split point
        split_at = _find_split_point(remaining, max_len)
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()

    return [c for c in chunks if c.strip()]


def _find_split_point(text: str, max_len: int) -> int:
    """Find the best position to split text within max_len."""
    # Try splitting at paragraph break
    idx = text.rfind("\n\n", 0, max_len)
    if idx > max_len // 4:
        return idx + 2

    # Try splitting at line break
    idx = text.rfind("\n", 0, max_len)
    if idx > max_len // 4:
        return idx + 1

    # Try splitting at sentence end
    idx = text.rfind(". ", 0, max_len)
    if idx > max_len // 4:
        return idx + 2

    # Try splitting at space
    idx = text.rfind(" ", 0, max_len)
    if idx > max_len // 4:
        return idx + 1

    # Hard split at max_len
    return max_len

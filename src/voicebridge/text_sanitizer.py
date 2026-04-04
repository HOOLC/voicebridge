from __future__ import annotations

import re
import unicodedata


def sanitize_spoken_text(raw_text: str) -> str:
    lines: list[str] = []
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower_line = line.lower()
        if lower_line.startswith(("thinking", "tool call", "tool output", "command output", "stderr", "stdout")):
            continue
        if line.startswith("```") or line.startswith("<thinking") or line.startswith("</thinking"):
            continue
        cleaned = _strip_tts_unfriendly_chars(line)
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines).strip()


def _strip_tts_unfriendly_chars(text: str) -> str:
    sanitized_chars: list[str] = []
    for char in text:
        if char == "*":
            continue
        if _is_emoji_or_symbol(char):
            continue
        sanitized_chars.append(char)

    cleaned = "".join(sanitized_chars)
    cleaned = cleaned.replace("`", "")
    cleaned = cleaned.replace("#", "")
    cleaned = cleaned.replace("|", "，")
    cleaned = cleaned.replace("\\", "")
    cleaned = re.sub(r"[_~^=<>@]+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"[ ]*([，。！？、,.!?；;：:])[ ]*", r"\1", cleaned)
    return cleaned.strip()


def _is_emoji_or_symbol(char: str) -> bool:
    category = unicodedata.category(char)
    if category == "So":
        return True
    codepoint = ord(char)
    return (
        0x1F000 <= codepoint <= 0x1FAFF
        or 0x2600 <= codepoint <= 0x27BF
        or 0xFE00 <= codepoint <= 0xFE0F
    )

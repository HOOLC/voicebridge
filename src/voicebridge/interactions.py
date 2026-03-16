from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class TurnSource(StrEnum):
    VOICE = "voice"
    FEISHU = "feishu"
    SCHEDULE = "schedule"


class SessionScope(StrEnum):
    SHARED = "shared"
    TRANSIENT = "transient"


class OutputChannel(StrEnum):
    VOICE = "voice"
    FEISHU = "feishu"


@dataclass(slots=True)
class BridgeTurn:
    turn_id: int
    source: TurnSource
    session_scope: SessionScope
    text: str
    output_targets: tuple[OutputChannel, ...]
    label: str = ""

    @property
    def deliver_to_voice(self) -> bool:
        return OutputChannel.VOICE in self.output_targets

    @property
    def deliver_to_feishu(self) -> bool:
        return OutputChannel.FEISHU in self.output_targets

    @property
    def prefers_voice_reply(self) -> bool:
        return self.deliver_to_voice and self.source is TurnSource.VOICE


@dataclass(slots=True)
class FeishuMessage:
    msg_type: str
    content: dict[str, Any] | list[Any] | str
    preview_text: str

    def serialized_content(self) -> str:
        if isinstance(self.content, str):
            return self.content
        return json.dumps(self.content, ensure_ascii=False)


@dataclass(slots=True)
class AssistantReply:
    preview_text: str
    voice_text: str = ""
    feishu_message: FeishuMessage | None = None


def build_text_feishu_message(text: str) -> FeishuMessage:
    clean_text = text.strip()
    return FeishuMessage(
        msg_type="text",
        content={"text": clean_text},
        preview_text=clean_text,
    )


def parse_assistant_reply(raw_reply: str, *, prefer_voice_reply: bool) -> AssistantReply:
    raw_text = raw_reply.strip()
    if not raw_text or _is_silence(raw_text):
        return AssistantReply(preview_text="")

    if prefer_voice_reply:
        voice_text = _sanitize_voice_reply(raw_text)
        if not voice_text:
            return AssistantReply(preview_text="")
        return AssistantReply(
            preview_text=voice_text,
            voice_text=voice_text,
            feishu_message=build_text_feishu_message(voice_text),
        )

    structured = _parse_structured_feishu_message(raw_text)
    if structured is not None:
        return AssistantReply(
            preview_text=structured.preview_text,
            feishu_message=structured,
        )

    text_reply = _sanitize_text_reply(raw_text)
    if not text_reply:
        return AssistantReply(preview_text="")
    return AssistantReply(
        preview_text=text_reply,
        feishu_message=build_text_feishu_message(text_reply),
    )


def _parse_structured_feishu_message(raw_text: str) -> FeishuMessage | None:
    payload = _load_json_object(raw_text)
    if not isinstance(payload, dict):
        return None

    payload = _normalize_nested_table_card_payload(payload)

    vb_type = str(payload.get("vb_type", "")).strip().lower()
    if vb_type == "report_card":
        return _build_report_card_message(payload)
    if vb_type == "table_card":
        return _build_table_card_message(payload)

    msg_type = str(payload.get("msg_type", "")).strip()
    content = payload.get("content")
    if not msg_type or content in (None, ""):
        return None

    preview_text = str(payload.get("preview_text") or "").strip()
    if not preview_text:
        if msg_type == "text" and isinstance(content, dict):
            preview_text = str(content.get("text") or "").strip()
        else:
            preview_text = f"[{msg_type}]"

    return FeishuMessage(msg_type=msg_type, content=content, preview_text=preview_text)


def _normalize_nested_table_card_payload(payload: dict[str, Any]) -> dict[str, Any]:
    table_card = payload.get("table_card")
    if not isinstance(table_card, dict):
        return payload

    columns = _normalize_string_items(table_card.get("columns"))
    raw_rows = table_card.get("rows")
    rows: list[list[str]] = []
    if isinstance(raw_rows, list):
        for item in raw_rows:
            if isinstance(item, dict) and columns:
                rows.append([str(item.get(column, "")).strip() for column in columns])
            elif isinstance(item, list):
                rows.append([str(cell).strip() for cell in item])

    notes_value = payload.get("notes")
    notes: list[str] = []
    if isinstance(notes_value, dict):
        for key, value in notes_value.items():
            if isinstance(value, list):
                for line in value:
                    line_text = str(line).strip()
                    if line_text:
                        notes.append(f"{key}：{line_text}")
            else:
                value_text = str(value).strip()
                if value_text:
                    notes.append(f"{key}：{value_text}")
    else:
        notes = _normalize_string_items(notes_value)

    title = (
        _clean_inline_text(payload.get("title"))
        or _clean_inline_text(table_card.get("title"))
        or "巡检结果"
    )
    summary = (
        _clean_multiline_text(payload.get("summary"))
        or _clean_multiline_text(table_card.get("summary"))
        or ""
    )
    preview_text = (
        _clean_inline_text(payload.get("preview_text"))
        or _clean_inline_text(table_card.get("preview_text"))
        or summary
        or title
    )

    return {
        "vb_type": "table_card",
        "title": title,
        "summary": summary,
        "columns": columns,
        "rows": rows,
        "notes": notes,
        "preview_text": preview_text,
    }


def _load_json_object(raw_text: str) -> Any:
    candidates = [raw_text.strip()]
    fenced = _strip_json_fence(raw_text)
    if fenced and fenced not in candidates:
        candidates.append(fenced)
    extracted = _extract_first_json_object(raw_text)
    if extracted and extracted not in candidates:
        candidates.append(extracted)

    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            repaired = _repair_json_candidate(candidate)
            if not repaired or repaired == candidate:
                continue
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                continue
    return None


def _strip_json_fence(raw_text: str) -> str:
    text = raw_text.strip()
    if not text.startswith("```") or not text.endswith("```"):
        return ""
    lines = text.splitlines()
    if not lines:
        return ""
    if lines[0].strip().lower() not in {"```", "```json", "```javascript", "```js"}:
        return ""
    if len(lines) < 2:
        return ""
    return "\n".join(lines[1:-1]).strip()


def _extract_first_json_object(raw_text: str) -> str:
    text = raw_text.strip()
    start = text.find("{")
    if start < 0:
        return ""

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue
        if char == "{":
            depth += 1
            continue
        if char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    return ""


def _repair_json_candidate(raw_text: str) -> str:
    text = raw_text.strip()
    if not text:
        return ""

    repaired = text
    repaired = repaired.lstrip("\ufeff")
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    repaired = re.sub(r",\s*\"\"\s*(?=[}\],])", "", repaired)
    repaired = re.sub(r"(\[\s*)\"\"\s*,", r"\1", repaired)
    repaired = re.sub(r",\s*\"\"\s*(\])", r"\1", repaired)
    repaired = re.sub(r"\[\s*,", "[", repaired)
    for _ in range(8):
        try:
            json.loads(repaired)
            break
        except json.JSONDecodeError as error:
            updated = _repair_json_from_error(repaired, error)
            if not updated or updated == repaired:
                break
            repaired = updated
    return repaired.strip()


def _repair_json_from_error(text: str, error: json.JSONDecodeError) -> str:
    pos = max(0, min(error.pos, len(text)))
    current = text[pos] if pos < len(text) else ""
    previous = _previous_non_whitespace_char(text, pos)

    if error.msg == "Expecting ',' delimiter":
        if current == "}" and previous not in {"", ",", "{", "["}:
            return text[:pos] + "]" + text[pos:]
        if current == "]" and previous not in {"", ",", "{", "["}:
            return text[:pos] + "}" + text[pos:]

    if error.msg.startswith("Unterminated string"):
        return text + '"'

    return text


def _previous_non_whitespace_char(text: str, pos: int) -> str:
    for index in range(pos - 1, -1, -1):
        char = text[index]
        if not char.isspace():
            return char
    return ""


def _build_report_card_message(payload: dict[str, Any]) -> FeishuMessage | None:
    title = _clean_inline_text(payload.get("title")) or "状态更新"
    summary = _clean_multiline_text(payload.get("summary"))
    preview_text = _clean_inline_text(payload.get("preview_text")) or summary or title

    elements: list[dict[str, Any]] = []
    if summary:
        elements.append(_markdown_div(summary))

    facts = _normalize_fact_items(payload.get("facts"))
    if facts:
        for chunk_start in range(0, len(facts), 2):
            fields = []
            for item in facts[chunk_start : chunk_start + 2]:
                fields.append(
                    {
                        "is_short": True,
                        "text": {
                            "tag": "lark_md",
                            "content": f"**{_escape_markdown(item['label'])}**\n{_escape_markdown(item['value'])}",
                        },
                    }
                )
            elements.append({"tag": "div", "fields": fields})

    sections = _normalize_section_items(payload.get("sections"))
    for section in sections:
        lines = [f"**{_escape_markdown(section['title'])}**"]
        lines.extend(f"- {_escape_markdown(line)}" for line in section["bullets"])
        elements.append(_markdown_div("\n".join(lines)))

    for title_text, key in (
        ("卡点 / 风险", "blockers"),
        ("需要用户决策", "decisions"),
        ("下一步", "next_steps"),
    ):
        bullets = _normalize_string_items(payload.get(key))
        if bullets:
            lines = [f"**{title_text}**"]
            lines.extend(f"- {_escape_markdown(line)}" for line in bullets)
            elements.append(_markdown_div("\n".join(lines)))

    if not elements:
        elements.append({"tag": "markdown", "content": preview_text})

    content = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": title[:80]},
        },
        "body": {"elements": elements},
    }
    return FeishuMessage(msg_type="interactive", content=content, preview_text=preview_text)


def _build_table_card_message(payload: dict[str, Any]) -> FeishuMessage | None:
    title = _clean_inline_text(payload.get("title")) or "表格更新"
    summary = _clean_multiline_text(payload.get("summary"))
    preview_text = _clean_inline_text(payload.get("preview_text")) or summary or title
    columns = _normalize_string_items(payload.get("columns"))
    rows = _normalize_table_rows(payload.get("rows"), columns)
    if not columns or not rows:
        return None

    elements: list[dict[str, Any]] = []
    if summary:
        elements.append({"tag": "markdown", "content": summary})

    elements.append(_build_column_set(columns, is_header=True))
    elements.append({"tag": "hr"})
    for row in rows:
        padded_row = row[: len(columns)] + [""] * max(0, len(columns) - len(row))
        elements.append(_build_column_set(padded_row[: len(columns)], is_header=False))
        elements.append({"tag": "hr"})

    notes = _normalize_string_items(payload.get("notes"))
    if notes:
        note_lines = ["**补充**"]
        note_lines.extend(f"- {_escape_markdown(line)}" for line in notes)
        elements.append({"tag": "markdown", "content": "\n".join(note_lines)})

    content = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": title[:80]},
        },
        "body": {"elements": elements},
    }
    return FeishuMessage(msg_type="interactive", content=content, preview_text=preview_text)


def _sanitize_voice_reply(raw_text: str) -> str:
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


def _sanitize_text_reply(raw_text: str) -> str:
    lines: list[str] = []
    for raw_line in raw_text.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        lower_line = line.lower().strip()
        if lower_line.startswith(("thinking", "tool call", "tool output", "command output", "stderr", "stdout")):
            continue
        if lower_line.startswith("<thinking") or lower_line.startswith("</thinking"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _is_silence(raw_text: str) -> bool:
    return raw_text.strip().lower() in {"[silence]", "silence", "<silence>"}


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


def _markdown_div(content: str) -> dict[str, Any]:
    return {
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": content,
        },
    }


def _normalize_fact_items(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    facts: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        label = _clean_inline_text(item.get("label"))
        fact_value = _clean_inline_text(item.get("value"))
        if not label or not fact_value:
            continue
        facts.append({"label": label, "value": fact_value})
    return facts


def _normalize_section_items(value: Any) -> list[dict[str, list[str] | str]]:
    if not isinstance(value, list):
        return []
    sections: list[dict[str, list[str] | str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        title = _clean_inline_text(item.get("title"))
        bullets = _normalize_string_items(item.get("bullets"))
        if not title or not bullets:
            continue
        sections.append({"title": title, "bullets": bullets})
    return sections


def _normalize_table_rows(value: Any, columns: list[str]) -> list[list[str]]:
    if not isinstance(value, list):
        return []
    rows: list[list[str]] = []
    for item in value:
        if isinstance(item, list):
            row = [_clean_inline_text(cell) for cell in item]
            row = [cell for cell in row if cell or len(row) == 1]
            if row:
                rows.append(row)
            continue
        if isinstance(item, dict):
            row = [_clean_inline_text(item.get(column)) for column in columns]
            if any(row):
                rows.append(row)
    return rows


def _normalize_string_items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        cleaned = _clean_multiline_text(item)
        if cleaned:
            items.append(cleaned)
    return items


def _clean_inline_text(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _clean_multiline_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines).strip()


def _escape_markdown(text: str) -> str:
    return text.replace("\\", "\\\\")


def _escape_table_cell(text: str) -> str:
    return _escape_markdown(text).replace("|", "\\|").replace("\n", "<br>")


def _build_column_set(cells: list[str], *, is_header: bool) -> dict[str, Any]:
    columns: list[dict[str, Any]] = []
    for cell in cells:
        content = _escape_markdown(cell) or "-"
        if is_header:
            content = f"**{content}**"
        columns.append(
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": [
                    {
                        "tag": "markdown",
                        "content": content,
                    }
                ],
            }
        )

    return {
        "tag": "column_set",
        "flex_mode": "none",
        "horizontal_spacing": "8px",
        "background_style": "grey" if is_header else "default",
        "columns": columns,
    }

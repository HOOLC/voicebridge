from __future__ import annotations

import json
import subprocess
from typing import Any

DEFAULT_WORKSPACE_DIRS = ("~/Quant", "~/QuantDev")

_WSL_SESSION_PROBE = r"""
from __future__ import annotations

from collections import deque
from pathlib import Path
import json
import time
import sys


def _clean_text(value: str) -> str:
    text = (value or "").replace("\r", "\n")
    parts = [part.strip() for part in text.splitlines() if part.strip()]
    collapsed = " / ".join(parts)
    return collapsed[:400]


def _extract_message_text(payload: dict) -> str:
    texts: list[str] = []
    for item in payload.get("content") or []:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
    return _clean_text("\n".join(texts))


def _match_workspace(cwd: str, workspace_specs: list[dict]) -> dict | None:
    for spec in workspace_specs:
        prefix = spec["cwd_prefix"]
        if cwd == prefix or cwd.startswith(prefix + "/"):
            return spec
    return None


def _summarize_session(path: Path, *, now_ts: float) -> dict:
    tail_lines: deque[str] = deque(maxlen=80)
    first_line = ""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        first_line = handle.readline().strip()
        for raw_line in handle:
            tail_lines.append(raw_line.rstrip("\n"))

    meta = {}
    if first_line:
        try:
            meta = json.loads(first_line)
        except json.JSONDecodeError:
            meta = {}

    payload = meta.get("payload") or {}
    session_id = str(payload.get("id", "")).strip()
    cwd = str(payload.get("cwd", "")).strip()

    last_role = ""
    last_excerpt = ""
    last_event_type = ""
    last_event_at = ""
    last_task_started = ""
    last_task_complete = ""
    error_hint = ""

    for raw_line in tail_lines:
        compact = raw_line.lower()
        if (
            '"type":"error"' in compact
            or "traceback (most recent call last)" in compact
            or '"failed"' in compact
            or " panic " in f" {compact} "
        ):
            error_hint = _clean_text(raw_line)

        try:
            item = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        timestamp = str(item.get("timestamp", "")).strip()
        item_type = str(item.get("type", "")).strip()
        item_payload = item.get("payload") or {}

        if item_type == "response_item" and isinstance(item_payload, dict):
            payload_type = str(item_payload.get("type", "")).strip()
            if payload_type == "message":
                text = _extract_message_text(item_payload)
                if text:
                    last_role = str(item_payload.get("role", "")).strip()
                    last_excerpt = text
                    last_event_type = f"message:{last_role or 'unknown'}"
                    last_event_at = timestamp
            elif payload_type == "function_call_output" and not last_excerpt:
                output = _clean_text(str(item_payload.get("output", "")))
                if output:
                    last_role = "tool"
                    last_excerpt = output
                    last_event_type = "tool_output"
                    last_event_at = timestamp
            continue

        if item_type != "event_msg" or not isinstance(item_payload, dict):
            continue

        event_type = str(item_payload.get("type", "")).strip()
        if event_type:
            last_event_type = event_type
            last_event_at = timestamp
        if event_type == "task_started":
            last_task_started = timestamp
        elif event_type == "task_complete":
            last_task_complete = timestamp
        elif event_type == "user_message":
            message = _clean_text(str(item_payload.get("message", "")))
            if message:
                last_role = "user"
                last_excerpt = message
                last_event_type = "user_message"
                last_event_at = timestamp

    stat = path.stat()
    age_minutes = int(max(0.0, (now_ts - stat.st_mtime) / 60.0))
    status_hint = "idle"
    if error_hint:
        status_hint = "errored"
    elif last_task_started and (not last_task_complete or last_task_started > last_task_complete):
        status_hint = "running"
    elif last_role == "user":
        status_hint = "waiting-reply"
    if age_minutes > 180 and status_hint != "errored":
        status_hint = "stale"

    return {
        "session_id": session_id or path.stem,
        "file": str(path),
        "cwd": cwd,
        "last_activity": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(stat.st_mtime)),
        "age_minutes": age_minutes,
        "status_hint": status_hint,
        "last_role": last_role or "",
        "last_event_type": last_event_type or "",
        "last_event_at": last_event_at or "",
        "last_excerpt": last_excerpt or "(no recent text payload)",
    }


request = json.loads(sys.argv[1])
lookback_hours = max(int(request.get("lookback_hours", 12)), 1)
lookback_seconds = lookback_hours * 3600
limit_per_workspace = max(int(request.get("limit_per_workspace", 3)), 1)
scan_limit = max(int(request.get("scan_limit", 200)), limit_per_workspace)
now_ts = time.time()
session_root = Path.home() / ".codex" / "sessions"

workspace_specs = []
for raw_spec in request.get("workspaces") or []:
    label = str(raw_spec.get("label", "")).strip()
    raw_prefix = str(raw_spec.get("cwd_prefix", "")).strip()
    if not label or not raw_prefix:
        continue
    workspace_specs.append(
        {
            "label": label,
            "cwd_prefix": str(Path(raw_prefix).expanduser()),
            "matched_count": 0,
            "sessions": [],
        }
    )

probe = {
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now_ts)),
    "session_root": str(session_root),
    "session_root_exists": session_root.exists(),
    "lookback_hours": lookback_hours,
    "scan_limit": scan_limit,
    "scanned_file_count": 0,
    "matched_session_count": 0,
    "workspaces": workspace_specs,
}

if session_root.exists():
    files = sorted(
        session_root.rglob("rollout-*.jsonl"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    probe["scanned_file_count"] = min(len(files), scan_limit)

    for path in files[:scan_limit]:
        try:
            stat = path.stat()
        except OSError:
            continue
        if now_ts - stat.st_mtime > lookback_seconds:
            continue

        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                first_line = handle.readline().strip()
        except OSError:
            continue

        if not first_line:
            continue
        try:
            meta = json.loads(first_line)
        except json.JSONDecodeError:
            continue

        cwd = str((meta.get("payload") or {}).get("cwd", "")).strip()
        if not cwd:
            continue

        workspace = _match_workspace(cwd, workspace_specs)
        if workspace is None:
            continue

        workspace["matched_count"] += 1
        probe["matched_session_count"] += 1
        if len(workspace["sessions"]) >= limit_per_workspace:
            continue
        workspace["sessions"].append(_summarize_session(path, now_ts=now_ts))

print(json.dumps(probe, ensure_ascii=False))
"""


def collect_recent_wsl_sessions(
    *,
    workspace_dirs: list[str] | tuple[str, ...] | None = None,
    lookback_hours: int = 12,
    limit_per_workspace: int = 3,
    scan_limit: int = 200,
) -> dict[str, Any]:
    raw_dirs = workspace_dirs or DEFAULT_WORKSPACE_DIRS
    workspace_specs = [
        {"label": str(item).strip(), "cwd_prefix": str(item).strip()}
        for item in raw_dirs
        if str(item).strip()
    ]
    request = {
        "lookback_hours": lookback_hours,
        "limit_per_workspace": limit_per_workspace,
        "scan_limit": scan_limit,
        "workspaces": workspace_specs,
    }
    try:
        result = subprocess.run(
            ["wsl", "python3", "-c", _WSL_SESSION_PROBE, json.dumps(request, ensure_ascii=False)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=20,
        )
    except Exception as error:  # noqa: BLE001
        raise RuntimeError(f"recent-sessions failed: {error}") from error

    if result.returncode != 0:
        details = "\n".join(
            part.strip()
            for part in (result.stdout, result.stderr)
            if part and part.strip()
        ).strip()
        raise RuntimeError(details or f"wsl probe exited with code {result.returncode}")

    output = result.stdout.strip()
    if not output:
        raise RuntimeError("recent-sessions returned empty output")

    json_start = output.find("{")
    json_end = output.rfind("}")
    if json_start != -1 and json_end != -1 and json_end >= json_start:
        output = output[json_start : json_end + 1]

    try:
        return json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"recent-sessions returned invalid JSON: {output[:400]}") from error


def render_recent_wsl_sessions(
    probe: dict[str, Any],
    *,
    json_mode: bool = False,
) -> str:
    if json_mode:
        return json.dumps(probe, ensure_ascii=False, indent=2)

    lines = [
        "[recent sessions]",
        f"generated_at={probe.get('generated_at', '')}",
        f"session_root={probe.get('session_root', '')}",
        f"session_root_exists={probe.get('session_root_exists', False)}",
        f"lookback_hours={probe.get('lookback_hours', 12)}",
        f"scanned_file_count={probe.get('scanned_file_count', 0)}",
        f"matched_session_count={probe.get('matched_session_count', 0)}",
    ]

    for workspace in probe.get("workspaces") or []:
        label = workspace.get("label", "")
        cwd_prefix = workspace.get("cwd_prefix", "")
        matched_count = workspace.get("matched_count", 0)
        sessions = workspace.get("sessions") or []
        lines.append("")
        lines.append(f"[workspace] {label} cwd_prefix={cwd_prefix} matched={matched_count} showing={len(sessions)}")
        if not sessions:
            lines.append("- none")
            continue
        for item in sessions:
            lines.append(
                "- "
                f"{item.get('status_hint', 'unknown')} | age={item.get('age_minutes', '?')}m | "
                f"last_activity={item.get('last_activity', '')} | cwd={item.get('cwd', '')}"
            )
            lines.append(f"  last_role={item.get('last_role', '') or '-'}")
            lines.append(f"  last_excerpt={item.get('last_excerpt', '')}")

    return "\n".join(lines)

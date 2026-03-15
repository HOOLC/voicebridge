from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import BridgeConfig


class AssistantRuntimeStore:
    def __init__(self, config: BridgeConfig):
        self._lock = threading.Lock()
        self.state_path = Path(config.assistant_state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        normalized = self._read_state()
        self._write_state(normalized)

    def sync_runtime(self, config: BridgeConfig, *, shared_thread_id: str = "") -> None:
        self._update_meta(
            runtime_summary={
                "tts_speaker": config.volcengine_tts_speaker,
                "tts_speed_ratio": config.volcengine_tts_speed_ratio,
                "interrupt_playback": config.bridge_interrupt_playback,
                "cancel_codex_on_interrupt": config.bridge_cancel_codex_on_interrupt,
                "feishu_enabled": config.feishu_enabled,
                "scheduled_task_count": len(config.scheduled_tasks),
            },
            shared_thread_id=shared_thread_id,
        )

    def set_queue_depth(self, depth: int) -> None:
        self._update_runtime(queue_depth=max(0, depth))

    def set_codex_busy(self, busy: bool) -> None:
        self._update_runtime(codex_busy=busy)

    def record_user_turn(
        self,
        *,
        turn_id: int,
        transcript: str,
        meaningful: bool,
        action: str,
        source: str,
    ) -> None:
        self._update_runtime(
            last_user_turn_id=turn_id,
            last_user_text=transcript,
            last_user_meaningful=meaningful,
            last_user_action=action,
            last_user_source=source,
        )

    def record_reply(
        self,
        *,
        turn_id: int,
        text: str,
        source: str,
        spoken: bool,
        thread_id: str = "",
    ) -> None:
        updates = {
            "last_reply_text": text,
            "last_reply_turn_id": turn_id,
            "last_reply_source": source,
            "last_reply_spoken": spoken,
        }
        if spoken:
            updates["last_spoken_reply_text"] = text
        self._update_runtime(**updates)
        self._update_meta(shared_thread_id=thread_id or None)

    def mark_silence(self, *, turn_id: int, source: str) -> None:
        self._update_runtime(last_silence_turn_id=turn_id, last_silence_source=source)

    def record_error(self, message: str) -> None:
        self._update_runtime(last_error=str(message).strip())

    def clear_error(self) -> None:
        self._update_runtime(last_error="")

    def get_last_spoken_reply_text(self) -> str:
        with self._lock:
            state = self._read_state()
        return str(state.get("runtime", {}).get("last_spoken_reply_text", "")).strip()

    def _update_runtime(self, **updates: Any) -> None:
        with self._lock:
            state = self._read_state()
            runtime = dict(state.get("runtime") or {})
            for key, value in updates.items():
                if value is None:
                    continue
                runtime[key] = value
            state["runtime"] = runtime
            state["updated_at"] = _now()
            self._write_state(state)

    def _update_meta(self, **updates: Any) -> None:
        with self._lock:
            state = self._read_state()
            meta = dict(state.get("meta") or {})
            for key, value in updates.items():
                if value is None:
                    continue
                meta[key] = value
            state["meta"] = meta
            state["updated_at"] = _now()
            self._write_state(state)

    def _read_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            payload = {}
        return self._normalize_state(payload if isinstance(payload, dict) else {})

    def _write_state(self, payload: dict[str, Any]) -> None:
        self.state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _default_state() -> dict[str, Any]:
        return {
            "updated_at": _now(),
            "meta": {
                "shared_thread_id": "",
                "runtime_summary": {},
            },
            "runtime": {
                "codex_busy": False,
                "queue_depth": 0,
                "last_user_text": "",
                "last_user_turn_id": 0,
                "last_user_action": "",
                "last_user_meaningful": False,
                "last_user_source": "",
                "last_reply_text": "",
                "last_reply_turn_id": 0,
                "last_reply_source": "",
                "last_reply_spoken": False,
                "last_spoken_reply_text": "",
                "last_silence_turn_id": 0,
                "last_silence_source": "",
                "last_error": "",
            },
        }

    @classmethod
    def _normalize_state(cls, payload: dict[str, Any]) -> dict[str, Any]:
        default = cls._default_state()
        normalized = {
            "updated_at": str(payload.get("updated_at") or default["updated_at"]),
            "meta": dict(default["meta"]),
            "runtime": dict(default["runtime"]),
        }

        meta = payload.get("meta")
        if isinstance(meta, dict):
            runtime_summary = meta.get("runtime_summary")
            if isinstance(runtime_summary, dict):
                normalized["meta"]["runtime_summary"] = runtime_summary
            shared_thread_id = meta.get("shared_thread_id")
            if shared_thread_id not in (None, ""):
                normalized["meta"]["shared_thread_id"] = str(shared_thread_id)

        runtime = payload.get("runtime")
        if isinstance(runtime, dict):
            for key in normalized["runtime"]:
                value = runtime.get(key)
                if value is not None:
                    normalized["runtime"][key] = value

        return normalized


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")

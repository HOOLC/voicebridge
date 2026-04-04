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

    def sync_runtime(self, config: BridgeConfig, *, last_reply_message_id: str = "") -> None:
        self._update_meta(
            runtime_summary={
                "tts_model": config.tts_model,
                "tts_voice_id": config.tts_voice_id,
                "interrupt_playback": config.bridge_interrupt_playback,
                "phone_bridge_command": config.phone_bridge_command,
                "reply_source": config.phone_reply_source,
            },
            last_reply_message_id=last_reply_message_id,
        )

    def set_queue_depth(self, depth: int) -> None:
        self._update_runtime(queue_depth=max(0, depth))

    def set_busy(self, busy: bool) -> None:
        self._update_runtime(busy=busy)

    def record_user_turn(
        self,
        *,
        turn_id: int,
        transcript: str,
        meaningful: bool,
        action: str,
    ) -> None:
        self._update_runtime(
            last_user_turn_id=turn_id,
            last_user_text=transcript,
            last_user_meaningful=meaningful,
            last_user_action=action,
        )

    def record_reply(
        self,
        *,
        turn_id: int,
        text: str,
        spoken: bool,
        message_id: str,
    ) -> None:
        updates = {
            "last_reply_text": text,
            "last_reply_turn_id": turn_id,
            "last_reply_spoken": spoken,
            "last_reply_message_id": message_id,
        }
        if spoken:
            updates["last_spoken_reply_text"] = text
        self._update_runtime(**updates)
        self._update_meta(last_reply_message_id=message_id)

    def record_error(self, message: str) -> None:
        self._update_runtime(last_error=str(message).strip())

    def clear_error(self) -> None:
        self._update_runtime(last_error="")

    def get_last_spoken_reply_text(self) -> str:
        with self._lock:
            state = self._read_state()
        return str(state.get("runtime", {}).get("last_spoken_reply_text", "")).strip()

    def get_last_reply_message_id(self) -> str:
        with self._lock:
            state = self._read_state()
        runtime = state.get("runtime") or {}
        meta = state.get("meta") or {}
        return str(runtime.get("last_reply_message_id") or meta.get("last_reply_message_id") or "").strip()

    def remember_last_reply_message_id(self, message_id: str) -> None:
        clean_message_id = str(message_id).strip()
        if not clean_message_id:
            return
        self._update_runtime(last_reply_message_id=clean_message_id)
        self._update_meta(last_reply_message_id=clean_message_id)

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
        except Exception:
            payload = {}
        return self._normalize_state(payload if isinstance(payload, dict) else {})

    def _write_state(self, payload: dict[str, Any]) -> None:
        self.state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _default_state() -> dict[str, Any]:
        return {
            "updated_at": _now(),
            "meta": {
                "last_reply_message_id": "",
                "runtime_summary": {},
            },
            "runtime": {
                "busy": False,
                "queue_depth": 0,
                "last_user_text": "",
                "last_user_turn_id": 0,
                "last_user_action": "",
                "last_user_meaningful": False,
                "last_reply_text": "",
                "last_reply_turn_id": 0,
                "last_reply_spoken": False,
                "last_reply_message_id": "",
                "last_spoken_reply_text": "",
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
            last_reply_message_id = meta.get("last_reply_message_id")
            if last_reply_message_id not in (None, ""):
                normalized["meta"]["last_reply_message_id"] = str(last_reply_message_id)

        runtime = payload.get("runtime")
        if isinstance(runtime, dict):
            for key in normalized["runtime"]:
                value = runtime.get(key)
                if value is not None:
                    normalized["runtime"][key] = value

        return normalized


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")

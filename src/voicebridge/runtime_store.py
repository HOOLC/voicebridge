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
        if not self.state_path.exists():
            self._write_state(self._default_state(config))

    def sync_config(self, config: BridgeConfig) -> None:
        with self._lock:
            state = self._read_state()
            state["mode"] = {
                "interrupt_playback": config.bridge_interrupt_playback,
                "cancel_codex_on_interrupt": config.bridge_cancel_codex_on_interrupt,
                "reply_chunk_chars": config.bridge_chunk_chars,
                "tts_speaker": config.volcengine_tts_speaker,
                "tts_speed_ratio": config.volcengine_tts_speed_ratio,
                "codex_model": config.codex_model,
                "codex_timeout_seconds": config.codex_timeout_seconds,
            }
            state["updated_at"] = _now()
            self._write_state(state)

    def set_queue_depth(self, depth: int) -> None:
        self._update_session(queue_depth=max(0, depth))

    def set_codex_busy(self, busy: bool) -> None:
        self._update_session(codex_busy=busy)

    def record_user_turn(self, *, turn_id: int, transcript: str, meaningful: bool, action: str) -> None:
        self._update_session(
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
        source: str,
        spoken: bool,
        thread_id: str = "",
    ) -> None:
        self._update_session(
            last_reply_text=text,
            last_reply_turn_id=turn_id,
            last_reply_source=source,
            last_reply_spoken=spoken,
            thread_id=thread_id or None,
        )

    def mark_silence(self, *, turn_id: int, source: str) -> None:
        self._update_session(last_silence_turn_id=turn_id, last_silence_source=source)

    def get_last_reply_text(self) -> str:
        with self._lock:
            state = self._read_state()
        return str(state.get("session", {}).get("last_reply_text", "")).strip()

    def _update_session(self, **updates: Any) -> None:
        with self._lock:
            state = self._read_state()
            session = dict(state.get("session") or {})
            for key, value in updates.items():
                if value is None:
                    continue
                session[key] = value
            state["session"] = session
            state["updated_at"] = _now()
            self._write_state(state)

    def _read_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            payload = {}
        if isinstance(payload, dict):
            return payload
        return {}

    def _write_state(self, payload: dict[str, Any]) -> None:
        self.state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _default_state(config: BridgeConfig) -> dict[str, Any]:
        return {
            "updated_at": _now(),
            "mode": {
                "interrupt_playback": config.bridge_interrupt_playback,
                "cancel_codex_on_interrupt": config.bridge_cancel_codex_on_interrupt,
                "reply_chunk_chars": config.bridge_chunk_chars,
                "tts_speaker": config.volcengine_tts_speaker,
                "tts_speed_ratio": config.volcengine_tts_speed_ratio,
                "codex_model": config.codex_model,
                "codex_timeout_seconds": config.codex_timeout_seconds,
            },
            "session": {
                "thread_id": "",
                "last_user_text": "",
                "last_user_turn_id": 0,
                "last_user_action": "",
                "last_user_meaningful": False,
                "last_reply_text": "",
                "last_reply_turn_id": 0,
                "last_reply_source": "",
                "last_reply_spoken": False,
                "last_silence_turn_id": 0,
                "last_silence_source": "",
                "codex_busy": False,
                "queue_depth": 0,
            },
        }


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")

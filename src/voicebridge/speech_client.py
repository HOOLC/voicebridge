from __future__ import annotations

import base64
import socket
import uuid
from typing import Any

import requests

from .config import BridgeConfig


class SpeechClient:
    def __init__(self, config: BridgeConfig):
        self.config = config
        self._session = requests.Session()
        self._user_id = socket.gethostname() or "voicebridge"

    def close(self) -> None:
        self._session.close()

    def prepare_models(self) -> dict[str, str]:
        self._require_asr_credentials()
        self._require_tts_credentials()
        return {
            "volcengine_asr_resource_id": self.config.volcengine_asr_resource_id,
            "tts_model": self.config.tts_model,
            "tts_voice_id": self.config.tts_voice_id,
        }

    def transcribe(self, wav_bytes: bytes) -> str:
        self._require_asr_credentials()
        response = self._session.post(
            "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash",
            headers={
                "Content-Type": "application/json",
                "X-Api-App-Id": self.config.volcengine_app_id or "",
                "X-Api-App-Key": self.config.volcengine_app_id or "",
                "X-Api-Access-Key": self.config.volcengine_access_key or "",
                "X-Api-Resource-Id": self.config.volcengine_asr_resource_id,
                "X-Api-Request-Id": str(uuid.uuid4()),
            },
            json={
                "user": {"uid": self._user_id},
                "audio": {
                    "format": "wav",
                    "data": base64.b64encode(wav_bytes).decode("utf-8"),
                },
                "request": {
                    "model_name": "bigmodel",
                    "enable_itn": True,
                    "show_utterances": True,
                },
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        if int(payload.get("code", 0) or 0) != 0:
            raise RuntimeError(f"Volcengine ASR failed: {payload}")
        return _extract_asr_text(payload)

    def synthesize(self, text: str) -> bytes:
        self._require_tts_credentials()
        response = self._session.post(
            f"{self.config.minimax_api_base}/v1/t2a_v2",
            headers={
                "Authorization": f"Bearer {self.config.minimax_api_key or ''}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.config.tts_model,
                "text": text,
                "stream": False,
                "language_boost": self.config.tts_language_boost,
                "output_format": "hex",
                "voice_setting": {
                    "voice_id": self.config.tts_voice_id,
                    "speed": self.config.tts_speed,
                    "vol": self.config.tts_volume,
                    "pitch": self.config.tts_pitch,
                },
                "audio_setting": {
                    "sample_rate": self.config.tts_sample_rate,
                    "bitrate": 128000,
                    "format": self.config.tts_format,
                    "channel": 1,
                },
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        base_resp = payload.get("base_resp") if isinstance(payload, dict) else {}
        status_code_raw = (base_resp or {}).get("status_code", -1)
        status_code = int(status_code_raw if status_code_raw is not None else -1)
        if status_code != 0:
            raise RuntimeError(f"MiniMax TTS failed: {payload}")
        data = payload.get("data") if isinstance(payload, dict) else {}
        audio_hex = str((data or {}).get("audio") or "").strip()
        if not audio_hex:
            raise RuntimeError("MiniMax TTS returned no audio data")
        try:
            return bytes.fromhex(audio_hex)
        except ValueError as error:
            raise RuntimeError("MiniMax TTS returned invalid audio payload") from error

    def _require_asr_credentials(self) -> None:
        missing = []
        if not self.config.volcengine_app_id:
            missing.append("bridge.yaml: volcengine_app_id")
        if not self.config.volcengine_access_key:
            missing.append("bridge.yaml: volcengine_access_key")
        if missing:
            raise RuntimeError(f"ASR credentials are incomplete. Missing: {', '.join(missing)}")

    def _require_tts_credentials(self) -> None:
        if not self.config.minimax_api_key:
            raise RuntimeError("MiniMax TTS credentials are incomplete. Missing: MINIMAX_API_KEY")


def _extract_asr_text(payload: dict[str, Any]) -> str:
    candidates = []

    result = payload.get("result")
    if isinstance(result, dict):
        text = result.get("text")
        if isinstance(text, str) and text.strip():
            candidates.append(text.strip())
        utterances = result.get("utterances")
        if isinstance(utterances, list):
            joined = "".join(
                str(item.get("text", "")).strip()
                for item in utterances
                if isinstance(item, dict) and str(item.get("text", "")).strip()
            ).strip()
            if joined:
                candidates.append(joined)

    for key in ("text", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())

    for item in candidates:
        if item:
            return item
    return ""

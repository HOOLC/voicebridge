from __future__ import annotations

import base64
import json
import socket
import uuid
from typing import Any

import requests

from .config import BridgeConfig


class VolcengineAudioClient:
    def __init__(self, config: BridgeConfig):
        self.config = config
        self._session = requests.Session()
        self._user_id = socket.gethostname() or "ai-kook"

    def close(self) -> None:
        self._session.close()

    def prepare_models(self) -> dict[str, str]:
        self._require_credentials()
        return {
            "speech_provider": self.config.speech_provider,
            "volcengine_app_id": self.config.volcengine_app_id or "",
            "volcengine_asr_resource_id": self.config.volcengine_asr_resource_id,
            "volcengine_tts_speaker": self.config.volcengine_tts_speaker,
        }

    def transcribe(self, wav_bytes: bytes) -> str:
        self._require_credentials()
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
        self._require_credentials()
        request_id = str(uuid.uuid4())
        response = self._session.post(
            "https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse",
            headers={
                "Content-Type": "application/json",
                "X-Api-App-Id": self.config.volcengine_app_id or "",
                "X-Api-Access-Key": self.config.volcengine_access_key or "",
                "X-Api-Resource-Id": self.config.volcengine_tts_resource_id,
                "X-Api-Request-Id": request_id,
            },
            json={
                "user": {"uid": self._user_id},
                "namespace": "BidirectionalTTS",
                "req_params": {
                    "text": text,
                    "model": "seed-tts-2.0-expressive",
                    "speaker": self.config.volcengine_tts_speaker,
                    "audio_params": {
                        "format": self.config.volcengine_tts_format,
                        "sample_rate": self.config.volcengine_tts_sample_rate,
                        "speech_rate": _ratio_to_signed_percent(self.config.volcengine_tts_speed_ratio),
                        "loudness_rate": _ratio_to_signed_percent(self.config.volcengine_tts_volume_ratio),
                        "pitch_rate": _ratio_to_signed_percent(self.config.volcengine_tts_pitch_ratio),
                        "enable_subtitle": False,
                    },
                },
            },
            stream=True,
            timeout=60,
        )
        response.raise_for_status()
        return _read_unidirectional_tts_audio(response)

    def _require_credentials(self) -> None:
        missing = []
        if not self.config.volcengine_app_id:
            missing.append("VOLCENGINE_APP_ID/DOUBAO_APP_ID")
        if not self.config.volcengine_access_key:
            missing.append("VOLCENGINE_ACCESS_KEY/DOUBAO_ACCESS_KEY/DOUBAO_ACCESS_TOKEN/DOUBAO_API_KEY")
        if missing:
            joined = ", ".join(missing)
            raise RuntimeError(f"Volcengine speech credentials are incomplete. Missing: {joined}")


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


def _read_unidirectional_tts_audio(response: requests.Response) -> bytes:
    audio_chunks: list[bytes] = []
    error_payloads: list[str] = []

    for raw_line in response.iter_lines(decode_unicode=True):
        if not raw_line:
            continue
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        payload_text = line[5:].strip()
        if not payload_text or payload_text == "[DONE]":
            continue
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            continue

        if int(payload.get("code", 0) or 0) not in (0, 20000000):
            error_payloads.append(payload_text)
            continue

        data = payload.get("data")
        if isinstance(data, str) and data:
            audio_chunks.append(base64.b64decode(data))

    if error_payloads:
        raise RuntimeError(f"Volcengine TTS failed: {error_payloads[-1]}")
    if not audio_chunks:
        raise RuntimeError("Volcengine TTS returned no audio data")
    return b"".join(audio_chunks)


def _ratio_to_signed_percent(value: float) -> int:
    percent = round((value - 1.0) * 100)
    return max(-50, min(100, percent))

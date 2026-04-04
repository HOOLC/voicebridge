from __future__ import annotations

from typing import Any

import yaml


DEFAULT_ASSISTANT_RUNTIME_YAML = """version: 1

voice:
  tts_model: "speech-2.8-turbo"
  tts_voice_id: "Chinese (Mandarin)_Warm_Bestie"
  format: "wav"
  sample_rate: 32000
  speed: 1.0
  volume: 1.0
  pitch: 0
  language_boost: "Chinese"

ack:
  default: "收到"
  variants:
    - "收到"
    - "好，收到"
    - "嗯，收到"

interaction:
  interrupt_playback: true
  vad_rms_threshold: 900

commands:
  transcript_min_chars: 2
  ignore_phrases:
    - "嗯"
    - "啊"
    - "呃"
    - "额"
    - "哦"
    - "喂"
  repeat_aliases:
    - "再说一遍"
    - "再说一次"
    - "重复一下"
    - "把刚才说的再说一遍"
    - "刚才说什么"
  stop_aliases:
    - "停一下"
    - "别说了"
    - "先别说"
    - "闭嘴"

phone:
  from_name: "voicebridge"
  reply_source: "final_answer"
  reply_timeout_seconds: 180
  recv_poll_interval_seconds: 1.0
"""


def load_default_runtime_data() -> dict[str, Any]:
    payload = yaml.safe_load(DEFAULT_ASSISTANT_RUNTIME_YAML) or {}
    return payload if isinstance(payload, dict) else {}

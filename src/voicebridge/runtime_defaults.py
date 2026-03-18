from __future__ import annotations

from typing import Any

import yaml


DEFAULT_ASSISTANT_RUNTIME_YAML = """version: 1

voice:
  tts_resource_id: "seed-tts-2.0"
  tts_speaker: "zh_female_vv_uranus_bigtts"
  format: "wav"
  sample_rate: 24000
  speed_ratio: 1.05
  volume_ratio: 1.0
  pitch_ratio: 1.0

ack:
  default: "收到啦"
  variants:
    - "收到啦"
    - "好呀，收到"
    - "嗯嗯，收到"
    - "在呢，收到啦"

interaction:
  interrupt_playback: true
  cancel_codex_on_interrupt: false
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

schedule:
  check_interval_seconds: 15
  tasks: []
"""


DEFAULT_TTS_VOICES_YAML = """version: 1
generated_at: "2026-03-18 00:00:00"
voices:
  - name: "Vivi 2.0"
    id: "zh_female_vv_uranus_bigtts"
    aliases: ["vivi"]
    description: "默认中文女声"
"""


DEFAULT_MEMORY_MD = """# 长期记忆

- 这里记录长期稳定偏好、常驻工作方式和需要跨天保留的上下文。
- 只保留高价值事实，不要把瞬时运行状态写进这里。
"""


DEFAULT_AGENTS_MD = """# VoiceBridge 助手完整手册

**目录**：`bridge-home`
**身份**：VoiceBridge 本地助手
**主要交互**：飞书私聊、电话、定时任务触发

## 基本原则

- 中文、简洁、自然
- 不输出思考过程、工具过程、命令输出
- 电话输出适合 TTS，避免路径和文件名
- 定时任务优先使用 `assistant-runtime.yaml`

## 配置速查

- `assistant-runtime.yaml`：音色、确认词、交互方式、命令别名、定时任务
- `tts-voices.yaml`：可用音色与别名
- `MEMORY.md`：长期记忆
- `memory/YYYY-MM-DD.md`：每日记忆
"""


def load_default_runtime_data() -> dict[str, Any]:
    payload = yaml.safe_load(DEFAULT_ASSISTANT_RUNTIME_YAML) or {}
    return payload if isinstance(payload, dict) else {}


def load_default_voice_catalog() -> list[dict[str, Any]]:
    payload = yaml.safe_load(DEFAULT_TTS_VOICES_YAML) or {}
    if not isinstance(payload, dict):
        return []
    voices = payload.get("voices")
    if not isinstance(voices, list):
        return []

    normalized: list[dict[str, Any]] = []
    for item in voices:
        if not isinstance(item, dict):
            continue
        voice_id = str(item.get("id", "")).strip()
        if not voice_id:
            continue
        aliases = item.get("aliases")
        alias_list = []
        if isinstance(aliases, list):
            alias_list = [str(alias).strip() for alias in aliases if str(alias).strip()]
        normalized.append(
            {
                "id": voice_id,
                "name": str(item.get("name") or voice_id).strip(),
                "aliases": alias_list,
                "description": str(item.get("description") or "").strip(),
            }
        )
    return normalized

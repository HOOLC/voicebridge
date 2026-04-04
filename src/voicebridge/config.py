from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

from .runtime_defaults import load_default_runtime_data


DEFAULT_CONFIG_PATH = Path("bridge.yaml")


class VoiceRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tts_model: str = "speech-2.8-hd"
    tts_voice_id: str = "Chinese (Mandarin)_Warm_Bestie"
    format: Literal["wav", "mp3", "flac"] = "wav"
    sample_rate: int = 32_000
    speed: float = 1.0
    volume: float = 1.0
    pitch: int = 0
    language_boost: str = "Chinese"


class AckRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: str = "收到"
    variants: list[str] = Field(
        default_factory=lambda: [
            "收到",
            "好，收到",
            "嗯，收到",
        ]
    )


class InteractionRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interrupt_playback: bool = True
    vad_rms_threshold: int = 900


class CommandsRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transcript_min_chars: int = 2
    ignore_phrases: list[str] = Field(
        default_factory=lambda: [
            "嗯",
            "啊",
            "呃",
            "额",
            "哦",
            "喂",
        ]
    )
    repeat_aliases: list[str] = Field(
        default_factory=lambda: [
            "再说一遍",
            "再说一次",
            "重复一下",
            "把刚才说的再说一遍",
            "刚才说什么",
        ]
    )
    stop_aliases: list[str] = Field(
        default_factory=lambda: [
            "停一下",
            "别说了",
            "先别说",
            "闭嘴",
        ]
    )


class PhoneRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_name: str = "voicebridge"
    reply_source: Literal["commentary", "final_answer", "all"] = "final_answer"
    reply_timeout_seconds: int = 180
    recv_poll_interval_seconds: float = 1.0


class AssistantRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    voice: VoiceRuntimeConfig = Field(default_factory=VoiceRuntimeConfig)
    ack: AckRuntimeConfig = Field(default_factory=AckRuntimeConfig)
    interaction: InteractionRuntimeConfig = Field(default_factory=InteractionRuntimeConfig)
    commands: CommandsRuntimeConfig = Field(default_factory=CommandsRuntimeConfig)
    phone: PhoneRuntimeConfig = Field(default_factory=PhoneRuntimeConfig)


class BridgeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_root: str = "."
    config_path: str = "bridge.yaml"

    assistant_runtime_config_path: str = "./bridge-home/assistant-runtime.yaml"
    assistant_state_path: str = "./bridge-home/assistant-state.json"

    volcengine_app_id: str | None = None
    volcengine_access_key: str | None = None
    volcengine_asr_resource_id: str = "volc.bigasr.auc_turbo"

    minimax_api_key: str | None = None
    minimax_api_base: str = "https://api.minimaxi.com"

    phone_bridge_command: str = "codex-feishu-agent"

    capture_device: str | int
    playback_device: str | int

    sample_rate: int = 16_000
    channels: int = 1
    frame_ms: int = 20
    vad_mode: int = Field(default=2, ge=0, le=3)
    speech_start_min_voiced_ms: int = 120
    min_speech_ms: int = 400
    silence_ms: int = 900
    preroll_ms: int = 300
    max_utterance_ms: int = 15_000

    print_transcript: bool = True
    runtime: AssistantRuntimeConfig = Field(default_factory=AssistantRuntimeConfig)

    @property
    def frame_bytes(self) -> int:
        samples = self.sample_rate * self.frame_ms // 1000
        return samples * self.channels * 2

    @property
    def tts_model(self) -> str:
        return self.runtime.voice.tts_model

    @property
    def tts_voice_id(self) -> str:
        return self.runtime.voice.tts_voice_id

    @property
    def tts_format(self) -> str:
        return self.runtime.voice.format

    @property
    def tts_sample_rate(self) -> int:
        return self.runtime.voice.sample_rate

    @property
    def tts_speed(self) -> float:
        return self.runtime.voice.speed

    @property
    def tts_volume(self) -> float:
        return self.runtime.voice.volume

    @property
    def tts_pitch(self) -> int:
        return self.runtime.voice.pitch

    @property
    def tts_language_boost(self) -> str:
        return self.runtime.voice.language_boost

    @property
    def bridge_ack_text(self) -> str:
        return self.runtime.ack.default

    @property
    def bridge_ack_variants(self) -> list[str]:
        return self.runtime.ack.variants

    @property
    def bridge_interrupt_playback(self) -> bool:
        return self.runtime.interaction.interrupt_playback

    @property
    def vad_rms_threshold(self) -> int:
        return self.runtime.interaction.vad_rms_threshold

    @property
    def bridge_repeat_aliases(self) -> list[str]:
        return self.runtime.commands.repeat_aliases

    @property
    def bridge_stop_aliases(self) -> list[str]:
        return self.runtime.commands.stop_aliases

    @property
    def transcript_min_chars(self) -> int:
        return self.runtime.commands.transcript_min_chars

    @property
    def transcript_ignore_phrases(self) -> list[str]:
        return self.runtime.commands.ignore_phrases

    @property
    def phone_from_name(self) -> str:
        return self.runtime.phone.from_name

    @property
    def phone_reply_source(self) -> str:
        return self.runtime.phone.reply_source

    @property
    def phone_reply_timeout_seconds(self) -> int:
        return self.runtime.phone.reply_timeout_seconds

    @property
    def phone_recv_poll_interval_seconds(self) -> float:
        return self.runtime.phone.recv_poll_interval_seconds


def load_config(path: str | Path | None = None) -> BridgeConfig:
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    load_dotenv(config_path.parent / ".env", override=False)
    raw_text = config_path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw_text) or {}
    if not isinstance(data, dict):
        raise ValueError("Config file must contain a YAML object")

    _apply_env_defaults(data)

    runtime_path = _resolve_local_path(
        config_path,
        str(data.get("assistant_runtime_config_path") or BridgeConfig.model_fields["assistant_runtime_config_path"].default),
    )
    runtime_data = _merge_yaml_object(load_default_runtime_data(), _load_yaml_object(runtime_path))
    runtime_payload = _build_runtime_payload(runtime_data)

    config = BridgeConfig.model_validate({**data, "runtime": runtime_payload})
    return config.model_copy(
        update={
            "project_root": str(config_path.parent.resolve()),
            "config_path": str(config_path.resolve()),
            "assistant_runtime_config_path": str(runtime_path),
            "assistant_state_path": str(_resolve_local_path(config_path, config.assistant_state_path)),
            "volcengine_app_id": _expand_env_vars(config.volcengine_app_id) if config.volcengine_app_id else None,
            "volcengine_access_key": _expand_env_vars(config.volcengine_access_key) if config.volcengine_access_key else None,
            "minimax_api_key": _expand_env_vars(config.minimax_api_key) if config.minimax_api_key else None,
            "minimax_api_base": _expand_env_vars(config.minimax_api_base).rstrip("/"),
            "phone_bridge_command": _expand_env_vars(config.phone_bridge_command),
        }
    )


def dump_device(device: dict[str, Any], index: int) -> str:
    name = str(device.get("name", "")).strip()
    hostapi = device.get("hostapi")
    inputs = device.get("max_input_channels")
    outputs = device.get("max_output_channels")
    rate = device.get("default_samplerate")
    return f"[{index}] {name} | hostapi={hostapi} | in={inputs} | out={outputs} | rate={rate}"


def _resolve_local_path(config_path: Path, raw_path: str) -> Path:
    path = Path(_expand_env_vars(raw_path))
    if path.is_absolute():
        return path
    return (config_path.parent / path).resolve()


def _expand_env_vars(value: str) -> str:
    return os.path.expandvars(value)


def _apply_env_defaults(data: dict[str, Any]) -> None:
    env_defaults = {
        "minimax_api_key": os.getenv("MINIMAX_API_KEY") or os.getenv("MINIMAX_TOKEN_PLAN_API_KEY"),
        "minimax_api_base": os.getenv("MINIMAX_API_BASE"),
    }
    for key, value in env_defaults.items():
        if (key not in data or data.get(key) in (None, "")) and value:
            data[key] = value


def _build_runtime_payload(runtime_data: dict[str, Any]) -> dict[str, Any]:
    voice_data = runtime_data.get("voice") if isinstance(runtime_data.get("voice"), dict) else {}
    ack_data = runtime_data.get("ack") if isinstance(runtime_data.get("ack"), dict) else {}
    interaction_data = runtime_data.get("interaction") if isinstance(runtime_data.get("interaction"), dict) else {}
    commands_data = runtime_data.get("commands") if isinstance(runtime_data.get("commands"), dict) else {}
    phone_data = runtime_data.get("phone") if isinstance(runtime_data.get("phone"), dict) else {}

    runtime_payload = {
        "version": int(runtime_data.get("version") or 1),
        "voice": {
            "tts_model": _first_present(
                voice_data.get("tts_model"),
                VoiceRuntimeConfig.model_fields["tts_model"].default,
            ),
            "tts_voice_id": _first_present(
                voice_data.get("tts_voice_id"),
                VoiceRuntimeConfig.model_fields["tts_voice_id"].default,
            ),
            "format": _first_present(
                voice_data.get("format"),
                VoiceRuntimeConfig.model_fields["format"].default,
            ),
            "sample_rate": _first_present(
                voice_data.get("sample_rate"),
                VoiceRuntimeConfig.model_fields["sample_rate"].default,
            ),
            "speed": _first_present(
                voice_data.get("speed"),
                VoiceRuntimeConfig.model_fields["speed"].default,
            ),
            "volume": _first_present(
                voice_data.get("volume"),
                VoiceRuntimeConfig.model_fields["volume"].default,
            ),
            "pitch": _first_present(
                voice_data.get("pitch"),
                VoiceRuntimeConfig.model_fields["pitch"].default,
            ),
            "language_boost": _first_present(
                voice_data.get("language_boost"),
                VoiceRuntimeConfig.model_fields["language_boost"].default,
            ),
        },
        "ack": {
            "default": _first_present(
                ack_data.get("default"),
                AckRuntimeConfig.model_fields["default"].default,
            ),
            "variants": _normalize_string_list(
                _first_present(
                    ack_data.get("variants"),
                    AckRuntimeConfig.model_fields["variants"].default_factory(),
                )
            ),
        },
        "interaction": {
            "interrupt_playback": _first_present(
                interaction_data.get("interrupt_playback"),
                InteractionRuntimeConfig.model_fields["interrupt_playback"].default,
            ),
            "vad_rms_threshold": _first_present(
                interaction_data.get("vad_rms_threshold"),
                InteractionRuntimeConfig.model_fields["vad_rms_threshold"].default,
            ),
        },
        "commands": {
            "transcript_min_chars": _first_present(
                commands_data.get("transcript_min_chars"),
                CommandsRuntimeConfig.model_fields["transcript_min_chars"].default,
            ),
            "ignore_phrases": _normalize_string_list(
                _first_present(
                    commands_data.get("ignore_phrases"),
                    CommandsRuntimeConfig.model_fields["ignore_phrases"].default_factory(),
                )
            ),
            "repeat_aliases": _normalize_string_list(
                _first_present(
                    commands_data.get("repeat_aliases"),
                    CommandsRuntimeConfig.model_fields["repeat_aliases"].default_factory(),
                )
            ),
            "stop_aliases": _normalize_string_list(
                _first_present(
                    commands_data.get("stop_aliases"),
                    CommandsRuntimeConfig.model_fields["stop_aliases"].default_factory(),
                )
            ),
        },
        "phone": {
            "from_name": _first_present(
                phone_data.get("from_name"),
                PhoneRuntimeConfig.model_fields["from_name"].default,
            ),
            "reply_source": _first_present(
                phone_data.get("reply_source"),
                PhoneRuntimeConfig.model_fields["reply_source"].default,
            ),
            "reply_timeout_seconds": _first_present(
                phone_data.get("reply_timeout_seconds"),
                PhoneRuntimeConfig.model_fields["reply_timeout_seconds"].default,
            ),
            "recv_poll_interval_seconds": _first_present(
                phone_data.get("recv_poll_interval_seconds"),
                PhoneRuntimeConfig.model_fields["recv_poll_interval_seconds"].default,
            ),
        },
    }
    return AssistantRuntimeConfig.model_validate(runtime_payload).model_dump(mode="python")


def _load_yaml_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    if isinstance(payload, dict):
        return payload
    return {}


def _merge_yaml_object(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge_yaml_object(current, value)
            continue
        merged[key] = value
    return merged


def _normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def frame_rms(frame_bytes: bytes) -> int:
    if not frame_bytes:
        return 0
    samples = memoryview(frame_bytes).cast("h")
    if not samples:
        return 0
    energy = sum(int(sample) * int(sample) for sample in samples)
    return int(math.sqrt(energy / len(samples)))


def normalize_command_text(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?：:；;“”\"'（）()\\-]+", "", text).lower()

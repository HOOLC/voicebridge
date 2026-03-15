from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field


DEFAULT_CONFIG_PATH = Path("bridge.yaml")


class ScheduledTaskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    cron: str
    prompt: str
    enabled: bool = True


class VoiceRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tts_resource_id: str = "seed-tts-2.0"
    tts_speaker: str = "zh_female_roumeinvyou_emo_v2_mars_bigtts"
    format: str = "wav"
    sample_rate: int = 24_000
    speed_ratio: float = 1.0
    volume_ratio: float = 1.0
    pitch_ratio: float = 1.0


class AckRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: str = "收到"
    variants: list[str] = Field(
        default_factory=lambda: [
            "收到啦",
            "好呀，收到",
            "嗯嗯，收到",
            "在呢，收到啦",
        ]
    )


class InteractionRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interrupt_playback: bool = True
    cancel_codex_on_interrupt: bool = False
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


class ScheduleRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_interval_seconds: int = 15
    tasks: list[ScheduledTaskConfig] = Field(default_factory=list)


class AssistantRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    voice: VoiceRuntimeConfig = Field(default_factory=VoiceRuntimeConfig)
    ack: AckRuntimeConfig = Field(default_factory=AckRuntimeConfig)
    interaction: InteractionRuntimeConfig = Field(default_factory=InteractionRuntimeConfig)
    commands: CommandsRuntimeConfig = Field(default_factory=CommandsRuntimeConfig)
    schedule: ScheduleRuntimeConfig = Field(default_factory=ScheduleRuntimeConfig)


class BridgeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_root: str = "."
    config_path: str = "bridge.yaml"

    assistant_runtime_config_path: str = "./bridge-home/assistant-runtime.yaml"
    assistant_runtime_example_path: str = "./bridge-home.example/assistant-runtime.yaml"
    assistant_voice_catalog_path: str = "./bridge-home/tts-voices.yaml"
    assistant_voice_catalog_example_path: str = "./bridge-home.example/tts-voices.yaml"
    assistant_state_path: str = "./bridge-home/assistant-state.json"
    assistant_memory_path: str = "./bridge-home/MEMORY.md"
    assistant_memory_example_path: str = "./bridge-home.example/MEMORY.md"
    assistant_daily_memory_dir: str = "./bridge-home/memory"
    assistant_daily_memory_example_dir: str = "./bridge-home.example/memory"

    volcengine_app_id: str | None = None
    volcengine_access_key: str | None = None
    volcengine_asr_resource_id: str = "volc.bigasr.auc_turbo"

    feishu_enabled: bool = False
    feishu_app_id: str | None = None
    feishu_app_secret: str | None = None
    feishu_user_id: str | None = None
    feishu_user_id_type: str = "user_id"

    codex_workspace: str = "./bridge-home"
    codex_command: str = "codex.cmd"
    codex_model: str | None = "gpt-5.3-codex-spark"
    codex_fallback_model: str | None = "gpt-5"
    codex_use_yolo: bool = True
    codex_http_proxy: str | None = None
    codex_https_proxy: str | None = None
    codex_all_proxy: str | None = None
    codex_no_proxy: str | None = None
    codex_session_state_file: str = ".voicebridge-session.json"
    codex_timeout_seconds: int = 600
    extra_search_paths: list[str] = Field(default_factory=list)

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
    def volcengine_tts_resource_id(self) -> str:
        return self.runtime.voice.tts_resource_id

    @property
    def volcengine_tts_speaker(self) -> str:
        return self.runtime.voice.tts_speaker

    @property
    def volcengine_tts_format(self) -> str:
        return self.runtime.voice.format

    @property
    def volcengine_tts_sample_rate(self) -> int:
        return self.runtime.voice.sample_rate

    @property
    def volcengine_tts_speed_ratio(self) -> float:
        return self.runtime.voice.speed_ratio

    @property
    def volcengine_tts_volume_ratio(self) -> float:
        return self.runtime.voice.volume_ratio

    @property
    def volcengine_tts_pitch_ratio(self) -> float:
        return self.runtime.voice.pitch_ratio

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
    def bridge_cancel_codex_on_interrupt(self) -> bool:
        return self.runtime.interaction.cancel_codex_on_interrupt

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
    def scheduler_check_interval_seconds(self) -> int:
        return self.runtime.schedule.check_interval_seconds

    @property
    def scheduled_tasks(self) -> list[ScheduledTaskConfig]:
        return self.runtime.schedule.tasks


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
    runtime_example_path = _resolve_local_path(
        config_path,
        str(data.get("assistant_runtime_example_path") or BridgeConfig.model_fields["assistant_runtime_example_path"].default),
    )
    catalog_path = _resolve_local_path(
        config_path,
        str(data.get("assistant_voice_catalog_path") or BridgeConfig.model_fields["assistant_voice_catalog_path"].default),
    )
    catalog_example_path = _resolve_local_path(
        config_path,
        str(
            data.get("assistant_voice_catalog_example_path")
            or BridgeConfig.model_fields["assistant_voice_catalog_example_path"].default
        ),
    )

    runtime_data = _load_yaml_object(runtime_path, runtime_example_path)
    voice_catalog = _load_voice_catalog_file(catalog_path, catalog_example_path)
    runtime_payload = _build_runtime_payload(runtime_data, voice_catalog)
    config = BridgeConfig.model_validate({**data, "runtime": runtime_payload})
    return config.model_copy(
        update={
            "project_root": str(config_path.parent.resolve()),
            "config_path": str(config_path.resolve()),
            "assistant_runtime_config_path": str(runtime_path),
            "assistant_runtime_example_path": str(runtime_example_path),
            "assistant_voice_catalog_path": str(catalog_path),
            "assistant_voice_catalog_example_path": str(catalog_example_path),
            "assistant_state_path": str(_resolve_local_path(config_path, config.assistant_state_path)),
            "assistant_memory_path": str(_resolve_local_path(config_path, config.assistant_memory_path)),
            "assistant_memory_example_path": str(_resolve_local_path(config_path, config.assistant_memory_example_path)),
            "assistant_daily_memory_dir": str(_resolve_local_path(config_path, config.assistant_daily_memory_dir)),
            "assistant_daily_memory_example_dir": str(
                _resolve_local_path(config_path, config.assistant_daily_memory_example_dir)
            ),
            "codex_workspace": str(_resolve_local_path(config_path, config.codex_workspace)),
            "codex_session_state_file": str(_resolve_local_path(config_path, config.codex_session_state_file)),
            "extra_search_paths": [_expand_env_vars(item) for item in config.extra_search_paths],
            "codex_http_proxy": _expand_env_vars(config.codex_http_proxy) if config.codex_http_proxy else None,
            "codex_https_proxy": _expand_env_vars(config.codex_https_proxy) if config.codex_https_proxy else None,
            "codex_all_proxy": _expand_env_vars(config.codex_all_proxy) if config.codex_all_proxy else None,
            "codex_no_proxy": _expand_env_vars(config.codex_no_proxy) if config.codex_no_proxy else None,
        }
    )


def dump_device(device: dict[str, Any], index: int) -> str:
    name = str(device.get("name", "")).strip()
    hostapi = device.get("hostapi")
    inputs = device.get("max_input_channels")
    outputs = device.get("max_output_channels")
    rate = device.get("default_samplerate")
    return f"[{index}] {name} | hostapi={hostapi} | in={inputs} | out={outputs} | rate={rate}"


def load_voice_catalog(config: BridgeConfig) -> list[dict[str, Any]]:
    return _load_voice_catalog_file(
        Path(config.assistant_voice_catalog_path),
        Path(config.assistant_voice_catalog_example_path),
    )


def _resolve_local_path(config_path: Path, raw_path: str) -> Path:
    path = Path(_expand_env_vars(raw_path))
    if path.is_absolute():
        return path
    return (config_path.parent / path).resolve()


def _expand_env_vars(value: str) -> str:
    return os.path.expandvars(value)


def _apply_env_defaults(data: dict[str, Any]) -> None:
    env_defaults = {
        "volcengine_app_id": (
            os.getenv("VOLCENGINE_APP_ID")
            or os.getenv("DOUBAO_APP_ID")
            or os.getenv("VOLC_APP_ID")
        ),
        "volcengine_access_key": (
            os.getenv("VOLCENGINE_ACCESS_KEY")
            or os.getenv("DOUBAO_ACCESS_KEY")
            or os.getenv("DOUBAO_ACCESS_TOKEN")
            or os.getenv("DOUBAO_API_KEY")
            or os.getenv("VOLC_ACCESS_KEY")
        ),
        "feishu_app_id": os.getenv("FEISHU_APP_ID") or os.getenv("LARK_APP_ID") or os.getenv("APP_ID"),
        "feishu_app_secret": os.getenv("FEISHU_APP_SECRET") or os.getenv("LARK_APP_SECRET") or os.getenv("APP_SECRET"),
        "feishu_user_id": os.getenv("FEISHU_USER_ID") or os.getenv("LARK_USER_ID"),
        "codex_http_proxy": os.getenv("http_proxy") or os.getenv("HTTP_PROXY"),
        "codex_https_proxy": os.getenv("https_proxy") or os.getenv("HTTPS_PROXY"),
        "codex_all_proxy": os.getenv("all_proxy") or os.getenv("ALL_PROXY"),
        "codex_no_proxy": os.getenv("no_proxy") or os.getenv("NO_PROXY"),
    }
    for key, value in env_defaults.items():
        if (key not in data or data.get(key) in (None, "")) and value:
            data[key] = value


def _build_runtime_payload(runtime_data: dict[str, Any], voice_catalog: list[dict[str, Any]]) -> dict[str, Any]:
    voice_data = runtime_data.get("voice") if isinstance(runtime_data.get("voice"), dict) else {}
    ack_data = runtime_data.get("ack") if isinstance(runtime_data.get("ack"), dict) else {}
    interaction_data = runtime_data.get("interaction") if isinstance(runtime_data.get("interaction"), dict) else {}
    commands_data = runtime_data.get("commands") if isinstance(runtime_data.get("commands"), dict) else {}
    schedule_data = runtime_data.get("schedule") if isinstance(runtime_data.get("schedule"), dict) else {}

    runtime_payload = {
        "version": int(runtime_data.get("version") or 1),
        "voice": {
            "tts_resource_id": _first_present(
                voice_data.get("tts_resource_id"),
                VoiceRuntimeConfig.model_fields["tts_resource_id"].default,
            ),
            "tts_speaker": _resolve_tts_speaker(
                _first_present(
                    voice_data.get("tts_speaker"),
                    VoiceRuntimeConfig.model_fields["tts_speaker"].default,
                ),
                voice_catalog,
            ),
            "format": _first_present(
                voice_data.get("format"),
                VoiceRuntimeConfig.model_fields["format"].default,
            ),
            "sample_rate": _first_present(
                voice_data.get("sample_rate"),
                VoiceRuntimeConfig.model_fields["sample_rate"].default,
            ),
            "speed_ratio": _first_present(
                voice_data.get("speed_ratio"),
                VoiceRuntimeConfig.model_fields["speed_ratio"].default,
            ),
            "volume_ratio": _first_present(
                voice_data.get("volume_ratio"),
                VoiceRuntimeConfig.model_fields["volume_ratio"].default,
            ),
            "pitch_ratio": _first_present(
                voice_data.get("pitch_ratio"),
                VoiceRuntimeConfig.model_fields["pitch_ratio"].default,
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
            "cancel_codex_on_interrupt": _first_present(
                interaction_data.get("cancel_codex_on_interrupt"),
                InteractionRuntimeConfig.model_fields["cancel_codex_on_interrupt"].default,
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
        "schedule": {
            "check_interval_seconds": _first_present(
                schedule_data.get("check_interval_seconds"),
                ScheduleRuntimeConfig.model_fields["check_interval_seconds"].default,
            ),
            "tasks": _normalize_tasks(schedule_data.get("tasks")),
        },
    }
    return AssistantRuntimeConfig.model_validate(runtime_payload).model_dump(mode="python")


def _load_yaml_object(primary_path: Path, fallback_path: Path) -> dict[str, Any]:
    for candidate_path in (primary_path, fallback_path):
        if not candidate_path.exists():
            continue
        try:
            payload = yaml.safe_load(candidate_path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _load_voice_catalog_file(catalog_path: Path, example_path: Path) -> list[dict[str, Any]]:
    for candidate_path in (catalog_path, example_path):
        if not candidate_path.exists():
            continue
        try:
            payload = yaml.safe_load(candidate_path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(payload, dict):
            continue
        voices = payload.get("voices")
        if not isinstance(voices, list):
            continue

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
        if normalized:
            return normalized
    return []


def _normalize_tasks(runtime_tasks: Any) -> list[dict[str, Any]]:
    if not isinstance(runtime_tasks, list):
        return []
    normalized = [item for item in runtime_tasks if isinstance(item, dict)]
    return [ScheduledTaskConfig.model_validate(item).model_dump(mode="python") for item in normalized]


def _normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _resolve_tts_speaker(value: Any, voice_catalog: list[dict[str, Any]]) -> str:
    target = str(value).strip()
    if not target:
        return ""
    normalized_target = _normalize_catalog_key(target)
    for item in voice_catalog:
        candidates = [str(item.get("id", "")).strip(), str(item.get("name", "")).strip()]
        aliases = item.get("aliases")
        if isinstance(aliases, list):
            candidates.extend(str(alias).strip() for alias in aliases if str(alias).strip())
        if normalized_target in {_normalize_catalog_key(candidate) for candidate in candidates if candidate}:
            return str(item.get("id", "")).strip() or target
    return target


def _normalize_catalog_key(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", value).lower()


def frame_rms(frame_bytes: bytes) -> int:
    if not frame_bytes:
        return 0
    samples = memoryview(frame_bytes).cast("h")
    if not samples:
        return 0
    energy = sum(int(sample) * int(sample) for sample in samples)
    return int(math.sqrt(energy / len(samples)))

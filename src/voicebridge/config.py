from __future__ import annotations

import os
import math
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field


DEFAULT_CONFIG_PATH = Path("bridge.yaml")


class BridgeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_root: str = "."
    config_path: str = "bridge.yaml"

    speech_provider: str = "volcengine"

    volcengine_app_id: str | None = None
    volcengine_access_key: str | None = None
    volcengine_asr_resource_id: str = "volc.bigasr.auc_turbo"
    volcengine_tts_resource_id: str = "seed-tts-2.0"
    volcengine_tts_cluster: str = "volcano_tts"
    volcengine_tts_speaker: str = "zh_female_roumeinvyou_emo_v2_mars_bigtts"
    volcengine_tts_format: str = "wav"
    volcengine_tts_sample_rate: int = 24_000
    volcengine_tts_speed_ratio: float = 1.0
    volcengine_tts_volume_ratio: float = 1.0
    volcengine_tts_pitch_ratio: float = 1.0
    volcengine_tts_emotion: str | None = None
    bridge_ack_text: str = "收到"
    bridge_ack_variants: list[str] = [
        "收到啦",
        "好呀，收到",
        "嗯嗯，收到",
        "在呢，收到啦"
    ]
    assistant_runtime_config_path: str = "./bridge-home/assistant-runtime.yaml"
    assistant_runtime_example_path: str = "./bridge-home.example/assistant-runtime.yaml"
    assistant_state_path: str = "./bridge-home/assistant-state.json"
    bridge_interrupt_playback: bool = True
    bridge_cancel_codex_on_interrupt: bool = False
    bridge_chunk_chars: int = 36
    bridge_repeat_aliases: list[str] = [
        "再说一遍",
        "再说一次",
        "重复一下",
        "把刚才说的再说一遍",
        "刚才说什么",
    ]
    bridge_stop_aliases: list[str] = [
        "停一下",
        "别说了",
        "先别说",
        "闭嘴",
    ]
    transcript_min_chars: int = 2
    transcript_ignore_phrases: list[str] = [
        "嗯",
        "啊",
        "呃",
        "额",
        "哦",
        "喂",
    ]

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
    codex_timeout_seconds: int = 60
    codex_reply_style_prompt: str = (
        "用简洁、甜一点、自然口语化的中文回答，只说最后要对用户说的话。不要复述思考过程、工具过程、命令输出或中间状态。语气温柔一点，但别油腻，也别太长。除非用户明确要求，否则控制在 1 到 4 句，并且自然一点，不要每次都用同样的句式。"
    )
    extra_search_paths: list[str] = []

    capture_device: str | int
    playback_device: str | int

    sample_rate: int = 16_000
    channels: int = 1
    frame_ms: int = 20
    vad_mode: int = Field(default=2, ge=0, le=3)
    vad_rms_threshold: int = 900
    speech_start_min_voiced_ms: int = 120
    min_speech_ms: int = 400
    silence_ms: int = 900
    preroll_ms: int = 300
    max_utterance_ms: int = 15_000

    play_remote_audio: bool = True
    print_transcript: bool = True
    print_status: bool = True

    @property
    def frame_bytes(self) -> int:
        samples = self.sample_rate * self.frame_ms // 1000
        return samples * self.channels * 2


def load_config(path: str | Path | None = None) -> BridgeConfig:
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    load_dotenv(config_path.parent / ".env", override=False)
    raw_text = config_path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw_text) or {}
    if not isinstance(data, dict):
        raise ValueError("Config file must contain a YAML object")

    _apply_env_defaults(data)
    _apply_runtime_assistant_overrides(config_path, data)
    config = BridgeConfig.model_validate(data)
    return config.model_copy(
        update={
            "project_root": str(config_path.parent.resolve()),
            "config_path": str(config_path.resolve()),
            "assistant_runtime_config_path": str(_resolve_local_path(config_path, config.assistant_runtime_config_path)),
            "assistant_runtime_example_path": str(_resolve_local_path(config_path, config.assistant_runtime_example_path)),
            "assistant_state_path": str(_resolve_local_path(config_path, config.assistant_state_path)),
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
        "codex_http_proxy": os.getenv("http_proxy") or os.getenv("HTTP_PROXY"),
        "codex_https_proxy": os.getenv("https_proxy") or os.getenv("HTTPS_PROXY"),
        "codex_all_proxy": os.getenv("all_proxy") or os.getenv("ALL_PROXY"),
        "codex_no_proxy": os.getenv("no_proxy") or os.getenv("NO_PROXY"),
    }
    for key, value in env_defaults.items():
        if (key not in data or data.get(key) in (None, "")) and value:
            data[key] = value


def _apply_runtime_assistant_overrides(config_path: Path, data: dict[str, Any]) -> None:
    runtime_raw = str(data.get("assistant_runtime_config_path") or BridgeConfig.model_fields["assistant_runtime_config_path"].default)
    example_raw = str(data.get("assistant_runtime_example_path") or BridgeConfig.model_fields["assistant_runtime_example_path"].default)
    runtime_path = _resolve_local_path(config_path, runtime_raw)
    example_path = _resolve_local_path(config_path, example_raw)
    runtime_data = _load_runtime_assistant_file(runtime_path, example_path)
    if not runtime_data:
        return

    voice = runtime_data.get("voice")
    if isinstance(voice, dict):
        _set_if_present(data, "volcengine_tts_resource_id", voice.get("tts_resource_id"))
        _set_if_present(data, "volcengine_tts_speaker", voice.get("tts_speaker"))
        _set_if_present(data, "volcengine_tts_format", voice.get("format"))
        _set_if_present(data, "volcengine_tts_sample_rate", voice.get("sample_rate"))
        _set_if_present(data, "volcengine_tts_speed_ratio", voice.get("speed_ratio"))
        _set_if_present(data, "volcengine_tts_volume_ratio", voice.get("volume_ratio"))
        _set_if_present(data, "volcengine_tts_pitch_ratio", voice.get("pitch_ratio"))

    ack = runtime_data.get("ack")
    if isinstance(ack, dict):
        _set_if_present(data, "bridge_ack_text", ack.get("default"))
        variants = ack.get("variants")
        if isinstance(variants, list) and variants:
            data["bridge_ack_variants"] = [str(item) for item in variants if str(item).strip()]

    style = runtime_data.get("style")
    if isinstance(style, dict):
        _set_if_present(data, "codex_reply_style_prompt", style.get("codex_reply_style_prompt"))

    interaction = runtime_data.get("interaction")
    if isinstance(interaction, dict):
        _set_if_present(data, "bridge_interrupt_playback", interaction.get("interrupt_playback"))
        _set_if_present(data, "bridge_cancel_codex_on_interrupt", interaction.get("cancel_codex_on_interrupt"))
        _set_if_present(data, "bridge_chunk_chars", interaction.get("reply_chunk_chars"))


def _load_runtime_assistant_file(runtime_path: Path, example_path: Path) -> dict[str, Any]:
    runtime_path.parent.mkdir(parents=True, exist_ok=True)

    if not example_path.exists():
        return {}

    if not runtime_path.exists():
        runtime_path.write_text(example_path.read_text(encoding="utf-8"), encoding="utf-8")

    for candidate_path in (runtime_path, example_path):
        try:
            payload = yaml.safe_load(candidate_path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            payload = None
        if isinstance(payload, dict):
            if candidate_path is example_path and runtime_path.read_text(encoding="utf-8") != example_path.read_text(encoding="utf-8"):
                runtime_path.write_text(example_path.read_text(encoding="utf-8"), encoding="utf-8")
            return payload

    runtime_path.write_text(example_path.read_text(encoding="utf-8"), encoding="utf-8")
    payload = yaml.safe_load(example_path.read_text(encoding="utf-8")) or {}
    return payload if isinstance(payload, dict) else {}


def _set_if_present(data: dict[str, Any], key: str, value: Any) -> None:
    if value not in (None, ""):
        data[key] = value


def frame_rms(frame_bytes: bytes) -> int:
    if not frame_bytes:
        return 0
    samples = memoryview(frame_bytes).cast("h")
    if not samples:
        return 0
    energy = sum(int(sample) * int(sample) for sample in samples)
    return int(math.sqrt(energy / len(samples)))

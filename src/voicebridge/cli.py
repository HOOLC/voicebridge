from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .audio import AudioDeviceResolver
from .codex_runner import CodexRunner
from .config import dump_device, load_config, load_voice_catalog
from .volcengine_audio import VolcengineAudioClient
from .voice_catalog import build_official_voice_catalog, write_official_voice_catalog
from .workspace import ensure_runtime_workspace, read_runtime_state


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001
        pass
    parser = _build_parser()
    args = parser.parse_args()
    args.func(args)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voicebridge")
    subparsers = parser.add_subparsers(dest="command", required=True)

    devices_parser = subparsers.add_parser("devices", help="List local audio devices")
    devices_parser.set_defaults(func=cmd_devices)

    run_parser = subparsers.add_parser("run", help="Run the local VoiceBridge service")
    run_parser.add_argument("--config", default="bridge.yaml")
    run_parser.set_defaults(func=cmd_run)

    status_parser = subparsers.add_parser("status", help="Show current VoiceBridge session state")
    status_parser.add_argument("--config", default="bridge.yaml")
    status_parser.set_defaults(func=cmd_status)

    check_parser = subparsers.add_parser("check", help="Run a read-only local health check")
    check_parser.add_argument("--config", default="bridge.yaml")
    check_parser.set_defaults(func=cmd_check)

    voices_parser = subparsers.add_parser("voices", help="List local TTS voices from bridge-home")
    voices_parser.add_argument("--config", default="bridge.yaml")
    voices_parser.add_argument("--refresh", action="store_true", help="Refresh the local voice catalog from official sources")
    voices_parser.add_argument("--output", default="", help="Write refreshed catalog to a specific YAML path")
    voices_parser.set_defaults(func=cmd_voices)

    reset_parser = subparsers.add_parser("reset-session", help="Delete the saved Codex thread id")
    reset_parser.add_argument("--config", default="bridge.yaml")
    reset_parser.set_defaults(func=cmd_reset_session)

    session_parser = subparsers.add_parser("session", help="Show the current Codex session state")
    session_parser.add_argument("--config", default="bridge.yaml")
    session_parser.set_defaults(func=cmd_session)

    return parser


def cmd_devices(_args: argparse.Namespace) -> None:
    for index, device in enumerate(AudioDeviceResolver.list_devices()):
        print(dump_device(device, index))


def cmd_status(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    ensure_runtime_workspace(config)
    codex = CodexRunner(config)
    state = read_runtime_state(config)
    session = codex.describe_state()
    runtime = state.get("runtime") or {}
    meta = state.get("meta") or {}
    print("[install]")
    print(f"workspace={config.codex_workspace}")
    print(f"capture_device={config.capture_device}")
    print(f"playback_device={config.playback_device}")
    print(f"feishu_enabled={config.feishu_enabled}")
    print(f"feishu_user_id={config.feishu_user_id or ''}")
    print(f"runtime_config={config.assistant_runtime_config_path}")
    print()

    print("[runtime]")
    print(f"tts_speaker={config.volcengine_tts_speaker}")
    print(f"tts_speed_ratio={config.volcengine_tts_speed_ratio}")
    print(f"interrupt_playback={config.bridge_interrupt_playback}")
    print(f"cancel_codex_on_interrupt={config.bridge_cancel_codex_on_interrupt}")
    print(f"scheduled_task_count={len(config.scheduled_tasks)}")
    print(f"memory_path={config.assistant_memory_path}")
    print()

    print("[state]")
    print(f"thread_id={session.get('thread_id', '')}")
    print(f"active_model={session.get('active_model', '')}")
    print(f"turn_count={session.get('turn_count', '')}")
    print(f"queue_depth={runtime.get('queue_depth', 0)}")
    print(f"codex_busy={runtime.get('codex_busy', False)}")
    print(f"last_user_text={runtime.get('last_user_text', '')}")
    print(f"last_reply_text={runtime.get('last_reply_text', '')}")
    print(f"last_error={runtime.get('last_error', '')}")
    print(f"shared_thread_id={meta.get('shared_thread_id', '')}")


def cmd_check(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    ensure_runtime_workspace(config)
    results: list[tuple[str, bool, str]] = []

    results.append(("config.load", True, str(Path(config.config_path))))

    try:
        AudioDeviceResolver.resolve_device(config.capture_device, needs_input=True)
        results.append(("audio.capture_device", True, str(config.capture_device)))
    except Exception as error:  # noqa: BLE001
        results.append(("audio.capture_device", False, str(error)))

    try:
        AudioDeviceResolver.resolve_device(config.playback_device, needs_output=True)
        results.append(("audio.playback_device", True, str(config.playback_device)))
    except Exception as error:  # noqa: BLE001
        results.append(("audio.playback_device", False, str(error)))

    results.extend(
        [
            ("workspace.runtime_config", Path(config.assistant_runtime_config_path).exists(), config.assistant_runtime_config_path),
            ("workspace.voice_catalog", Path(config.assistant_voice_catalog_path).exists(), config.assistant_voice_catalog_path),
            ("workspace.memory", Path(config.assistant_memory_path).exists(), config.assistant_memory_path),
            ("workspace.memory_dir", Path(config.assistant_daily_memory_dir).exists(), config.assistant_daily_memory_dir),
        ]
    )

    speech_ok = bool(config.volcengine_app_id and config.volcengine_access_key)
    results.append(("credentials.speech", speech_ok, "volcengine_app_id/access_key"))

    feishu_ok = True
    if config.feishu_enabled:
        feishu_ok = bool(config.feishu_app_id and config.feishu_app_secret and config.feishu_user_id)
    results.append(("credentials.feishu", feishu_ok, "feishu_enabled requires app_id/app_secret/user_id"))

    codex_ok = bool(str(config.codex_command).strip())
    results.append(("codex.command", codex_ok, str(config.codex_command)))

    for name, ok, detail in results:
        status = "ok" if ok else "fail"
        print(f"{status} {name} {detail}")

    if any(not ok for _, ok, _ in results):
        raise SystemExit(1)


def cmd_voices(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    ensure_runtime_workspace(config)
    if args.refresh:
        output_path = Path(args.output).resolve() if args.output else Path(config.assistant_voice_catalog_path)
        voices = build_official_voice_catalog()
        compatible_voices = _filter_compatible_voices(config, voices)
        write_official_voice_catalog(output_path, compatible_voices)
        print(f"refreshed={output_path}")
        print(f"candidate_voice_count={len(voices)}")
        print(f"compatible_voice_count={len(compatible_voices)}")
        print()
    voices = load_voice_catalog(config)

    print("[active voice]")
    print(f"tts_speaker={config.volcengine_tts_speaker}")
    print(f"tts_volume_ratio={config.volcengine_tts_volume_ratio}")
    print()
    print("[voice catalog]")
    print(f"path={config.assistant_voice_catalog_path}")
    if not voices:
        print("none")
        return

    print(f"count={len(voices)}")
    for item in voices:
        marker = "*" if item.get("id") == config.volcengine_tts_speaker else "-"
        print(f"{marker} {item.get('name', '')} | id={item.get('id', '')}")
        aliases = item.get("aliases") or []
        if aliases:
            print(f"  aliases={', '.join(str(alias) for alias in aliases)}")
        description = str(item.get("description", "")).strip()
        if description:
            print(f"  description={description}")


def _filter_compatible_voices(config, voices: list[dict]) -> list[dict]:
    client = VolcengineAudioClient(config)
    compatible: list[dict] = []
    try:
        for item in voices:
            speaker = str(item.get("id", "")).strip()
            if not speaker:
                continue
            voice_config = config.runtime.voice.model_copy(update={"tts_speaker": speaker})
            client.config = config.model_copy(update={"runtime": config.runtime.model_copy(update={"voice": voice_config})})
            try:
                client.synthesize("你好")
            except Exception:
                continue
            compatible.append(item)
    finally:
        client.close()
    return compatible


def cmd_session(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    ensure_runtime_workspace(config)
    codex = CodexRunner(config)
    for key, value in codex.describe_state().items():
        print(f"{key}={value}")


def cmd_reset_session(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    ensure_runtime_workspace(config)
    state_path = config.codex_session_state_file
    if os.path.exists(state_path):
        os.remove(state_path)
        print(f"removed {state_path}")
        return
    print(f"not found: {state_path}")


def cmd_run(args: argparse.Namespace) -> None:
    from .bridge_runtime import BridgeRuntime

    config = load_config(args.config)
    ensure_runtime_workspace(config)
    runtime = BridgeRuntime(config)

    print(f"workspace={config.codex_workspace}")
    print(f"capture_device={config.capture_device}")
    print(f"playback_device={config.playback_device}")
    print(f"tts_speaker={config.volcengine_tts_speaker}")
    print(f"codex_model={config.codex_model}")
    print(f"feishu_enabled={config.feishu_enabled}")
    print(f"scheduled_task_count={len(config.scheduled_tasks)}")
    print(
        (
            f"VAD判定: mode={config.vad_mode}, rms阈值={config.vad_rms_threshold}, "
            f"起始连续语音={config.speech_start_min_voiced_ms}ms, "
            f"结束静音={config.silence_ms}ms"
        ),
        flush=True,
    )
    print("桥接已启动，按 Ctrl+C 停止", flush=True)

    try:
        runtime.run()
    except KeyboardInterrupt:
        print("stopping", flush=True)
    finally:
        runtime.close()


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

from .audio import AudioDeviceResolver
from .config import dump_device, load_config
from .workspace import ensure_runtime_workspace, read_runtime_state


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
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

    status_parser = subparsers.add_parser("status", help="Show current VoiceBridge phone bridge state")
    status_parser.add_argument("--config", default="bridge.yaml")
    status_parser.set_defaults(func=cmd_status)

    check_parser = subparsers.add_parser("check", help="Run a read-only local health check")
    check_parser.add_argument("--config", default="bridge.yaml")
    check_parser.set_defaults(func=cmd_check)

    return parser


def cmd_devices(_args: argparse.Namespace) -> None:
    for index, device in enumerate(AudioDeviceResolver.list_devices()):
        print(dump_device(device, index))


def cmd_status(args: argparse.Namespace) -> None:
    config = _load_prepared_config(args.config)
    state = read_runtime_state(config)
    runtime = state.get("runtime") or {}
    meta = state.get("meta") or {}
    print("[install]")
    print(f"phone_bridge_command={config.phone_bridge_command}")
    print(f"capture_device={config.capture_device}")
    print(f"playback_device={config.playback_device}")
    print(f"runtime_config={config.assistant_runtime_config_path}")
    print()

    print("[runtime]")
    print(f"tts_model={config.tts_model}")
    print(f"tts_voice_id={config.tts_voice_id}")
    print(f"tts_sample_rate={config.tts_sample_rate}")
    print(f"interrupt_playback={config.bridge_interrupt_playback}")
    print(f"reply_source={config.phone_reply_source}")
    print(f"reply_timeout_seconds={config.phone_reply_timeout_seconds}")
    print()

    print("[state]")
    print(f"busy={runtime.get('busy', False)}")
    print(f"queue_depth={runtime.get('queue_depth', 0)}")
    print(f"last_user_text={runtime.get('last_user_text', '')}")
    print(f"last_reply_text={runtime.get('last_reply_text', '')}")
    print(f"last_reply_message_id={runtime.get('last_reply_message_id', '') or meta.get('last_reply_message_id', '')}")
    print(f"last_error={runtime.get('last_error', '')}")


def cmd_check(args: argparse.Namespace) -> None:
    config = _load_prepared_config(args.config)
    results: list[tuple[str, bool, str]] = []

    results.append(("config.load", True, str(Path(config.config_path))))

    try:
        AudioDeviceResolver.resolve_device(config.capture_device, needs_input=True)
        results.append(("audio.capture_device", True, str(config.capture_device)))
    except Exception as error:
        results.append(("audio.capture_device", False, str(error)))

    try:
        AudioDeviceResolver.resolve_device(config.playback_device, needs_output=True)
        results.append(("audio.playback_device", True, str(config.playback_device)))
    except Exception as error:
        results.append(("audio.playback_device", False, str(error)))

    results.extend(
        [
            ("workspace.runtime_config", Path(config.assistant_runtime_config_path).exists(), config.assistant_runtime_config_path),
            ("workspace.state_dir", Path(config.assistant_state_path).parent.exists(), str(Path(config.assistant_state_path).parent)),
        ]
    )

    minimax_ok = bool(config.minimax_api_key)
    results.append(("credentials.minimax", minimax_ok, "MINIMAX_API_KEY"))

    asr_ok = bool(config.volcengine_app_id and config.volcengine_access_key)
    results.append(("credentials.asr", asr_ok, "bridge.yaml: volcengine_app_id/access_key"))

    phone_backend_ok = _wsl_command_exists(config.phone_bridge_command)
    results.append(("phone_bridge.command", phone_backend_ok, config.phone_bridge_command))

    for name, ok, detail in results:
        status = "ok" if ok else "fail"
        print(f"{status} {name} {detail}")

    if any(not ok for _, ok, _ in results):
        raise SystemExit(1)


def _wsl_command_exists(command: str) -> bool:
    script = f"command -v {shlex.quote(command)} >/dev/null 2>&1"
    result = subprocess.run(
        ["wsl", "bash", "-lc", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )
    return result.returncode == 0


def _load_prepared_config(config_path: str):
    config = load_config(config_path)
    ensure_runtime_workspace(config)
    return load_config(config_path)


def cmd_run(args: argparse.Namespace) -> None:
    from .bridge_runtime import BridgeRuntime

    config = _load_prepared_config(args.config)
    runtime = BridgeRuntime(config)

    print(f"phone_bridge_command={config.phone_bridge_command}")
    print(f"capture_device={config.capture_device}")
    print(f"playback_device={config.playback_device}")
    print(f"tts_model={config.tts_model}")
    print(f"tts_voice_id={config.tts_voice_id}")
    print(f"reply_source={config.phone_reply_source}")
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

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from .audio import AudioDeviceResolver
from .bridge_runtime import BridgeRuntime
from .codex_runner import CodexRunner
from .config import dump_device, load_config


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
    _ensure_runtime_workspace(config)
    codex = CodexRunner(config)
    session = codex.describe_state()
    print("[codex workspace]")
    print(config.codex_workspace)
    print()
    print("[codex session]")
    for key in ("thread_id", "requested_model", "active_model", "updated_at", "turn_count", "resume_command"):
        print(f"{key}={session.get(key, '')}")
    print()
    print("[speech]")
    print(f"provider={config.speech_provider}")
    print(f"volcengine_app_id={config.volcengine_app_id or ''}")
    print(f"tts_speaker={config.volcengine_tts_speaker}")


def cmd_session(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    _ensure_runtime_workspace(config)
    codex = CodexRunner(config)
    for key, value in codex.describe_state().items():
        print(f"{key}={value}")


def cmd_reset_session(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    _ensure_runtime_workspace(config)
    state_path = config.codex_session_state_file
    import os

    if os.path.exists(state_path):
        os.remove(state_path)
        print(f"removed {state_path}")
        return
    print(f"not found: {state_path}")


def cmd_run(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    _ensure_runtime_workspace(config)
    runtime = BridgeRuntime(config)

    print(f"workspace={config.codex_workspace}")
    print(f"capture_device={config.capture_device}")
    print(f"playback_device={config.playback_device}")
    print(f"speech_provider={config.speech_provider}")
    print(f"tts_speaker={config.volcengine_tts_speaker}")
    print(f"codex_model={config.codex_model}")
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


def _ensure_runtime_workspace(config) -> None:
    workdir = Path(config.codex_workspace)
    example_dir = Path(config.assistant_runtime_example_path).parent
    if not example_dir.exists():
        workdir.mkdir(parents=True, exist_ok=True)
        return
    if not workdir.exists():
        shutil.copytree(example_dir, workdir)
        return
    for example_file in example_dir.iterdir():
        target = workdir / example_file.name
        if target.exists():
            continue
        if example_file.is_dir():
            shutil.copytree(example_file, target)
        else:
            shutil.copy2(example_file, target)


if __name__ == "__main__":
    main()

from __future__ import annotations

import json
import threading
from datetime import date
from pathlib import Path
from typing import Any

from .config import BridgeConfig, load_config

class RuntimeConfigManager:
    def __init__(self, config: BridgeConfig):
        self._config_path = config.config_path
        self._lock = threading.Lock()
        self._config = config

    def get(self) -> BridgeConfig:
        with self._lock:
            return self._config

    def reload(self) -> BridgeConfig:
        refreshed = load_config(self._config_path)
        with self._lock:
            self._config = refreshed
            return refreshed


def ensure_runtime_workspace(config: BridgeConfig) -> None:
    workspace_dir = Path(config.codex_workspace)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    _copy_if_missing(Path(config.assistant_runtime_example_path), Path(config.assistant_runtime_config_path))
    _copy_if_missing(Path(config.assistant_voice_catalog_example_path), Path(config.assistant_voice_catalog_path))
    _copy_if_missing(Path(config.assistant_memory_example_path), Path(config.assistant_memory_path))

    _copy_tree_if_missing(Path(config.assistant_daily_memory_example_dir), Path(config.assistant_daily_memory_dir))

    Path(config.assistant_state_path).parent.mkdir(parents=True, exist_ok=True)
    Path(config.assistant_daily_memory_dir).mkdir(parents=True, exist_ok=True)


def read_runtime_state(config: BridgeConfig) -> dict[str, Any]:
    state_path = Path(config.assistant_state_path)
    if not state_path.exists():
        return {}
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return payload if isinstance(payload, dict) else {}


def get_today_memory_path(config: BridgeConfig) -> Path:
    daily_dir = Path(config.assistant_daily_memory_dir)
    daily_dir.mkdir(parents=True, exist_ok=True)
    return daily_dir / f"{date.today().isoformat()}.md"


def load_memory_context(config: BridgeConfig, *, max_chars_per_file: int = 4000) -> dict[str, str]:
    long_term = _read_text(Path(config.assistant_memory_path), max_chars=max_chars_per_file)
    today = _read_text(get_today_memory_path(config), max_chars=max_chars_per_file)
    return {
        "long_term": long_term,
        "daily": today,
    }
def _copy_if_missing(example_path: Path, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists() or not example_path.exists():
        return
    target_path.write_text(example_path.read_text(encoding="utf-8"), encoding="utf-8")


def _copy_tree_if_missing(example_dir: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    if not example_dir.exists():
        return
    for source in example_dir.rglob("*"):
        relative = source.relative_to(example_dir)
        destination = target_dir / relative
        if source.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def _read_text(path: Path, *, max_chars: int) -> str:
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        return ""
    if len(text) <= max_chars:
        return text
    return text[-max_chars:].strip()

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .config import BridgeConfig, load_config
from .runtime_defaults import DEFAULT_ASSISTANT_RUNTIME_YAML


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
    _write_text_if_missing(Path(config.assistant_runtime_config_path), DEFAULT_ASSISTANT_RUNTIME_YAML)
    Path(config.assistant_state_path).parent.mkdir(parents=True, exist_ok=True)


def read_runtime_state(config: BridgeConfig) -> dict[str, Any]:
    state_path = Path(config.assistant_state_path)
    if not state_path.exists():
        return {}
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_text_if_missing(target_path: Path, content: str) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        return
    target_path.write_text(content, encoding="utf-8")

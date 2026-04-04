from __future__ import annotations

import json
import shlex
import subprocess
import time
from dataclasses import dataclass

from .config import BridgeConfig


@dataclass(slots=True)
class PhoneBridgeMessage:
    message_id: str
    text: str
    source: str


class PhoneBridgeBackend:
    def __init__(self, config: BridgeConfig):
        self.config = config

    def send_text(self, text: str) -> None:
        clean_text = text.strip()
        if not clean_text:
            raise ValueError("phone-send requires non-empty content")
        command = [
            self.config.phone_bridge_command,
            "phone-send",
            "--content",
            clean_text,
        ]
        from_name = self.config.phone_from_name.strip()
        if from_name:
            command.extend(["--from", from_name])
        self._run_command(command, timeout=30)

    def get_latest_message_id(self, *, source: str) -> str:
        messages = self._recv_messages(after_id="", source=source, limit=1, timeout=15)
        if not messages:
            return ""
        return messages[-1].message_id

    def wait_for_reply(self, *, after_id: str, source: str, timeout_seconds: int) -> PhoneBridgeMessage:
        deadline = time.monotonic() + max(5, timeout_seconds)
        last_seen_id = after_id.strip()
        while time.monotonic() < deadline:
            messages = self._recv_messages(after_id=last_seen_id, source=source, limit=1, timeout=15)
            if messages:
                return messages[-1]
            time.sleep(max(0.2, self.config.phone_recv_poll_interval_seconds))
        raise TimeoutError(f"phone-recv 在 {timeout_seconds} 秒内没有拿到新回复")

    def _recv_messages(
        self,
        *,
        after_id: str,
        source: str,
        limit: int,
        timeout: int,
    ) -> list[PhoneBridgeMessage]:
        command = [
            self.config.phone_bridge_command,
            "phone-recv",
            "--source",
            source,
            "--limit",
            str(max(1, limit)),
        ]
        if after_id:
            command.extend(["--after-id", after_id])
        output = self._run_command(command, timeout=timeout)
        messages: list[PhoneBridgeMessage] = []
        for line in output.splitlines():
            stripped = line.strip()
            if not stripped.startswith("{"):
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            message_id = str(payload.get("id") or payload.get("itemId") or "").strip()
            text = str(payload.get("content") or "").strip()
            message_source = str(payload.get("source") or "").strip()
            if not message_id or not text:
                continue
            messages.append(PhoneBridgeMessage(message_id=message_id, text=text, source=message_source))
        return messages

    @staticmethod
    def _filter_command_output(text: str) -> str:
        lines: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()
            if not stripped:
                continue
            lowered = stripped.lower()
            if lowered.startswith("proxy set to:"):
                continue
            lines.append(line)
        return "\n".join(lines)

    def _run_command(self, command: list[str], *, timeout: int) -> str:
        script = " ".join(shlex.quote(part) for part in command)
        result = subprocess.run(
            ["wsl", "bash", "-lc", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        stdout = self._filter_command_output(result.stdout)
        stderr = self._filter_command_output(result.stderr)
        combined = "\n".join(part for part in (stdout, stderr) if part).strip()
        if result.returncode != 0:
            raise RuntimeError(combined or f"{self.config.phone_bridge_command} exited with code {result.returncode}")
        return stdout

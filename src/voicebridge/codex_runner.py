from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import BridgeConfig
from .interactions import AssistantReply, TurnSource, parse_assistant_reply
from .workspace import load_memory_context


class CodexCancelledError(RuntimeError):
    pass


@dataclass(slots=True)
class CodexRunResult:
    thread_id: str
    reply: AssistantReply
    raw_reply_text: str
    active_model: str | None


@dataclass(slots=True)
class CompactSessionResult:
    carryover_summary: str
    compacted_at: str
    reason: str
    previous_thread_id: str


class CodexRunner:
    _KIMI_SHARED_SESSION_ID = "__KIMI_CONTINUE__"

    def __init__(self, config: BridgeConfig):
        self.config = config
        self.state_path = Path(config.codex_session_state_file)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        Path(config.codex_workspace).mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._active_process: subprocess.Popen[bytes] | None = None
        self._cancelled_pids: set[int] = set()

    def run(
        self,
        user_text: str,
        *,
        prefer_voice_reply: bool,
        source: TurnSource,
        persist_session: bool = True,
    ) -> CodexRunResult:
        state = self._load_state() if persist_session else {}
        compact_result: CompactSessionResult | None = None
        if persist_session:
            state, compact_result = self._auto_compact_if_needed(state)

        prompt = self._build_prompt(
            user_text,
            prefer_voice_reply=prefer_voice_reply,
            source=source,
            carryover_summary=compact_result.carryover_summary if compact_result else "",
        )

        try:
            result = self._run_once(
                prompt=prompt,
                output_model=self.config.agent_primary_model,
                thread_id=state.get("thread_id") if persist_session else None,
            )
        except RuntimeError as error:
            fallback_model = self.config.agent_fallback_model
            if not fallback_model or not self._is_unsupported_model_error(str(error), str(error)):
                raise
            result = self._run_once(
                prompt=prompt,
                output_model=fallback_model,
                thread_id=state.get("thread_id") if persist_session else None,
            )

        thread_id, raw_reply_text = self._parse_codex_output(
            result["stdout_text"],
            result["output_path"],
            stream_thread_id=str(result.get("stream_thread_id", "")),
            stream_reply_text=str(result.get("stream_reply_text", "")),
            allow_state_fallback=persist_session,
        )
        reply = parse_assistant_reply(raw_reply_text, prefer_voice_reply=prefer_voice_reply)
        active_model = result["active_model"]
        if persist_session:
            self._save_state(
                thread_id=thread_id,
                active_model=active_model,
                requested_model=self.config.agent_primary_model,
                reply_text=reply.preview_text,
                raw_reply_text=raw_reply_text,
                input_chars=len(prompt),
                previous_state=state,
                compact_result=compact_result,
            )
        return CodexRunResult(
            thread_id=thread_id,
            reply=reply,
            raw_reply_text=raw_reply_text,
            active_model=active_model,
        )

    def cancel_current(self) -> None:
        with self._lock:
            process = self._active_process
            if process is None:
                return
            self._cancelled_pids.add(process.pid)
        self._kill_process_tree(process)

    def describe_state(self) -> dict[str, str]:
        state = self._load_state()
        thread_id = str(state.get("thread_id", "")).strip()
        return {
            "thread_id": thread_id,
            "cli_provider": self._stringify_state_value(state.get("cli_provider", self.config.agent_cli_provider)),
            "cli_command": self._stringify_state_value(state.get("cli_command", self.config.agent_cli_command)),
            "requested_model": self._stringify_state_value(state.get("requested_model", "")),
            "active_model": self._stringify_state_value(state.get("active_model", "")),
            "updated_at": self._stringify_state_value(state.get("updated_at", "")),
            "turn_count": str(state.get("turn_count", 0)),
            "estimated_history_chars": str(state.get("estimated_history_chars", 0)),
            "compact_count": str(state.get("compact_count", 0)),
            "last_compact_at": self._stringify_state_value(state.get("last_compact_at", "")),
            "auto_compact_trigger_chars": str(self.config.codex_auto_compact_trigger_chars),
            "auto_compact_turn_threshold": str(self.config.codex_auto_compact_turn_threshold),
            "resume_command": self._format_resume_command(thread_id),
        }

    def reset_session(self) -> None:
        self.cancel_current()
        self.state_path.unlink(missing_ok=True)

    def build_background_command(
        self,
        *,
        workspace: str,
        model: str | None = None,
        use_yolo: bool | None = None,
        cli_provider: str = "configured",
    ) -> list[str]:
        provider = self.config.agent_cli_provider if cli_provider == "configured" else cli_provider
        cli_command = self._resolve_cli_command(provider)
        should_use_yolo = self.config.codex_use_yolo if use_yolo is None else use_yolo
        active_model = model if model is not None else self._default_model_for_provider(provider)

        if provider == "kimi":
            command = [
                cli_command,
                "--print",
                "--output-format",
                "text",
                "--final-message-only",
                "--input-format",
                "text",
                "-w",
                workspace,
            ]
            if should_use_yolo:
                command.append("--yolo")
            if active_model:
                command.extend(["-m", active_model])
            return command

        command = [cli_command, "exec", "-C", workspace]
        if should_use_yolo:
            command.append("--yolo")
        command.extend(["--skip-git-repo-check", "--add-dir", self.config.project_root])
        for extra_path in self.config.extra_search_paths:
            if str(extra_path).strip():
                command.extend(["--add-dir", str(extra_path).strip()])
        if not should_use_yolo:
            command.append("--dangerously-bypass-approvals-and-sandbox")
        if active_model:
            command.extend(["-m", active_model])
        command.append("-")
        return command

    def build_process_env(self) -> dict[str, str]:
        return self._build_env()

    def _run_once(self, *, prompt: str, output_model: str | None, thread_id: str | None) -> dict[str, object]:
        prompt = self._sanitize_prompt_for_cli(prompt)
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".txt", delete=False) as output_file:
            output_path = Path(output_file.name)

        command = self._build_command(prompt=prompt, output_path=output_path, thread_id=thread_id, model=output_model)
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=self._build_env(),
        )
        self._write_prompt_to_stdin(process, prompt)
        with self._lock:
            self._active_process = process

        event_queue: "queue.Queue[tuple[str, str | None]]" = queue.Queue()
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        stream_thread_id = ""
        stream_reply_text = ""
        saw_task_complete = False

        stdout_thread = threading.Thread(
            target=self._read_pipe_lines,
            args=(process.stdout, "stdout", event_queue),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._read_pipe_lines,
            args=(process.stderr, "stderr", event_queue),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        try:
            start_time = time.monotonic()
            stdout_done = False
            stderr_done = False

            while True:
                try:
                    source, payload = event_queue.get(timeout=0.2)
                except queue.Empty:
                    source = ""
                    payload = None

                if source == "stdout" and payload is not None:
                    stdout_lines.append(payload)
                    parsed_thread_id, parsed_reply, parsed_complete = self._parse_stream_event(payload)
                    if parsed_thread_id:
                        stream_thread_id = parsed_thread_id
                    if parsed_reply:
                        stream_reply_text = parsed_reply
                    if parsed_complete:
                        saw_task_complete = True
                        break
                elif source == "stderr" and payload is not None:
                    stderr_lines.append(payload)
                elif source == "stdout_done":
                    stdout_done = True
                elif source == "stderr_done":
                    stderr_done = True

                if process.poll() is not None and stdout_done and stderr_done:
                    break

                if time.monotonic() - start_time > self.config.codex_timeout_seconds:
                    self._kill_process_tree(process)
                    raise RuntimeError(f"{self.config.agent_cli_name} 超时，{self.config.codex_timeout_seconds} 秒内没有完成")

            if saw_task_complete and process.poll() is None:
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self._kill_process_tree(process)

            stdout_thread.join(timeout=1.0)
            stderr_thread.join(timeout=1.0)
        finally:
            with self._lock:
                if self._active_process is process:
                    self._active_process = None

        stdout_text = "".join(stdout_lines)
        stderr_text = "".join(stderr_lines)
        cancelled = process.pid in self._cancelled_pids
        self._cancelled_pids.discard(process.pid)

        if cancelled:
            output_path.unlink(missing_ok=True)
            raise CodexCancelledError("Codex run cancelled")

        if process.returncode not in (None, 0) and not saw_task_complete and not stream_reply_text:
            output_path.unlink(missing_ok=True)
            combined = "\n".join(part for part in (stdout_text, stderr_text) if part).strip()
            raise RuntimeError(combined or f"Codex exited with code {process.returncode}")

        return {
            "stdout_text": stdout_text,
            "stderr_text": stderr_text,
            "output_path": output_path,
            "active_model": output_model,
            "stream_thread_id": stream_thread_id,
            "stream_reply_text": stream_reply_text,
        }

    def _build_command(self, *, prompt: str, output_path: Path, thread_id: str | None, model: str | None) -> list[str]:
        cli_command = self._resolve_agent_cli_command()
        if self.config.agent_cli_provider == "kimi":
            command = [
                cli_command,
                "--print",
                "--output-format",
                "text",
                "--final-message-only",
                "--input-format",
                "text",
                "-w",
                self.config.codex_workspace,
            ]
            if self.config.codex_use_yolo:
                command.append("--yolo")
            if model:
                command.extend(["-m", model])
            if thread_id:
                command.append("--continue")
            return command

        command = [cli_command, "exec", "-C", self.config.codex_workspace]

        if self.config.codex_use_yolo:
            command.append("--yolo")

        command.extend(
            [
                "--skip-git-repo-check",
                "--add-dir",
                self.config.project_root,
                "--json",
                "-o",
                str(output_path),
            ]
        )

        for extra_path in self.config.extra_search_paths:
            if str(extra_path).strip():
                command.extend(["--add-dir", str(extra_path).strip()])

        if not self.config.codex_use_yolo:
            command.append("--dangerously-bypass-approvals-and-sandbox")

        if model:
            command.extend(["-m", model])

        if thread_id:
            command.extend(["resume", thread_id, "-"])
        else:
            command.append("-")

        return command

    def _format_resume_command(self, thread_id: str) -> str:
        if not thread_id:
            return ""

        command = [self._resolve_agent_cli_command()]
        if self.config.agent_cli_provider == "kimi":
            if self.config.codex_use_yolo:
                command.append("--yolo")
            command.extend(["--continue", "-w", self.config.codex_workspace])
            return subprocess.list2cmdline(command)

        if self.config.codex_use_yolo:
            command.append("--yolo")
        command.extend(["resume", thread_id, "-C", self.config.codex_workspace])
        return subprocess.list2cmdline(command)

    def _resolve_agent_cli_command(self) -> str:
        return self._resolve_cli_command(self.config.agent_cli_provider)

    def _resolve_cli_command(self, provider: str) -> str:
        configured = str(self._configured_command_for_provider(provider)).strip()
        if configured and (Path(configured).exists() or shutil.which(configured)):
            return configured

        candidates: list[str] = []
        if configured:
            candidates.append(configured)
        if provider == "kimi":
            candidates.extend(["kimi.exe", "kimi"])
        else:
            candidates.extend(["codex.cmd", "codex", "codex.exe"])

        seen: set[str] = set()
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            if Path(candidate).exists() or shutil.which(candidate):
                return candidate
        return configured

    def _configured_command_for_provider(self, provider: str) -> str:
        if provider == "kimi":
            return self.config.kimi_command
        return self.config.codex_command

    def _default_model_for_provider(self, provider: str) -> str | None:
        if provider == "kimi":
            return self.config.kimi_model
        return self.config.codex_model

    def _build_prompt(
        self,
        user_text: str,
        *,
        prefer_voice_reply: bool,
        source: TurnSource,
        carryover_summary: str = "",
    ) -> str:
        source_line = {
            TurnSource.VOICE: "这一轮输入来自电话语音转写。",
            TurnSource.FEISHU: "这一轮输入来自飞书私聊消息。",
            TurnSource.SCHEDULE: "这一轮输入来自定时任务触发，但这一轮仍使用共享主会话，要延续前文上下文。",
        }[source]

        memory_context = load_memory_context(self.config)
        file_block = (
            "当前工作目录的 AGENTS.md 是主要规则来源；先读它，再处理用户请求。\n"
            "如果用户要求你调整语音助手自己的行为、音色、模式或确认词，可以直接修改这些文件：\n"
            f"- 运行配置：{self.config.assistant_runtime_config_path}\n"
            f"- 长期记忆：{self.config.assistant_memory_path}\n"
            f"- 当日记忆目录：{self.config.assistant_daily_memory_dir}\n"
        )
        memory_block = self._build_memory_block(memory_context)
        patrol_block = self._build_patrol_block(user_text, source=source)
        carryover_block = self._build_carryover_block(carryover_summary)

        if prefer_voice_reply:
            return (
                "你正在电话通道中工作。\n"
                f"{source_line}\n"
                "输出会直接转成语音播报，只说最终要给用户听的话。\n"
                "不要输出思考过程、工具过程、代码、路径、命令、JSON 或 Markdown。\n"
                f"{file_block}"
                f"{memory_block}"
                f"{carryover_block}"
                "如果这轮没有必要播报给用户，请只输出 [silence]。\n"
                f"\n用户刚才说：{user_text.strip()}"
            )

        return (
            "你正在飞书私聊通道中工作。\n"
            f"{source_line}\n"
            "只输出最终要发给用户的内容，不要输出思考过程、工具过程、命令输出或中间状态。\n"
            "默认直接回答；只有在当前工作目录 AGENTS.md 明确要求结构化汇报，或你判断结构化更合适时，才输出 JSON 对象。\n"
            "如果输出 JSON，不要包 Markdown 代码块。\n"
            f"{file_block}"
            f"{memory_block}"
            f"{carryover_block}"
            f"{patrol_block}"
            "如果这轮不需要发任何消息，请只输出 [silence]。\n"
            f"用户刚才说：{user_text.strip()}"
        )

    @staticmethod
    def _build_memory_block(memory_context: dict[str, str]) -> str:
        parts: list[str] = []
        if memory_context.get("long_term"):
            parts.append(f"长期记忆：\n{memory_context['long_term']}\n")
        if memory_context.get("daily"):
            parts.append(f"今日记忆：\n{memory_context['daily']}\n")
        if not parts:
            return ""
        return "可参考这些本地记忆：\n" + "".join(parts)

    @staticmethod
    def _build_carryover_block(carryover_summary: str) -> str:
        summary = carryover_summary.strip()
        if not summary:
            return ""
        return (
            "下面是共享主会话在自动 compact 后保留下来的续接摘要。"
            "它用于延续旧会话上下文；如果和当前工作目录文件、实时采样结果或用户本轮明确要求冲突，以新的信息为准。\n"
            f"{summary}\n"
        )

    def _build_patrol_block(self, user_text: str, *, source: TurnSource) -> str:
        if not self._looks_like_patrol_request(user_text):
            return ""

        format_rule = (
            "这类请求默认输出 report_card JSON，按 session 分段汇报，不要退化成普通文本或 Markdown 表格。\n"
            "定时任务触发的巡检也沿用这个口径，agent session 动作不需要为了定时发送改成表格。\n"
            '默认 schema：{"vb_type":"report_card","title":"标题","summary":"一句话结论","facts":[{"label":"Session数","value":"3"}],"sections":[{"title":"4 | Claude | running","bullets":["当前任务：...","进展：...","卡点：无"]}],"blockers":["无"],"decisions":["无"],"next_steps":["继续观察"],"preview_text":"简短预览"}\n'
            "只有在多列对比、任务队列这类天然适合矩阵展示的数据上，才改用 table_card。\n"
        )

        return (
            "这是巡检 / 状态汇报类请求。\n"
            "查看和巡检本质上是同一类任务：都要总结 tmux session 和当前工作目录里的相关上下文；区别只是巡检可以发督促，查看不发督促。\n"
            f"{format_rule}"
            "巡检口径和汇报重点，优先遵循当前工作目录里的 AGENTS.md。\n"
            "必须先基于下面这份程序侧预采样结果作答，不要自己猜 session 数量，也不要拿旧上下文补出预采样里不存在的 session。\n"
            "如果预采样和历史上下文冲突，以预采样为准；如果信息不足，就明确写缺失项，不要脑补。\n"
            f"{self._collect_patrol_context()}"
        )

    def _collect_patrol_context(self) -> str:
        timestamp = datetime.now().isoformat(timespec="seconds")
        session_result = self._run_preflight_command(["wsl", "tmux", "list-sessions"], timeout=10)
        if session_result["returncode"] != 0:
            return (
                "程序预采样结果：\n"
                f"- 采样时间：{timestamp}\n"
                f"- tmux list-sessions 失败：{session_result['output'] or '无输出'}\n"
            )

        session_lines = [line.strip() for line in session_result["output"].splitlines() if line.strip()]
        if not session_lines:
            return (
                "程序预采样结果：\n"
                f"- 采样时间：{timestamp}\n"
                "- tmux 当前无 session\n"
            )

        pane_result = self._run_preflight_command(
            [
                "wsl",
                "tmux",
                "list-panes",
                "-a",
                "-F",
                "#{session_name}:#{window_index}.#{pane_index} pid=#{pane_pid} cmd=#{pane_current_command} title=#{pane_title}",
            ],
            timeout=10,
        )
        pane_lines = [line.strip() for line in pane_result["output"].splitlines() if line.strip()]
        pane_items = [self._parse_pane_line(line) for line in pane_lines]
        pane_items = [item for item in pane_items if item]

        first_captures: dict[str, str] = {}
        for item in pane_items:
            first_captures[item["target"]] = self._capture_pane(item["target"])

        time.sleep(3)

        second_captures: dict[str, str] = {}
        for item in pane_items:
            second_captures[item["target"]] = self._capture_pane(item["target"])

        lines = [
            "程序预采样结果：",
            f"- 采样时间：{timestamp}",
            f"- 实际 session 数：{len(session_lines)}",
        ]

        for session_line in session_lines:
            lines.append(f"- session: {session_line}")

        if pane_result["returncode"] != 0:
            lines.append(f"- list-panes 失败：{pane_result['output'] or '无输出'}")
            return "\n".join(lines) + "\n"

        if not pane_items:
            lines.append("- list-panes 返回为空，无法识别 pane")
            return "\n".join(lines) + "\n"

        for item in pane_items:
            target = item["target"]
            child_processes = self._get_child_processes(item["pane_pid"]) if item["pane_pid"] else ""
            agent_type = self._detect_agent_type(
                pane_command=item["pane_command"],
                pane_title=item["pane_title"],
                child_processes=child_processes,
            )
            snap1 = first_captures.get(target, "")
            snap2 = second_captures.get(target, "")
            status_hint = self._classify_patrol_status(agent_type=agent_type, snap1=snap1, snap2=snap2)
            delta_hint = "diff" if snap1 and snap2 and snap1 != snap2 else "same"

            lines.append(
                f"\n[{item['session_name']}] target={target} agent={agent_type} cmd={item['pane_command'] or '-'} "
                f"title={item['pane_title'] or '-'} status_hint={status_hint} snapshots={delta_hint}"
            )
            if child_processes:
                lines.append("child_processes:")
                lines.append(child_processes)
            lines.append("snapshot_last_20:")
            lines.append(snap2 or "(capture failed)")

        return "\n".join(lines) + "\n"

    @staticmethod
    def _run_preflight_command(command: list[str], *, timeout: int) -> dict[str, Any]:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=timeout,
            )
        except Exception as error:  # noqa: BLE001
            return {"returncode": -1, "output": str(error)}

        output = "\n".join(
            part.strip()
            for part in (result.stdout, result.stderr)
            if part and part.strip()
        ).strip()
        return {"returncode": result.returncode, "output": CodexRunner._clean_preflight_output(output)}

    @staticmethod
    def _clean_preflight_output(output: str) -> str:
        if not output.strip():
            return ""
        cleaned_lines: list[str] = []
        for raw_line in output.splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()
            if not stripped:
                cleaned_lines.append("")
                continue
            lowered = stripped.lower()
            if lowered.startswith("proxy set to:"):
                continue
            if "screen size is bogus" in lowered:
                continue
            cleaned_lines.append(line)
        return "\n".join(cleaned_lines).strip()

    @staticmethod
    def _parse_pane_line(raw_line: str) -> dict[str, str] | None:
        prefix, separator, suffix = raw_line.partition(" pid=")
        if not separator:
            return None

        target, _, session_name = prefix.partition(":")
        if not target or not session_name:
            return None

        pid_part, _, rest = suffix.partition(" cmd=")
        cmd_part, _, title_part = rest.partition(" title=")
        return {
            "target": prefix.strip(),
            "session_name": target.strip(),
            "pane_pid": pid_part.strip(),
            "pane_command": cmd_part.strip(),
            "pane_title": title_part.strip(),
        }

    def _capture_pane(self, target: str) -> str:
        result = self._run_preflight_command(
            ["wsl", "tmux", "capture-pane", "-t", target, "-p", "-S", "-20"],
            timeout=10,
        )
        return result["output"] if result["returncode"] == 0 else ""

    def _get_child_processes(self, pane_pid: str) -> str:
        if not pane_pid:
            return ""
        result = self._run_preflight_command(
            ["wsl", "ps", "--ppid", pane_pid, "-o", "args="],
            timeout=10,
        )
        return result["output"] if result["returncode"] == 0 else ""

    @staticmethod
    def _detect_agent_type(*, pane_command: str, pane_title: str, child_processes: str) -> str:
        command = pane_command.strip().lower()
        title = pane_title.strip()
        children = child_processes.lower()

        if command == "claude":
            return "Claude"
        if command == "node" and title.startswith("OC"):
            return "OpenCode"
        if command == "node" and "codex" in children:
            return "Codex"
        if command == "cursor":
            return "Cursor"
        if command in {"bash", "zsh", "fish"}:
            return "Shell"
        return "unknown"

    @staticmethod
    def _classify_patrol_status(*, agent_type: str, snap1: str, snap2: str) -> str:
        if not snap1 or not snap2:
            return "terminated"
        if snap1 != snap2:
            return "running"

        nonempty_lines = [line.strip() for line in snap2.splitlines() if line.strip()]
        if not nonempty_lines:
            return "idle"

        tail_10 = "\n".join(nonempty_lines[-10:])
        tail_5 = "\n".join(nonempty_lines[-5:])
        last_line = nonempty_lines[-1]

        if any(token in tail_10 for token in ("[y/n]", "(Y/n)", "[Y/n]", "Please select", "Do you want")):
            return "waiting-for-input"
        if CodexRunner._has_numbered_choices(nonempty_lines[-6:]):
            return "waiting-for-input"
        if any(token in tail_5 for token in ("Error:", "error:", "FAILED", "Traceback (most recent call last)")):
            return "errored"
        if " panic " in f" {tail_5.lower()} ":
            return "errored"

        if agent_type == "Codex":
            if "Working (" in tail_10 or "Thinking" in tail_10:
                return "running"
            if last_line.startswith("›") or "gpt-" in last_line:
                return "idle"
        elif agent_type == "Claude":
            if last_line.startswith("❯") or last_line.startswith("⏵⏵"):
                return "idle"
        elif agent_type == "OpenCode":
            if any(token in tail_10 for token in ("Thinking:", "Generating", "Queued")):
                return "running"
            if "enter submit" in tail_10:
                return "waiting-for-input"
            return "idle"
        elif agent_type == "Shell":
            if last_line.endswith(("$", "%", ">", "❯")):
                return "idle"

        return "idle"

    @staticmethod
    def _has_numbered_choices(lines: list[str]) -> bool:
        matches = 0
        for line in lines:
            stripped = line.strip()
            if stripped[:2].isdigit():
                matches += 1
                continue
            if len(stripped) > 2 and stripped[0].isdigit() and stripped[1] == ".":
                matches += 1
        return matches >= 2

    def _parse_codex_output(
        self,
        stdout_text: str,
        output_path: Path,
        stream_thread_id: str = "",
        stream_reply_text: str = "",
        allow_state_fallback: bool = True,
    ) -> tuple[str, str]:
        if self.config.agent_cli_provider == "kimi":
            return self._parse_kimi_output(
                stdout_text,
                output_path,
                stream_thread_id=stream_thread_id,
                stream_reply_text=stream_reply_text,
                allow_state_fallback=allow_state_fallback,
            )

        thread_id = stream_thread_id.strip()
        reply_text = stream_reply_text.strip()
        if output_path.exists():
            file_reply = output_path.read_text(encoding="utf-8").strip()
            if file_reply:
                reply_text = file_reply
        output_path.unlink(missing_ok=True)

        for raw_line in stdout_text.splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                item = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            if item.get("type") == "thread.started":
                thread_id = str(item.get("thread_id", "")).strip()
                continue

            if item.get("type") == "item.completed":
                payload = item.get("item") or {}
                if payload.get("type") == "agent_message":
                    text = str(payload.get("text", "")).strip()
                    if text:
                        reply_text = text

        if not thread_id and allow_state_fallback:
            previous_state = self._load_state()
            thread_id = str(previous_state.get("thread_id", "")).strip()
        if not thread_id:
            raise RuntimeError("Codex did not return a thread id")
        if not reply_text:
            raise RuntimeError("Codex did not return a final reply")
        return thread_id, reply_text

    def _parse_kimi_output(
        self,
        stdout_text: str,
        output_path: Path,
        stream_thread_id: str = "",
        stream_reply_text: str = "",
        allow_state_fallback: bool = True,
    ) -> tuple[str, str]:
        reply_text = stream_reply_text.strip() or stdout_text.strip()
        output_path.unlink(missing_ok=True)

        thread_id = stream_thread_id.strip()
        if not thread_id and allow_state_fallback:
            previous_state = self._load_state()
            thread_id = str(previous_state.get("thread_id", "")).strip()
        if not thread_id:
            thread_id = self._KIMI_SHARED_SESSION_ID
        if not reply_text:
            raise RuntimeError("Kimi did not return a final reply")
        return thread_id, reply_text

    def _load_state(self) -> dict[str, object]:
        if not self.state_path.exists():
            return {}
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            return {}
        cli_provider = str(state.get("cli_provider") or "codex").strip().lower() or "codex"
        if cli_provider != self.config.agent_cli_provider:
            return {}
        return state

    def _save_state(
        self,
        *,
        thread_id: str,
        active_model: str | None,
        requested_model: str | None,
        reply_text: str,
        raw_reply_text: str,
        input_chars: int,
        previous_state: dict[str, object],
        compact_result: CompactSessionResult | None,
    ) -> None:
        turn_count = int(previous_state.get("turn_count", 0) or 0) + 1
        estimated_history_chars = (
            int(previous_state.get("estimated_history_chars", 0) or 0) + max(0, input_chars) + len(raw_reply_text)
        )
        payload = {
            "thread_id": thread_id,
            "cli_provider": self.config.agent_cli_provider,
            "cli_command": self.config.agent_cli_command,
            "codex_workspace": self.config.codex_workspace,
            "requested_model": requested_model,
            "active_model": active_model,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "turn_count": turn_count,
            "estimated_history_chars": estimated_history_chars,
            "compact_count": int(previous_state.get("compact_count", 0) or 0),
            "last_compact_at": str(previous_state.get("last_compact_at", "")).strip(),
            "last_compact_reason": str(previous_state.get("last_compact_reason", "")).strip(),
            "last_compact_summary": str(previous_state.get("last_compact_summary", "")).strip(),
            "previous_thread_id": str(previous_state.get("previous_thread_id", "")).strip(),
            "last_reply_preview": reply_text[:120],
        }
        if compact_result is not None:
            payload["last_compact_at"] = compact_result.compacted_at
            payload["last_compact_reason"] = compact_result.reason
            payload["last_compact_summary"] = compact_result.carryover_summary
            payload["previous_thread_id"] = compact_result.previous_thread_id
        self.state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _auto_compact_if_needed(
        self, previous_state: dict[str, object]
    ) -> tuple[dict[str, object], CompactSessionResult | None]:
        reason = self._get_auto_compact_reason(previous_state)
        if reason is None:
            return previous_state, None

        previous_thread_id = str(previous_state.get("thread_id", "")).strip()
        compacted_at = datetime.now().isoformat(timespec="seconds")
        carryover_summary = ""

        if previous_thread_id:
            try:
                result = self._run_once(
                    prompt=self._build_compact_prompt(),
                    output_model=str(previous_state.get("active_model") or self.config.agent_primary_model or "").strip() or None,
                    thread_id=previous_thread_id,
                )
                _, raw_summary = self._parse_codex_output(
                    result["stdout_text"],
                    result["output_path"],
                    stream_thread_id=str(result.get("stream_thread_id", "")),
                    stream_reply_text=str(result.get("stream_reply_text", "")),
                    allow_state_fallback=True,
                )
                carryover_summary = self._sanitize_compact_summary(raw_summary)
            except Exception:
                carryover_summary = ""

        if not carryover_summary:
            carryover_summary = self._build_fallback_compact_summary(previous_state)

        next_state = {
            "compact_count": int(previous_state.get("compact_count", 0) or 0) + 1,
            "last_compact_at": compacted_at,
            "last_compact_reason": reason,
            "last_compact_summary": carryover_summary,
            "previous_thread_id": previous_thread_id,
        }
        return next_state, CompactSessionResult(
            carryover_summary=carryover_summary,
            compacted_at=compacted_at,
            reason=reason,
            previous_thread_id=previous_thread_id,
        )

    def _get_auto_compact_reason(self, previous_state: dict[str, object]) -> str | None:
        if not self.config.codex_auto_compact_enabled:
            return None
        if not str(previous_state.get("thread_id", "")).strip():
            return None

        estimated_history_chars = int(previous_state.get("estimated_history_chars", 0) or 0)
        turn_count = int(previous_state.get("turn_count", 0) or 0)
        if estimated_history_chars >= self.config.codex_auto_compact_trigger_chars:
            return f"estimated_history_chars>={self.config.codex_auto_compact_trigger_chars}"
        if turn_count >= self.config.codex_auto_compact_turn_threshold:
            return f"turn_count>={self.config.codex_auto_compact_turn_threshold}"
        return None

    def _build_compact_prompt(self) -> str:
        return (
            "你正在为共享主会话做自动 compact。"
            "你的输出不是给用户看的，而是给下一段新会话做续接上下文。\n"
            "只保留未来继续工作真正需要的信息，删掉寒暄、重复描述、完整日志、工具过程和无关细节。\n"
            f"总长度控制在 {self.config.codex_auto_compact_summary_max_chars} 个字符以内。\n"
            "输出纯文本，不要 Markdown 代码块，不要 JSON。\n"
            "按下面结构输出；没有内容的项直接写“无”：\n"
            "目标:\n"
            "已完成:\n"
            "进行中:\n"
            "关键事实:\n"
            "待办:\n"
            "风险/注意:\n"
        )

    def _sanitize_compact_summary(self, raw_text: str) -> str:
        text = raw_text.strip()
        if not text:
            return ""
        if text.startswith("```") and text.endswith("```"):
            lines = text.splitlines()
            if len(lines) >= 3:
                text = "\n".join(lines[1:-1]).strip()
        lines = [line.rstrip() for line in text.splitlines()]
        cleaned = "\n".join(line for line in lines if line.strip()).strip()
        if len(cleaned) > self.config.codex_auto_compact_summary_max_chars:
            cleaned = cleaned[: self.config.codex_auto_compact_summary_max_chars].rstrip()
        return cleaned

    def _build_fallback_compact_summary(self, previous_state: dict[str, object]) -> str:
        previous_summary = str(previous_state.get("last_compact_summary", "")).strip()
        last_reply_preview = str(previous_state.get("last_reply_preview", "")).strip()
        lines = [
            "目标:",
            "- 沿用上一共享主会话的工作目标继续执行。",
            "已完成:",
            f"- 最近一次可见回复摘要：{last_reply_preview or '无'}",
            "进行中:",
            "- 自动 compact 时未能拿到完整总结，请结合当前工作目录文件和实时信息继续。",
            "关键事实:",
            f"- 上一线程 ID：{str(previous_state.get('thread_id', '')).strip() or '无'}",
        ]
        if previous_summary:
            lines.extend(
                [
                    "待办:",
                    "- 先参考上一轮 compact 摘要，再结合本轮用户输入续接。",
                    "风险/注意:",
                    f"- 上一轮 compact 摘要：{previous_summary[:800]}",
                ]
            )
        else:
            lines.extend(
                [
                    "待办:",
                    "- 结合当前用户输入与本地文件重新建立上下文。",
                    "风险/注意:",
                    "- 自动 compact 的完整摘要生成失败，本轮上下文延续可能不完整。",
                ]
            )
        return "\n".join(lines)

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        proxy_pairs = {
            "http_proxy": self.config.codex_http_proxy,
            "HTTP_PROXY": self.config.codex_http_proxy,
            "https_proxy": self.config.codex_https_proxy,
            "HTTPS_PROXY": self.config.codex_https_proxy,
            "all_proxy": self.config.codex_all_proxy,
            "ALL_PROXY": self.config.codex_all_proxy,
            "no_proxy": self.config.codex_no_proxy,
            "NO_PROXY": self.config.codex_no_proxy,
        }
        for key, value in proxy_pairs.items():
            if value:
                env[key] = value
        if self.config.agent_cli_provider == "kimi":
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
        return env

    @staticmethod
    def _stringify_state_value(value: object) -> str:
        if value in (None, ""):
            return ""
        return str(value).strip()

    @staticmethod
    def _sanitize_prompt_for_cli(prompt: str) -> str:
        if not prompt:
            return ""
        return prompt.encode("utf-8", errors="replace").decode("utf-8")

    @staticmethod
    def _write_prompt_to_stdin(process: subprocess.Popen[bytes], prompt: str) -> None:
        if process.stdin is None:
            return

        try:
            process.stdin.write(prompt)
            process.stdin.close()
        except BrokenPipeError:
            pass
        except Exception:  # noqa: BLE001
            try:
                process.stdin.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _is_unsupported_model_error(stdout_text: str, stderr_text: str) -> bool:
        combined = f"{stdout_text}\n{stderr_text}".lower()
        return "model is not supported" in combined or "invalid_request_error" in combined

    @staticmethod
    def _read_pipe_lines(
        pipe: subprocess.PIPE | None,
        label: str,
        event_queue: "queue.Queue[tuple[str, str | None]]",
    ) -> None:
        if pipe is None:
            event_queue.put((f"{label}_done", None))
            return

        try:
            for line in pipe:
                event_queue.put((label, line))
        finally:
            try:
                pipe.close()
            except Exception:  # noqa: BLE001
                pass
            event_queue.put((f"{label}_done", None))

    @staticmethod
    def _parse_stream_event(raw_line: str) -> tuple[str, str, bool]:
        thread_id = ""
        reply_text = ""
        task_complete = False

        raw_line = raw_line.strip()
        if not raw_line:
            return thread_id, reply_text, task_complete

        try:
            item = json.loads(raw_line)
        except json.JSONDecodeError:
            return thread_id, reply_text, task_complete
        if not isinstance(item, dict):
            return thread_id, reply_text, task_complete

        item_type = str(item.get("type", "")).strip()
        if item_type == "thread.started":
            thread_id = str(item.get("thread_id", "")).strip()
            return thread_id, reply_text, task_complete

        if item_type == "item.completed":
            payload = item.get("item") or {}
            if payload.get("type") == "agent_message":
                reply_text = str(payload.get("text", "")).strip()
            return thread_id, reply_text, task_complete

        if item_type == "event_msg":
            payload = item.get("payload") or {}
            payload_type = str(payload.get("type", "")).strip()
            if payload_type == "agent_message":
                reply_text = str(payload.get("message", "")).strip()
            elif payload_type == "task_complete":
                task_complete = True
                reply_text = str(payload.get("last_agent_message", "")).strip()
            return thread_id, reply_text, task_complete

        return thread_id, reply_text, task_complete

    @staticmethod
    def _looks_like_patrol_request(user_text: str) -> bool:
        text = user_text.strip().lower()
        if not text:
            return False
        keywords = (
            "/boss",
            "boss",
            "巡检",
            "查岗",
            "状态",
            "查看",
            "agent",
            "告警",
            "阻塞",
            "日报",
            "汇总",
            "进展",
        )
        return any(keyword in text for keyword in keywords)

    @staticmethod
    def _kill_process_tree(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return

        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
        except Exception:  # noqa: BLE001
            pass

        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                pass

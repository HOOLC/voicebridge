from __future__ import annotations

import json
import os
import queue
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

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


class CodexRunner:
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
        prompt = self._build_prompt(user_text, prefer_voice_reply=prefer_voice_reply, source=source)

        try:
            result = self._run_once(
                prompt=prompt,
                output_model=self.config.codex_model,
                thread_id=state.get("thread_id") if persist_session else None,
            )
        except RuntimeError as error:
            fallback_model = self.config.codex_fallback_model
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
                reply_text=reply.preview_text,
                previous_state=state,
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
            "requested_model": str(state.get("requested_model", "")).strip(),
            "active_model": str(state.get("active_model", "")).strip(),
            "updated_at": str(state.get("updated_at", "")).strip(),
            "turn_count": str(state.get("turn_count", 0)),
            "resume_command": self._format_resume_command(thread_id),
        }

    def reset_session(self) -> None:
        self.cancel_current()
        self.state_path.unlink(missing_ok=True)

    def _run_once(self, *, prompt: str, output_model: str | None, thread_id: str | None) -> dict[str, object]:
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
                    raise RuntimeError(f"Codex exec 超时，{self.config.codex_timeout_seconds} 秒内没有完成")

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
        command = [self.config.codex_command, "exec", "-C", self.config.codex_workspace]

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

        command = ["codex"]
        if self.config.codex_use_yolo:
            command.append("--yolo")
        command.extend(["resume", thread_id, "-C", self.config.codex_workspace])
        return subprocess.list2cmdline(command)

    def _build_prompt(self, user_text: str, *, prefer_voice_reply: bool, source: TurnSource) -> str:
        source_line = {
            TurnSource.VOICE: "这一轮输入来自电话语音转写。",
            TurnSource.FEISHU: "这一轮输入来自飞书私聊消息。",
            TurnSource.SCHEDULE: "这一轮输入来自定时任务触发，但这一轮仍使用共享主会话，要延续前文上下文。",
        }[source]

        memory_context = load_memory_context(self.config)
        file_block = (
            "如果用户要求你调整语音助手自己的行为、音色、模式或确认词，你可以直接修改这些文件：\n"
            f"- 运行配置：{self.config.assistant_runtime_config_path}\n"
            f"- 长期记忆：{self.config.assistant_memory_path}\n"
            f"- 当日记忆目录：{self.config.assistant_daily_memory_dir}\n"
        )
        memory_block = self._build_memory_block(memory_context)
        patrol_block = self._build_patrol_block(user_text)

        if prefer_voice_reply:
            return (
                "你正在和电话另一头的用户沟通。\n"
                "你的输出会被直接转成语音播报，所以只说用户真正需要听到的话。\n"
                f"{source_line}\n"
                "要求：中文、简短、自然、像电话汇报；不要念代码、路径、命令、JSON、Markdown、思考过程或工具过程。\n"
                "具体的电话汇报习惯和定制化风格，遵循当前工作目录里的 AGENTS.md。\n"
                f"{file_block}"
                f"{memory_block}"
                "如果这轮没有必要播报给用户，请只输出 [silence]。\n"
                f"\n用户刚才说：{user_text.strip()}"
            )

        return (
            "你正在飞书上和用户沟通。\n"
            "你的输出会直接发送到飞书私聊，可以比电话模式更自由，但仍然只输出最终要发出去的内容。\n"
            f"{source_line}\n"
            "要求：中文、简洁、自然；不要输出思考过程、工具过程、命令输出或中间状态。\n"
            "具体的飞书输出风格和巡检汇报重点，遵循当前工作目录里的 AGENTS.md。\n"
            "短问题直接输出最终文本。\n"
            "如果是巡检、状态、日报、汇总、对比、包含多段信息、需要展示进展/卡点/待决策，优先输出一个纯 JSON 对象，不要包 Markdown 代码块。\n"
            "你可以使用三种结构化格式：\n"
            "1. 直接输出飞书原生格式："
            '{"msg_type":"text|post|interactive","content":{...},"preview_text":"简短预览"}\n'
            "2. 输出 report_card："
            '{"vb_type":"report_card","title":"标题","summary":"一句话结论","facts":[{"label":"主会话","value":"正常"}],"sections":[{"title":"Agent 进展","bullets":["alpha | Codex | 正在处理任务A | 进度 60%"]}],"blockers":["无"],"decisions":["无"],"next_steps":["继续观察"],"preview_text":"巡检：整体正常"}\n'
            "3. 输出 table_card："
            '{"vb_type":"table_card","title":"标题","summary":"一句话结论","columns":["Session","进程","当前任务","进展","卡点"],"rows":[["alpha","Codex","任务A","60%","无"]],"notes":["需要用户决策：无"],"preview_text":"巡检：整体正常"}\n'
            "结构化消息优先服务于飞书可读性：标题短、结论前置、字段少而准；简单问题不要硬上卡片。\n"
            f"{file_block}"
            f"{memory_block}"
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

    def _build_patrol_block(self, user_text: str) -> str:
        if not self._looks_like_patrol_request(user_text):
            return ""

        return "这是巡检 / 状态汇报类请求，具体的聚焦点和格式遵循当前工作目录里的 AGENTS.md。\n"

    def _parse_codex_output(
        self,
        stdout_text: str,
        output_path: Path,
        stream_thread_id: str = "",
        stream_reply_text: str = "",
        allow_state_fallback: bool = True,
    ) -> tuple[str, str]:
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

    def _load_state(self) -> dict[str, object]:
        if not self.state_path.exists():
            return {}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _save_state(
        self,
        *,
        thread_id: str,
        active_model: str | None,
        reply_text: str,
        previous_state: dict[str, object],
    ) -> None:
        turn_count = int(previous_state.get("turn_count", 0) or 0) + 1
        payload = {
            "thread_id": thread_id,
            "codex_workspace": self.config.codex_workspace,
            "requested_model": self.config.codex_model,
            "active_model": active_model,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "turn_count": turn_count,
            "last_reply_preview": reply_text[:120],
        }
        self.state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

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
        return env

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
            "巡检",
            "查岗",
            "状态",
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

from __future__ import annotations

import json
import os
import queue
import random
import re
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .audio import AudioPlayer, RecordedUtterance, VoiceCapture
from .codex_runner import CodexCancelledError, CodexRunner
from .config import BridgeConfig, ScheduledTaskConfig
from .cron_scheduler import CronTaskScheduler
from .feishu_bridge import FeishuBridge
from .interactions import BridgeTurn, FeishuMessage, OutputChannel, SessionScope, TurnSource, build_text_feishu_message
from .runtime_store import AssistantRuntimeStore
from .volcengine_audio import VolcengineAudioClient
from .workspace import RuntimeConfigManager


@dataclass(slots=True)
class QueuedTask:
    turn: BridgeTurn
    utterance: RecordedUtterance | None = None


class BridgeRuntime:
    def __init__(self, config: BridgeConfig):
        self.config_manager = RuntimeConfigManager(config)
        self.config = config
        self.audio_client = VolcengineAudioClient(config)
        self.codex = CodexRunner(config)
        self.player = AudioPlayer(config)
        self.capture = VoiceCapture(config)
        self.store = AssistantRuntimeStore(config)
        self.feishu = FeishuBridge(config, on_text=self._handle_feishu_text, log=self._log)
        self.scheduler = CronTaskScheduler(self.config_manager, on_trigger=self._handle_scheduled_task, log=self._log)
        self._queue: "queue.Queue[QueuedTask | None]" = queue.Queue()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._turn_lock = threading.Lock()
        self._latest_turn_id = 0
        self._latest_voice_turn_id = 0
        self._ack_cache: dict[tuple[str, str], bytes] = {}

    def prepare(self) -> dict[str, str]:
        self._refresh_config()
        prepared = self.audio_client.prepare_models()
        self._warm_ack_cache()
        self._sync_runtime_state()
        self._log("桥接", "语音资源已就绪")
        return prepared

    def run(self) -> None:
        self.prepare()
        if not self._worker.is_alive():
            self._worker.start()
        self.feishu.start()
        self.scheduler.start()
        self._log(
            "桥接",
            (
                "开始监听：开口判定=VAD命中 + RMS超过阈值"
                f"({self.config.vad_rms_threshold}) + 连续语音达到"
                f"{self.config.speech_start_min_voiced_ms}ms"
            ),
        )
        self.capture.run_forever(self._handle_utterance, on_speech_start=self._handle_speech_start)

    def close(self) -> None:
        self.scheduler.stop()
        self.feishu.close()
        self._queue.put(None)
        self.player.stop()
        self.codex.cancel_current()
        self.audio_client.close()
        self.store.set_codex_busy(False)
        self.store.set_queue_depth(0)
        self._sync_runtime_state()
        self._log("桥接", "运行已停止")

    def _refresh_config(self) -> None:
        try:
            new_config = self.config_manager.reload()
        except Exception as error:  # noqa: BLE001
            self.store.record_error(f"配置刷新失败：{error}")
            self._log("配置", f"刷新失败，继续沿用当前配置：{error}")
            return
        speaker_changed = new_config.volcengine_tts_speaker != self.config.volcengine_tts_speaker
        ack_changed = tuple(new_config.bridge_ack_variants) != tuple(self.config.bridge_ack_variants)
        self.config = new_config
        self.audio_client.config = new_config
        self.codex.config = new_config
        self.player.config = new_config
        self.capture.config = new_config
        self.feishu.config = new_config
        if speaker_changed or ack_changed:
            self._ack_cache.clear()
            self._warm_ack_cache()
        self._sync_runtime_state()

    def _sync_runtime_state(self) -> None:
        thread_id = self.codex.describe_state().get("thread_id", "")
        self.store.sync_runtime(self.config, shared_thread_id=thread_id)

    def _warm_ack_cache(self) -> None:
        phrases = {self.config.bridge_ack_text, *self.config.bridge_ack_variants}
        for phrase in phrases:
            text = phrase.strip()
            if not text:
                continue
            key = (self.config.volcengine_tts_speaker, text)
            if key not in self._ack_cache:
                self._ack_cache[key] = self.audio_client.synthesize(text)

    def _handle_speech_start(self) -> None:
        with self._turn_lock:
            self._latest_turn_id += 1
            turn_id = self._latest_turn_id
            self._latest_voice_turn_id = turn_id
        actions = ["停止当前播报"]
        if self.config.bridge_interrupt_playback:
            self.player.stop()
        if self.config.bridge_cancel_codex_on_interrupt:
            self.codex.cancel_current()
            actions.append("取消当前 Codex")
        self._log(
            "检测",
            (
                f"检测到开口，轮次={turn_id}；命中条件：VAD + RMS阈值 + 连续语音"
                f"{self.config.speech_start_min_voiced_ms}ms。"
                f"{'，'.join(actions)}。"
            ),
        )

    def _handle_utterance(self, utterance: RecordedUtterance) -> None:
        with self._turn_lock:
            turn_id = self._latest_voice_turn_id
        self._log(
            "采集",
            (
                f"一段话采集完成，轮次={turn_id}，时长={utterance.duration_ms}ms；"
                f"结束条件：已说话至少{self.config.min_speech_ms}ms 且静音达到"
                f"{self.config.silence_ms}ms，或总时长达到{self.config.max_utterance_ms}ms；"
                f"本次结束原因={utterance.end_reason}。"
            ),
        )
        targets = (OutputChannel.VOICE,)
        if self.feishu.enabled:
            targets += (OutputChannel.FEISHU,)
        self._queue.put(
            QueuedTask(
                turn=BridgeTurn(
                    turn_id=turn_id,
                    source=TurnSource.VOICE,
                    session_scope=SessionScope.SHARED,
                    text="",
                    output_targets=targets,
                    label="电话",
                ),
                utterance=utterance,
            )
        )
        self.store.set_queue_depth(self._queue.qsize())

    def _handle_feishu_text(self, text: str) -> None:
        clean_text = text.strip()
        if not clean_text:
            return
        if clean_text == "/new":
            self._reset_shared_session()
            self._queue.put(
                QueuedTask(
                    turn=BridgeTurn(
                        turn_id=self._next_turn_id(),
                        source=TurnSource.FEISHU,
                        session_scope=SessionScope.SHARED,
                        text="用户刚刚要求新开一个会话。请只用一句简短自然的中文确认已经开始新会话，并等待用户下一条消息。",
                        output_targets=(OutputChannel.FEISHU,),
                        label="飞书",
                    )
                )
            )
            self.store.set_queue_depth(self._queue.qsize())
            return
        if clean_text.startswith("/new "):
            self._reset_shared_session()
            clean_text = clean_text[5:].strip()
            if not clean_text:
                return
        self._log("飞书", f"转发给 Codex：{json.dumps(clean_text, ensure_ascii=False)}")
        self._queue.put(
            QueuedTask(
                turn=BridgeTurn(
                    turn_id=self._next_turn_id(),
                    source=TurnSource.FEISHU,
                    session_scope=SessionScope.SHARED,
                    text=clean_text,
                    output_targets=(OutputChannel.FEISHU,),
                    label="飞书",
                )
            )
        )
        self.store.set_queue_depth(self._queue.qsize())

    def _handle_scheduled_task(self, task: ScheduledTaskConfig) -> None:
        if task.type == "background_process":
            self._launch_background_process_task(task)
            return

        clean_prompt = task.prompt.strip()
        if not clean_prompt:
            return
        targets = (OutputChannel.FEISHU,) if self.feishu.enabled else ()
        self._queue.put(
            QueuedTask(
                turn=BridgeTurn(
                    turn_id=self._next_turn_id(),
                    source=TurnSource.SCHEDULE,
                    session_scope=SessionScope.SHARED,
                    text=clean_prompt,
                    output_targets=targets,
                    label=task.name,
                )
            )
        )
        self.store.set_queue_depth(self._queue.qsize())

    def _launch_background_process_task(self, task: ScheduledTaskConfig) -> None:
        self._refresh_config()
        try:
            command, cwd, env, stdin_text = self._build_background_process_payload(task)
            process = _spawn_background_process(command, cwd=cwd, env=env, stdin_text=stdin_text)
        except Exception as error:  # noqa: BLE001
            self.store.record_error(f"定时后台任务启动失败：{task.name}: {error}")
            self._log("定时", f"后台任务启动失败：{task.name}：{error}")
            return

        command_preview = subprocess.list2cmdline(command)
        self._log("定时", f"后台任务已启动：{task.name} pid={process.pid} cwd={cwd} cmd={command_preview}")

    def _build_background_process_payload(
        self,
        task: ScheduledTaskConfig,
    ) -> tuple[list[str], str, dict[str, str], str]:
        cwd = str(task.cwd or self.config.project_root).strip() or self.config.project_root
        cwd_path = Path(cwd)
        if not cwd_path.exists():
            raise FileNotFoundError(f"工作目录不存在：{cwd_path}")
        if not cwd_path.is_dir():
            raise NotADirectoryError(f"工作目录不是目录：{cwd_path}")

        if task.use_agent_cli:
            prompt = task.prompt.strip()
            command = self.codex.build_background_command(
                workspace=str(cwd_path),
                model=None,
                use_yolo=task.use_yolo,
                cli_provider=task.cli_provider,
            )
            return command, str(cwd_path), self.codex.build_process_env(), prompt

        executable = str(task.command or "").strip()
        if not executable:
            raise ValueError("缺少 command")
        args = [str(item) for item in task.args]
        return [executable, *args], str(cwd_path), self.codex.build_process_env(), str(task.stdin or "")

    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            self.store.set_queue_depth(self._queue.qsize())
            if item is None:
                return
            try:
                self._process_task(item)
            except Exception as error:  # noqa: BLE001
                self.store.set_codex_busy(False)
                self.store.record_error(str(error))
                self._log("桥接", f"轮次={item.turn.turn_id}，任务处理失败：{error}")

    def _process_task(self, task: QueuedTask) -> None:
        if task.turn.source is TurnSource.VOICE:
            self._process_voice_turn(task.turn, task.utterance)
            return
        self._process_text_turn(task.turn)

    def _process_voice_turn(self, turn: BridgeTurn, utterance: RecordedUtterance | None) -> None:
        if utterance is None:
            self._log("桥接", f"轮次={turn.turn_id} 缺少语音数据，已跳过")
            self.store.record_error(f"轮次={turn.turn_id} 缺少语音数据")
            return

        self._refresh_config()
        transcript = self.audio_client.transcribe(utterance.wav_bytes).strip()
        if self.config.print_transcript:
            self._log("识别", f"轮次={turn.turn_id}，识别结果：{transcript or '空文本'}")

        if transcript and turn.deliver_to_feishu:
            self._send_feishu_message(build_text_feishu_message(f"这是电话输入：{transcript}"))

        action = self._classify_transcript(transcript)
        meaningful = action not in {"ignore", "empty"}
        self.store.record_user_turn(
            turn_id=turn.turn_id,
            transcript=transcript,
            meaningful=meaningful,
            action=action,
            source=turn.source.value,
        )

        if action == "empty":
            self._log("识别", f"轮次={turn.turn_id} 为空文本，忽略")
            return
        if action == "ignore":
            self._log("过滤", f"轮次={turn.turn_id} 判定为无效内容，忽略，不转发 Codex")
            return
        if action == "repeat_last":
            self._handle_repeat_last(turn.turn_id)
            return
        if action == "stop_playback":
            self.player.stop()
            self._log("控制", f"轮次={turn.turn_id} 收到停止播报指令")
            return

        should_ack = utterance.end_reason != "max_duration"
        if should_ack:
            ack_text = self._choose_ack_text()
            ack_key = (self.config.volcengine_tts_speaker, ack_text)
            self._log("确认", f"轮次={turn.turn_id}，先短回复：{ack_text}")
            self.player.play_wav_async(self._ack_cache[ack_key])
        else:
            self._log("确认", f"轮次={turn.turn_id} 为长段续传，跳过短确认")

        turn.text = transcript
        self._process_text_turn(turn)

    def _process_text_turn(self, turn: BridgeTurn) -> None:
        self._refresh_config()
        text = turn.text.strip()
        if not text:
            return

        if turn.source is not TurnSource.VOICE:
            self.store.record_user_turn(
                turn_id=turn.turn_id,
                transcript=text,
                meaningful=True,
                action="send_to_codex",
                source=turn.source.value,
            )

        self.store.clear_error()
        self.store.set_codex_busy(True)
        try:
            self._log("Codex", f"轮次={turn.turn_id}，已发送文本到 Codex")
            result = self.codex.run(
                text,
                prefer_voice_reply=turn.prefers_voice_reply,
                source=turn.source,
                persist_session=turn.session_scope is SessionScope.SHARED,
            )
        except CodexCancelledError:
            self.store.set_codex_busy(False)
            self._log("Codex", f"轮次={turn.turn_id}，处理中途被取消")
            return
        except Exception as error:  # noqa: BLE001
            self.store.set_codex_busy(False)
            self.store.record_error(str(error))
            self._log("Codex", f"轮次={turn.turn_id}，执行失败：{error}")
            return
        finally:
            self.store.set_codex_busy(False)

        reply = result.reply
        if not reply.preview_text:
            self.store.mark_silence(turn_id=turn.turn_id, source="codex")
            self._log("回复", f"轮次={turn.turn_id}，Codex 判定无需输出")
            return

        newer_voice_turn_exists = turn.deliver_to_voice and self._has_newer_voice_turn(turn.turn_id)
        shared_thread_id = result.thread_id if turn.session_scope is SessionScope.SHARED else ""
        self.store.record_reply(
            turn_id=turn.turn_id,
            text=reply.preview_text,
            source="codex",
            spoken=turn.deliver_to_voice and not newer_voice_turn_exists,
            thread_id=shared_thread_id,
        )
        self._log("回复", f"轮次={turn.turn_id}，Codex 最终文本：{reply.preview_text}")

        if turn.deliver_to_feishu and reply.feishu_message is not None:
            self._send_feishu_message(reply.feishu_message)

        if newer_voice_turn_exists:
            self._log("丢弃", f"轮次={turn.turn_id} 的回复已过时，保存到历史但不播报")
            return

        if turn.deliver_to_voice and reply.voice_text:
            self._speak_text(turn.turn_id, reply.voice_text)

    def _handle_repeat_last(self, turn_id: int) -> None:
        reply_text = self.store.get_last_spoken_reply_text()
        if not reply_text:
            self._log("控制", f"轮次={turn_id} 要求复述，但当前没有可复述内容")
            return
        self._log("控制", f"轮次={turn_id}，复述上一条回复")
        self.store.record_reply(turn_id=turn_id, text=reply_text, source="repeat", spoken=True)
        self._send_feishu_message(build_text_feishu_message(reply_text))
        self._speak_text(turn_id, reply_text)

    def _speak_text(self, turn_id: int, text: str) -> None:
        spoken_text = text.strip()
        if not spoken_text:
            return
        if self._has_newer_voice_turn(turn_id):
            self._log("丢弃", f"轮次={turn_id} 的播报在合成前被新开口打断")
            return
        self._log("播报", f"轮次={turn_id}，开始整段播放正式回复")
        wav_bytes = self.audio_client.synthesize(spoken_text)
        if self._has_newer_voice_turn(turn_id):
            self._log("丢弃", f"轮次={turn_id} 的播报在合成后被新开口打断")
            return
        self.player.play_wav(wav_bytes)

    def _choose_ack_text(self) -> str:
        variants = [item.strip() for item in self.config.bridge_ack_variants if item.strip()]
        if not variants:
            variants = [self.config.bridge_ack_text]
        return random.choice(variants)

    def _classify_transcript(self, transcript: str) -> str:
        text = transcript.strip()
        if not text:
            return "empty"

        normalized = _normalize_text(text)
        if normalized in {_normalize_text(item) for item in self.config.bridge_repeat_aliases}:
            return "repeat_last"
        if normalized in {_normalize_text(item) for item in self.config.bridge_stop_aliases}:
            return "stop_playback"
        if not self._is_meaningful_transcript(text):
            return "ignore"
        return "send_to_codex"

    def _is_meaningful_transcript(self, text: str) -> bool:
        normalized = _normalize_text(text)
        if len(normalized) < self.config.transcript_min_chars:
            return False
        if normalized in {_normalize_text(item) for item in self.config.transcript_ignore_phrases}:
            return False
        if len(set(normalized)) == 1 and len(normalized) <= 3:
            return False
        return True

    def _has_newer_voice_turn(self, turn_id: int) -> bool:
        with self._turn_lock:
            return self._latest_voice_turn_id > turn_id

    def _next_turn_id(self) -> int:
        with self._turn_lock:
            self._latest_turn_id += 1
            return self._latest_turn_id

    def _send_feishu_message(self, message: FeishuMessage) -> None:
        if not self.feishu.enabled:
            return
        try:
            self._log("飞书", f"发送消息类型={message.msg_type}，预览={json.dumps(message.preview_text, ensure_ascii=False)}")
            self.feishu.send(message)
        except Exception as error:  # noqa: BLE001
            self.store.record_error(f"飞书发消息失败：{error}")
            self._log("飞书", f"发消息失败：{error}")

    def _reset_shared_session(self) -> None:
        self.player.stop()
        self.codex.reset_session()
        self.store.sync_runtime(self.config, shared_thread_id="")
        self._log("会话", "已重置共享会话")

    def _log(self, tag: str, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        try:
            print(f"[{timestamp}] [{tag}] {message}", flush=True)
        except OSError:
            pass


def _normalize_text(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?：:；;“”\"'（）()\-]+", "", text).lower()


def _spawn_background_process(
    command: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    stdin_text: str,
) -> subprocess.Popen[str]:
    popen_kwargs: dict[str, object] = {
        "cwd": cwd,
        "env": env,
        "stdin": subprocess.PIPE if stdin_text else subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "close_fds": True,
    }

    if os.name == "nt":
        creationflags = 0
        for flag_name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
            creationflags |= int(getattr(subprocess, flag_name, 0))
        if creationflags:
            popen_kwargs["creationflags"] = creationflags
    else:
        popen_kwargs["start_new_session"] = True

    process = subprocess.Popen(command, **popen_kwargs)
    if stdin_text and process.stdin is not None:
        try:
            process.stdin.write(stdin_text)
            process.stdin.close()
        except BrokenPipeError:
            pass
        except Exception:  # noqa: BLE001
            try:
                process.stdin.close()
            except Exception:  # noqa: BLE001
                pass
    return process

from __future__ import annotations

import queue
import random
import re
import threading
from dataclasses import dataclass
from datetime import datetime

from .audio import AudioPlayer, RecordedUtterance, VoiceCapture
from .codex_runner import CodexCancelledError, CodexRunner
from .config import BridgeConfig, load_config
from .runtime_store import AssistantRuntimeStore
from .volcengine_audio import VolcengineAudioClient


@dataclass(slots=True)
class PendingTurn:
    turn_id: int
    utterance: RecordedUtterance


class BridgeRuntime:
    def __init__(self, config: BridgeConfig):
        self.config = config
        self.audio_client = VolcengineAudioClient(config)
        self.codex = CodexRunner(config)
        self.player = AudioPlayer(config)
        self.capture = VoiceCapture(config)
        self.store = AssistantRuntimeStore(config)
        self._queue: "queue.Queue[PendingTurn | None]" = queue.Queue()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._turn_lock = threading.Lock()
        self._latest_turn_id = 0
        self._ack_cache: dict[tuple[str, str], bytes] = {}

    def prepare(self) -> dict[str, str]:
        self._reload_runtime_config()
        prepared = self.audio_client.prepare_models()
        self._warm_ack_cache()
        self.store.sync_config(self.config)
        self._log("桥接", "语音资源已就绪")
        return prepared

    def run(self) -> None:
        self.prepare()
        if not self._worker.is_alive():
            self._worker.start()
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
        self._queue.put(None)
        self.player.stop()
        self.codex.cancel_current()
        self.audio_client.close()
        self.store.set_codex_busy(False)
        self.store.set_queue_depth(0)
        self._log("桥接", "运行已停止")

    def _reload_runtime_config(self) -> None:
        new_config = load_config(self.config.config_path)
        speaker_changed = new_config.volcengine_tts_speaker != self.config.volcengine_tts_speaker
        ack_changed = tuple(new_config.bridge_ack_variants) != tuple(self.config.bridge_ack_variants)
        self.config = new_config
        self.audio_client.config = new_config
        self.codex.config = new_config
        self.player.config = new_config
        self.store.sync_config(new_config)
        if speaker_changed or ack_changed:
            self._ack_cache.clear()
            self._warm_ack_cache()

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
            turn_id = self._latest_turn_id
        self._log(
            "采集",
            (
                f"一段话采集完成，轮次={turn_id}，时长={utterance.duration_ms}ms；"
                f"结束条件：已说话至少{self.config.min_speech_ms}ms 且静音达到"
                f"{self.config.silence_ms}ms，或总时长达到{self.config.max_utterance_ms}ms。"
            ),
        )
        self._queue.put(PendingTurn(turn_id=turn_id, utterance=utterance))
        self.store.set_queue_depth(self._queue.qsize())

    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            self.store.set_queue_depth(self._queue.qsize())
            if item is None:
                return
            self._process_turn(item)

    def _process_turn(self, pending: PendingTurn) -> None:
        self._reload_runtime_config()
        transcript = self.audio_client.transcribe(pending.utterance.wav_bytes).strip()
        if self.config.print_transcript:
            self._log("识别", f"轮次={pending.turn_id}，识别结果：{transcript or '空文本'}")

        action = self._classify_transcript(transcript)
        meaningful = action not in {"ignore", "empty"}
        self.store.record_user_turn(
            turn_id=pending.turn_id,
            transcript=transcript,
            meaningful=meaningful,
            action=action,
        )

        if action == "empty":
            self._log("识别", f"轮次={pending.turn_id} 为空文本，忽略")
            return
        if action == "ignore":
            self._log("过滤", f"轮次={pending.turn_id} 判定为无效内容，忽略，不转发 Codex")
            return
        if action == "repeat_last":
            self._handle_repeat_last(pending.turn_id)
            return
        if action == "stop_playback":
            self.player.stop()
            self._log("控制", f"轮次={pending.turn_id} 收到停止播报指令")
            return

        ack_text = self._choose_ack_text()
        ack_key = (self.config.volcengine_tts_speaker, ack_text)
        self._log("确认", f"轮次={pending.turn_id}，先短回复：{ack_text}")
        self.player.play_wav_async(self._ack_cache[ack_key])

        self.store.set_codex_busy(True)
        try:
            self._log("Codex", f"轮次={pending.turn_id}，已发送文本到 Codex")
            result = self.codex.run(transcript)
        except CodexCancelledError:
            self.store.set_codex_busy(False)
            self._log("Codex", f"轮次={pending.turn_id}，处理中途被取消")
            return
        except Exception as error:  # noqa: BLE001
            self.store.set_codex_busy(False)
            self._log("Codex", f"轮次={pending.turn_id}，执行失败：{error}")
            return
        finally:
            self.store.set_codex_busy(False)

        reply_text = result.reply_text.strip()
        if not reply_text:
            self.store.mark_silence(turn_id=pending.turn_id, source="codex")
            self._log("回复", f"轮次={pending.turn_id}，Codex 判定无需播报")
            return

        newer_turn_exists = self._has_newer_turn(pending.turn_id)
        self.store.record_reply(
            turn_id=pending.turn_id,
            text=reply_text,
            source="codex",
            spoken=not newer_turn_exists,
            thread_id=result.thread_id,
        )
        self._log("回复", f"轮次={pending.turn_id}，Codex 最终文本：{reply_text}")

        if newer_turn_exists:
            self._log("丢弃", f"轮次={pending.turn_id} 的回复已过时，保存到历史但不播报")
            return

        self._speak_text(pending.turn_id, reply_text, source="codex")

    def _handle_repeat_last(self, turn_id: int) -> None:
        reply_text = self.store.get_last_reply_text()
        if not reply_text:
            self._log("控制", f"轮次={turn_id} 要求复述，但当前没有可复述内容")
            return
        self._log("控制", f"轮次={turn_id}，复述上一条回复")
        self.store.record_reply(turn_id=turn_id, text=reply_text, source="repeat", spoken=True)
        self._speak_text(turn_id, reply_text, source="repeat")

    def _speak_text(self, turn_id: int, text: str, *, source: str) -> None:
        spoken_text = text.strip()
        if not spoken_text:
            return
        if self._has_newer_turn(turn_id):
            self._log("丢弃", f"轮次={turn_id} 的播报在合成前被新开口打断")
            return
        self._log("播报", f"轮次={turn_id}，开始整段播放正式回复")
        wav_bytes = self.audio_client.synthesize(spoken_text)
        if self._has_newer_turn(turn_id):
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

    def _has_newer_turn(self, turn_id: int) -> bool:
        with self._turn_lock:
            return self._latest_turn_id > turn_id

    def _log(self, tag: str, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        try:
            print(f"[{timestamp}] [{tag}] {message}", flush=True)
        except OSError:
            pass


def _normalize_text(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?：:；;“”\"'（）()\-]+", "", text).lower()

from __future__ import annotations

import queue
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass
from io import BytesIO
from typing import Callable

import sounddevice as sd
import webrtcvad

from .config import BridgeConfig, frame_rms


@dataclass(slots=True)
class RecordedUtterance:
    wav_bytes: bytes
    duration_ms: int
    end_reason: str


class AudioDeviceResolver:
    @staticmethod
    def list_devices() -> list[dict]:
        devices = sd.query_devices()
        return [dict(item) for item in devices]

    @staticmethod
    def resolve_device(value: str | int, *, needs_input: bool = False, needs_output: bool = False) -> int:
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)

        target = str(value).strip().lower()
        devices = AudioDeviceResolver.list_devices()
        for index, device in enumerate(devices):
            name = str(device.get("name", "")).lower()
            if target not in name:
                continue
            if needs_input and int(device.get("max_input_channels", 0)) < 1:
                continue
            if needs_output and int(device.get("max_output_channels", 0)) < 1:
                continue
            return index
        raise ValueError(f"Audio device not found: {value}")


class VoiceCapture:
    def __init__(self, config: BridgeConfig):
        self.config = config
        self.vad = webrtcvad.Vad(config.vad_mode)
        self.capture_device_id = AudioDeviceResolver.resolve_device(config.capture_device, needs_input=True)
        self._frames: "queue.Queue[bytes | None]" = queue.Queue()

    def run_forever(
        self,
        on_utterance: Callable[[RecordedUtterance], None],
        *,
        on_speech_start: Callable[[], None] | None = None
    ) -> None:
        prebuffer_frames = max(1, self.config.preroll_ms // self.config.frame_ms)
        prebuffer: deque[bytes] = deque(maxlen=prebuffer_frames)

        active_frames: list[bytes] = []
        speech_active = False
        voiced_ms = 0
        silence_ms = 0
        total_ms = 0
        start_voiced_ms = 0

        def callback(indata, frames, time_info, status) -> None:  # type: ignore[no-untyped-def]
            del frames, time_info
            if status:
                return
            self._frames.put(bytes(indata))

        with sd.RawInputStream(
            samplerate=self.config.sample_rate,
            blocksize=self.config.sample_rate * self.config.frame_ms // 1000,
            device=self.capture_device_id,
            channels=self.config.channels,
            dtype="int16",
            callback=callback
        ):
            while True:
                frame = self._frames.get()
                if frame is None:
                    return

                if len(frame) != self.config.frame_bytes:
                    continue

                rms = frame_rms(frame)
                is_speech = self.vad.is_speech(frame, self.config.sample_rate) and rms >= self.config.vad_rms_threshold

                if not speech_active:
                    prebuffer.append(frame)
                    if not is_speech:
                        start_voiced_ms = 0
                        continue
                    start_voiced_ms += self.config.frame_ms
                    if start_voiced_ms < self.config.speech_start_min_voiced_ms:
                        continue
                    speech_active = True
                    if on_speech_start is not None:
                        on_speech_start()
                    active_frames = list(prebuffer)
                    voiced_ms = self.config.frame_ms
                    silence_ms = 0
                    total_ms = len(active_frames) * self.config.frame_ms
                    start_voiced_ms = 0
                    continue

                active_frames.append(frame)
                total_ms += self.config.frame_ms
                if is_speech:
                    voiced_ms += self.config.frame_ms
                    silence_ms = 0
                else:
                    silence_ms += self.config.frame_ms

                reached_silence = voiced_ms >= self.config.min_speech_ms and silence_ms >= self.config.silence_ms
                reached_max = total_ms >= self.config.max_utterance_ms
                if not reached_silence and not reached_max:
                    continue

                end_reason = "silence" if reached_silence else "max_duration"
                utterance = RecordedUtterance(
                    wav_bytes=_pcm_to_wav(
                        b"".join(active_frames),
                        sample_rate=self.config.sample_rate,
                        channels=self.config.channels
                    ),
                    duration_ms=total_ms,
                    end_reason=end_reason,
                )
                on_utterance(utterance)

                prebuffer.clear()
                active_frames = []
                speech_active = False
                voiced_ms = 0
                silence_ms = 0
                total_ms = 0
                start_voiced_ms = 0


class AudioPlayer:
    def __init__(self, config: BridgeConfig):
        self.config = config
        self.playback_device_id = AudioDeviceResolver.resolve_device(config.playback_device, needs_output=True)
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    def play_wav(self, wav_bytes: bytes) -> None:
        self._play_wav(wav_bytes, self._stop_event)

    def play_wav_async(self, wav_bytes: bytes) -> None:
        self.stop()
        stop_event = threading.Event()
        thread = threading.Thread(target=self._play_wav, args=(wav_bytes, stop_event), daemon=True)
        with self._lock:
            self._stop_event = stop_event
            self._thread = thread
        thread.start()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            stop_event = self._stop_event
        stop_event.set()
        if thread and thread.is_alive():
            thread.join(timeout=0.3)
        with self._lock:
            if self._thread is thread:
                self._thread = None

    def _play_wav(self, wav_bytes: bytes, stop_event: threading.Event) -> None:
        with wave.open(BytesIO(wav_bytes), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_rate = wav_file.getframerate()
            sample_width = wav_file.getsampwidth()
            if sample_width != 2:
                raise ValueError("Only 16-bit WAV playback is supported")

            with sd.RawOutputStream(
                samplerate=sample_rate,
                device=self.playback_device_id,
                channels=channels,
                dtype="int16"
            ) as stream:
                chunk_frames = max(1, sample_rate // 20)
                while True:
                    if stop_event.is_set():
                        break
                    frames = wav_file.readframes(chunk_frames)
                    if not frames:
                        break
                    stream.write(frames)
                    time.sleep(0.001)


def _pcm_to_wav(pcm_bytes: bytes, *, sample_rate: int, channels: int) -> bytes:
    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_bytes)
    return buffer.getvalue()

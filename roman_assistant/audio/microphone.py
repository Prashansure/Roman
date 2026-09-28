"""Toggle-based microphone capture, writing frames directly to a WAV file."""

import tempfile
import threading
import wave
from pathlib import Path
from typing import Callable

from config.settings import (
    MIC_CHANNELS,
    MIC_DEVICE_INDEX,
    MIC_FRAMES_PER_BUFFER,
    MIC_RATE,
)


class Microphone:
    def __init__(self) -> None:
        import pyaudio

        self._pyaudio = pyaudio
        self._audio = pyaudio.PyAudio()
        self._stream = None
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    def start(self, on_stopped: Callable[[Path | None, str | None], None]) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._record, args=(on_stopped,), name="roman-microphone", daemon=True
            )
            self._thread.start()

    def _record(self, on_stopped) -> None:
        path = None
        error = None
        try:
            stream = self._audio.open(
                format=self._pyaudio.paInt16,
                channels=MIC_CHANNELS,
                rate=MIC_RATE,
                input=True,
                input_device_index=MIC_DEVICE_INDEX,
                frames_per_buffer=MIC_FRAMES_PER_BUFFER,
            )
            with self._lock:
                self._stream = stream
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp:
                path = Path(temp.name)
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(MIC_CHANNELS)
                wav.setsampwidth(self._audio.get_sample_size(self._pyaudio.paInt16))
                wav.setframerate(MIC_RATE)
                while not self._stop.is_set():
                    wav.writeframes(stream.read(MIC_FRAMES_PER_BUFFER, exception_on_overflow=False))
            stream.stop_stream()
            stream.close()
            with self._lock:
                self._stream = None
            if path.stat().st_size <= 44:
                path.unlink(missing_ok=True)
                path = None
        except Exception as exc:
            error = str(exc)
            if path:
                path.unlink(missing_ok=True)
                path = None
        finally:
            on_stopped(path, error)

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        self.stop()
        with self._lock:
            stream = self._stream
        if stream is not None:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
        try:
            self._audio.terminate()
        except Exception:
            pass

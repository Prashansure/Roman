"""Toggle-based microphone capture, writing frames directly to a WAV file."""

import tempfile
import threading
import wave
from pathlib import Path
from typing import Callable

from config import settings as app_settings


def list_input_devices() -> list[tuple[int, str]]:
    """List available audio inputs for the functional voice settings panel."""
    import pyaudio

    audio = pyaudio.PyAudio()
    try:
        devices = []
        for index in range(audio.get_device_count()):
            info = audio.get_device_info_by_index(index)
            if int(info.get("maxInputChannels", 0)) > 0:
                devices.append((index, str(info.get("name", f"Input {index}"))))
        return devices
    finally:
        audio.terminate()


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
        stream = None
        try:
            stream = self._audio.open(
                format=self._pyaudio.paInt16,
                channels=app_settings.MIC_CHANNELS,
                rate=app_settings.MIC_RATE,
                input=True,
                input_device_index=app_settings.MIC_DEVICE_INDEX,
                frames_per_buffer=app_settings.MIC_FRAMES_PER_BUFFER,
            )
            with self._lock:
                self._stream = stream
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp:
                path = Path(temp.name)
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(app_settings.MIC_CHANNELS)
                wav.setsampwidth(self._audio.get_sample_size(self._pyaudio.paInt16))
                wav.setframerate(app_settings.MIC_RATE)
                while not self._stop.is_set():
                    wav.writeframes(stream.read(app_settings.MIC_FRAMES_PER_BUFFER, exception_on_overflow=False))
            if path.stat().st_size <= 44:
                path.unlink(missing_ok=True)
                path = None
        except Exception as exc:
            error = str(exc)
            if path:
                path.unlink(missing_ok=True)
                path = None
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
            with self._lock:
                self._stream = None
            try:
                self._audio.terminate()
            except Exception:
                pass
            if error and path:
                path.unlink(missing_ok=True)
                path = None
            try:
                on_stopped(path, error)
            except Exception:
                if path:
                    path.unlink(missing_ok=True)
                raise

    def stop(self) -> None:
        self._stop.set()

    def close(self, timeout: float = 1.0) -> None:
        self.stop()
        with self._lock:
            stream = self._stream
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        try:
            self._audio.terminate()
        except Exception:
            pass

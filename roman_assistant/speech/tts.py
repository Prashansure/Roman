"""Sentence-level Windows SAPI speech without interpolating text into commands."""

import os
import re
import subprocess
import threading
import time


class Speech:
    _POWERSHELL_SCRIPT = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.Speak($env:ROMAN_SPEECH_TEXT)"
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._paused = threading.Event()
        self._cancelled = threading.Event()
        self._process = None

    def pause(self) -> None:
        self._paused.set()
        with self._lock:
            process = self._process
        if process and process.poll() is None:
            self._terminate(process)

    def resume(self) -> None:
        self._paused.clear()

    def cancel(self) -> None:
        self._cancelled.set()
        self._paused.clear()
        with self._lock:
            process = self._process
        if process and process.poll() is None:
            self._terminate(process)

    @staticmethod
    def _terminate(process) -> None:
        def stop_process() -> None:
            try:
                process.terminate()
                process.wait(timeout=0.75)
            except subprocess.TimeoutExpired:
                process.kill()
            except Exception:
                pass

        threading.Thread(target=stop_process, name="roman-stop-speech", daemon=True).start()

    @staticmethod
    def _sentences(text: str) -> list[str]:
        return [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]

    def speak(self, text: str) -> None:
        self._cancelled.clear()
        for sentence in self._sentences(text):
            while self._paused.is_set() and not self._cancelled.is_set():
                time.sleep(0.1)
            if self._cancelled.is_set():
                return
            if os.name != "nt":
                continue
            env = os.environ.copy()
            env["ROMAN_SPEECH_TEXT"] = sentence
            process = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", self._POWERSHELL_SCRIPT],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            with self._lock:
                self._process = process
            while process.poll() is None:
                if self._cancelled.is_set() or self._paused.is_set():
                    process.terminate()
                    break
                self._cancelled.wait(0.05)
            with self._lock:
                if self._process is process:
                    self._process = None

    def close(self) -> None:
        self.cancel()

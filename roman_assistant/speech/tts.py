"""Sentence-level Windows SAPI speech without interpolating text into commands."""

import os
import logging
import re
import subprocess
import threading
import time

logger = logging.getLogger(__name__)


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
        self._paused_sentence = None

    def prepare(self) -> None:
        """Reset cancellation before a new assistant request begins."""
        self._cancelled.clear()
        self._paused.clear()
        self._paused_sentence = None

    @property
    def available(self) -> bool:
        return os.name == "nt"

    def pause(self) -> None:
        self._paused.set()
        with self._lock:
            process = self._process
            self._paused_sentence = getattr(self, "_current_sentence", None)
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
        sentences = self._sentences(text)
        index = 0
        while index < len(sentences):
            sentence = sentences[index]
            while self._paused.is_set() and not self._cancelled.is_set():
                time.sleep(0.1)
            if self._cancelled.is_set():
                return
            if os.name != "nt":
                index += 1
                continue
            env = os.environ.copy()
            env["ROMAN_SPEECH_TEXT"] = sentence
            with self._lock:
                self._current_sentence = sentence
            try:
                process = subprocess.Popen(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", self._POWERSHELL_SCRIPT],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception:
                logger.exception("Could not start Windows speech synthesis")
                return
            with self._lock:
                self._process = process
            while process.poll() is None and not self._cancelled.is_set() and not self._paused.is_set():
                self._cancelled.wait(0.05)
            if self._cancelled.is_set() or self._paused.is_set():
                self._terminate(process)
            with self._lock:
                if self._process is process:
                    self._process = None
                restart_sentence = self._paused_sentence == sentence
                if restart_sentence:
                    self._paused_sentence = None
            if self._cancelled.is_set():
                return
            if self._paused.is_set() or restart_sentence:
                # Resume by restarting the sentence that was cut off.
                continue
            result = process.poll()
            if result:
                logger.error("Speech synthesis exited with code %s", result)
            index += 1

    def close(self) -> None:
        self.cancel()

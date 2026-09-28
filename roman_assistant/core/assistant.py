"""Connects capture, transcription, local chat, and speech workers."""

import threading
from pathlib import Path

from audio.microphone import Microphone
from audio.whisper import Transcriber
from core.state import AssistantState, EventBus
from llm.ollama_client import OllamaClient
from speech.tts import Speech


class Assistant:
    def __init__(self) -> None:
        self.events = EventBus()
        self.microphone = None
        self.transcriber = Transcriber()
        self.llm = OllamaClient()
        self.speech = Speech()
        self._lock = threading.Lock()
        self._processing = False
        self._listening = False
        self._closed = False
        self._state = AssistantState.ONLINE

    def _state_to(self, state: AssistantState, message: str = "") -> None:
        self._state = state
        self.events.publish("state", state, message)

    def toggle_listening(self) -> None:
        if self._closed:
            return
        if self._listening:
            self.microphone.stop()
            self.events.publish("log", "Microphone stopping…")
            return
        if self._processing:
            self.events.publish("log", "ROMAN Ⅰ is still working on the current request.")
            return
        try:
            self.microphone = Microphone()
            self._listening = True
            self.speech.cancel()
            self._state_to(AssistantState.LISTENING, "Listening · press C to stop")
            self.events.publish("log", "Listening. Speak naturally, then press C to finish.")
            self.microphone.start(self._recording_stopped)
        except Exception as exc:
            self._listening = False
            self._state_to(AssistantState.ERROR, f"Microphone unavailable: {exc}")

    def _recording_stopped(self, filename: Path | None, error: str | None) -> None:
        self._listening = False
        if error:
            self._state_to(AssistantState.ERROR, f"Microphone error: {error}")
            return
        if filename is None:
            self._state_to(AssistantState.ONLINE, "No audio captured")
            return
        self._state_to(AssistantState.TRANSCRIBING, "Transcribing audio…")
        threading.Thread(
            target=self._transcribe_and_answer,
            args=(filename,),
            name="roman-transcription",
            daemon=True,
        ).start()

    def _transcribe_and_answer(self, filename: Path) -> None:
        try:
            query = self.transcriber.transcribe(str(filename))
            if self.transcriber.notice:
                self.events.publish("log", self.transcriber.notice)
                self.transcriber.notice = ""
            if not query:
                self.events.publish("log", "No clear speech detected. Try again when ready.")
                self._state_to(AssistantState.ONLINE, "Ready")
                return
            self.events.publish("message", "You", query)
            self._answer(query)
        except Exception as exc:
            self._state_to(AssistantState.ERROR, f"Transcription failed: {exc}")
        finally:
            filename.unlink(missing_ok=True)

    def send_message(self, query: str) -> None:
        query = query.strip()
        if not query:
            return
        if self._processing or self._listening:
            self.events.publish("log", "Finish the current request before sending another message.")
            return
        self.speech.cancel()
        self.events.publish("message", "You", query)
        threading.Thread(target=self._answer, args=(query,), name="roman-answer", daemon=True).start()

    def _answer(self, query: str) -> None:
        with self._lock:
            if self._processing:
                self.events.publish("log", "ROMAN Ⅰ is already handling a request.")
                return
            self._processing = True
        try:
            self._state_to(AssistantState.THINKING, "Thinking…")
            answer = self.llm.ask(query)
            self.events.publish("message", "ROMAN Ⅰ", answer)
            self._state_to(AssistantState.SPEAKING, "ROMAN Ⅰ is speaking")
            self.speech.speak(answer)
            if self._state not in (AssistantState.PAUSED, AssistantState.CANCELLED):
                self._state_to(AssistantState.ONLINE, "Ready")
        except Exception as exc:
            self._state_to(AssistantState.ERROR, f"Couldn’t reach the local model: {exc}")
        finally:
            with self._lock:
                self._processing = False

    def pause(self) -> None:
        self.speech.pause()
        self._state_to(AssistantState.PAUSED, "Speech paused · press A to resume")

    def resume(self) -> None:
        self.speech.resume()
        if self._processing:
            self._state_to(AssistantState.SPEAKING, "ROMAN Ⅰ is speaking")

    def cancel(self) -> None:
        self.speech.cancel()
        self._state_to(AssistantState.CANCELLED, "Response cancelled")
        self._state_to(AssistantState.ONLINE, "Ready")

    def shutdown(self) -> None:
        self._closed = True
        self._state_to(AssistantState.SHUTTING_DOWN, "ROMAN Ⅰ is closing")
        if self.microphone:
            self.microphone.close()
        self.speech.close()

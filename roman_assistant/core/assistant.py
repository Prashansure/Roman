"""Coordinates Roman's chat, persistence, microphone, transcription, and speech."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from audio.microphone import Microphone
from audio.whisper import Transcriber
from core.state import AssistantState, EventBus
from llm.ollama_client import OllamaClient
from memory.memory_manager import MemoryManager
from speech.tts import Speech

logger = logging.getLogger(__name__)


class Assistant:
    def __init__(
        self,
        memory: MemoryManager | None = None,
        llm: OllamaClient | None = None,
        transcriber: Transcriber | None = None,
        speech: Speech | None = None,
        microphone_factory: Callable[[], Microphone] = Microphone,
        shutdown_timeout: float = 3.0,
    ) -> None:
        self.events = EventBus()
        self.microphone: Microphone | None = None
        self.transcriber = transcriber or Transcriber()
        self.llm = llm or OllamaClient()
        self.memory = memory or MemoryManager()
        self.speech = speech or Speech()
        self._microphone_factory = microphone_factory
        self._lock = threading.RLock()
        self._processing = False
        self._listening = False
        self._closed = False
        self._cancel_requested = threading.Event()
        self._workers: set[threading.Thread] = set()
        self._shutdown_timeout = max(0.1, shutdown_timeout)
        self._state = AssistantState.ONLINE

    @property
    def state(self) -> AssistantState:
        with self._lock:
            return self._state

    def _state_to(self, state: AssistantState, message: str = "") -> None:
        with self._lock:
            self._state = state
        self.events.publish("state", state, message)

    def _start_worker(self, target, *args, name: str) -> bool:
        def run() -> None:
            try:
                target(*args)
            finally:
                with self._lock:
                    self._workers.discard(threading.current_thread())

        worker = threading.Thread(target=run, name=name, daemon=True)
        with self._lock:
            if self._closed:
                return False
            self._workers.add(worker)
            try:
                worker.start()
            except RuntimeError:
                self._workers.discard(worker)
                logger.exception("Could not start assistant worker %s", name)
                return False
        return True

    def _ensure_session(self) -> str:
        return self.memory.session_id or self.memory.start_new_conversation()

    def toggle_listening(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._listening:
                microphone = self.microphone
                start_recording = False
            elif self._processing:
                self.events.publish("log", "Roman is still working on the current request.")
                return
            else:
                self._listening = True
                self._cancel_requested.clear()
                self.speech.prepare()
                microphone = None
                start_recording = True

        if not start_recording:
            if microphone:
                microphone.stop()
                self.events.publish("log", "Microphone stopping…")
            return

        try:
            microphone = self._microphone_factory()
            with self._lock:
                if self._closed:
                    microphone.close()
                    self._listening = False
                    return
                self.microphone = microphone
            self._state_to(AssistantState.LISTENING, "Listening · press C to stop")
            self.events.publish("log", "Listening. Speak naturally, then press C to finish.")
            microphone.start(self._recording_stopped)
        except Exception as exc:
            with self._lock:
                self._listening = False
                microphone = self.microphone
            if microphone:
                microphone.close()
            message = f"Microphone unavailable: {exc}"
            logger.exception("Microphone startup failed")
            self.events.publish("log", message, True)
            self._state_to(AssistantState.ERROR, message)

    def _recording_stopped(self, filename: Path | None, error: str | None) -> None:
        with self._lock:
            self._listening = False
            cancelled = self._cancel_requested.is_set() or self._closed
            if not cancelled and filename is not None and not error:
                self._processing = True
                session_id = self._ensure_session()
            else:
                session_id = None

        if cancelled:
            if filename:
                filename.unlink(missing_ok=True)
            if not self._closed:
                self._state_to(AssistantState.ONLINE, "Ready")
            return
        if error:
            message = f"Microphone error: {error}"
            logger.error("Microphone capture failed: %s", error)
            self.events.publish("log", message, True)
            self._state_to(AssistantState.ERROR, message)
            return
        if filename is None:
            self._state_to(AssistantState.ONLINE, "No audio captured")
            return

        self._state_to(AssistantState.TRANSCRIBING, "Transcribing audio…")
        if not self._start_worker(
            self._transcribe_and_answer, filename, session_id,
            name="roman-transcription",
        ):
            filename.unlink(missing_ok=True)
            self._release_request()
            self.events.publish("log", "Could not start transcription worker.", True)

    def _transcribe_and_answer(self, filename: Path, session_id: str) -> None:
        try:
            query = self.transcriber.transcribe(str(filename))
            if self._cancel_requested.is_set() or self._closed:
                return
            if self.transcriber.notice:
                self.events.publish("log", self.transcriber.notice)
                self.transcriber.notice = ""
            if not query:
                self.events.publish("log", "No clear speech detected. Try again when ready.")
                self._state_to(AssistantState.ONLINE, "Ready")
                return
            if not self.memory.add_user_message(query, session_id):
                message = "Could not save the transcribed message to conversation history."
                self.events.publish("log", message, True)
                self._state_to(AssistantState.ERROR, message)
                return
            self.events.publish("message", "You", query)
            self._answer(query, reserved=True, session_id=session_id, user_saved=True)
        except Exception as exc:
            if not self._cancel_requested.is_set() and not self._closed:
                message = f"Transcription failed: {exc}"
                logger.exception("Transcription failed")
                self.events.publish("log", message, True)
                self._state_to(AssistantState.ERROR, message)
        finally:
            filename.unlink(missing_ok=True)
            if self.state == AssistantState.TRANSCRIBING:
                self._release_request()

    def send_message(self, query: str) -> bool:
        query = query.strip()
        if not query:
            return False
        with self._lock:
            if self._closed or self._processing or self._listening:
                accepted = False
                session_id = None
            else:
                self._processing = True
                self._cancel_requested.clear()
                self.speech.prepare()
                session_id = self._ensure_session()
                accepted = self.memory.add_user_message(query, session_id)
                if not accepted:
                    self._processing = False

        if not accepted:
            if session_id is None:
                self.events.publish("log", "Finish the current request before sending another message.")
            else:
                message = "Could not save your message to conversation history; it was not sent."
                self.events.publish("log", message, True)
                self._state_to(AssistantState.ERROR, message)
            return False

        self.events.publish("message", "You", query)
        if not self._start_worker(
            self._answer, query, True, session_id, True, name="roman-answer"
        ):
            self._release_request()
            self.events.publish("log", "Could not start the assistant response worker.", True)
        return True

    def _answer(
        self,
        query: str,
        reserved: bool = False,
        session_id: str | None = None,
        user_saved: bool = False,
    ) -> None:
        if not reserved:
            with self._lock:
                if self._closed or self._processing or self._listening:
                    self.events.publish("log", "Roman is already handling a request.")
                    return
                self._processing = True
                self._cancel_requested.clear()
                self.speech.prepare()

        generation_started = False
        generation_ended = False
        parts: list[str] = []
        try:
            session_id = session_id or self._ensure_session()
            if self._cancel_requested.is_set() or self._closed:
                return
            if not user_saved:
                if not self.memory.add_user_message(query, session_id):
                    message = "Could not save your message to conversation history; it was not sent."
                    self.events.publish("log", message, True)
                    self._state_to(AssistantState.ERROR, message)
                    return
                self.events.publish("message", "You", query)

            self._state_to(AssistantState.THINKING, "Thinking…")
            self.events.publish("generation_start")
            generation_started = True
            history = list(self.memory.recent_history)
            if history and history[-1] == {"role": "user", "content": query}:
                history.pop()
            for token in self.llm.stream(
                query,
                history=history,
                memory=self.memory.memory_for_prompt(),
                cancel_event=self._cancel_requested,
            ):
                if self._cancel_requested.is_set() or self._closed:
                    break
                parts.append(token)
                self.events.publish("generation_token", token)

            answer = "".join(parts).strip()
            if self._cancel_requested.is_set() or self._closed:
                self.events.publish("generation_end", answer, False)
                generation_ended = True
                return
            if not answer:
                self.events.publish("generation_end", "", False)
                generation_ended = True
                message = "The local model returned an empty response. Your message is saved; try again."
                self.events.publish("log", message, True)
                self._state_to(AssistantState.ERROR, message)
                return

            self.events.publish("generation_end", answer, True)
            generation_ended = True
            if self.memory.add_assistant_message(answer, session_id):
                self.events.publish("conversation_saved")
            else:
                self.events.publish("log", "Response generated but could not be saved to history.", True)

            try:
                extraction = self.llm.extract_memories(query, answer)
                self.memory.update_memories(extraction)
            except Exception:
                if not self._cancel_requested.is_set() and not self._closed:
                    logger.exception("Memory extraction failed")
                    self.events.publish("log", "Memory update failed; the conversation remains saved.")

            if self._cancel_requested.is_set() or self._closed:
                return
            self._state_to(AssistantState.SPEAKING, "Roman is speaking")
            self.speech.speak(answer)
        except Exception as exc:
            if generation_started and not generation_ended:
                self.events.publish("generation_end", "".join(parts), False)
            if self._cancel_requested.is_set() or self._closed:
                return
            message = f"Couldn’t reach the local model: {exc}"
            logger.exception("Assistant response failed")
            self.events.publish("log", message, True)
            self._state_to(AssistantState.ERROR, message)
        finally:
            self._release_request()

    def _release_request(self) -> None:
        with self._lock:
            self._processing = False
            closed = self._closed
        if not closed and self.state not in (AssistantState.ERROR, AssistantState.ONLINE):
            self._state_to(AssistantState.ONLINE, "Ready")

    def pause(self) -> None:
        with self._lock:
            if not self._processing or self._state != AssistantState.SPEAKING or self._closed:
                return
        self.speech.pause()
        self._state_to(AssistantState.PAUSED, "Speech paused · press A to resume")

    def resume(self) -> None:
        with self._lock:
            if self._state != AssistantState.PAUSED or self._closed:
                return
        self.speech.resume()
        self._state_to(AssistantState.SPEAKING, "Roman is speaking")

    def cancel(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._cancel_requested.set()
            microphone = self.microphone if self._listening else None
            active = self._processing or self._listening
        self.llm.cancel()
        self.speech.cancel()
        if microphone:
            microphone.stop()
        self._state_to(AssistantState.CANCELLED, "Response cancelled")
        if not active:
            self._state_to(AssistantState.ONLINE, "Ready")

    def new_conversation(self) -> str:
        with self._lock:
            active = self._processing or self._listening
        if active:
            self.cancel()
        session_id = self.memory.start_new_conversation()
        self.events.publish("conversation_reset")
        return session_id

    def load_conversation(self, session_id: str):
        with self._lock:
            active = self._processing or self._listening
        if active:
            self.cancel()
        return self.memory.load_conversation(session_id)

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._cancel_requested.set()
            microphone = self.microphone
            workers = tuple(self._workers)
        self._state_to(AssistantState.SHUTTING_DOWN, "Roman is closing")

        deadline = time.monotonic() + self._shutdown_timeout
        try:
            self.llm.cancel()
        except Exception:
            logger.exception("Failed to cancel the Ollama request during shutdown")
        try:
            self.speech.close()
        except Exception:
            logger.exception("Failed to stop speech during shutdown")
        if microphone:
            try:
                microphone.close(timeout=max(0.0, deadline - time.monotonic()))
            except TypeError:
                microphone.close()
            except Exception:
                logger.exception("Failed to close the microphone during shutdown")

        current = threading.current_thread()
        for worker in workers:
            if worker is current:
                continue
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        alive = [worker.name for worker in workers if worker.is_alive()]
        if alive:
            logger.warning("Workers still stopping after shutdown timeout: %s", ", ".join(alive))
        try:
            self.llm.close()
        except Exception:
            logger.exception("Failed to release the Ollama client during shutdown")

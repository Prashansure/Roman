import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from core.assistant import Assistant
from memory.backend_adapter import MemoryBackendAdapter, MemoryBackendError
from speech.tts import Speech


class FakeLLM:
    def __init__(self, response: str = "local answer") -> None:
        self.response = response
        self.calls: list[dict] = []
        self.cancelled = False
        self.closed = False

    def stream(self, query, history=None, memory="", cancel_event=None):
        self.calls.append({"query": query, "history": list(history or []), "memory": memory})
        if cancel_event and cancel_event.is_set():
            return
        yield self.response

    def extract_memories(self, *_args):
        return {"should_remember": False, "memories": []}

    def cancel(self):
        self.cancelled = True

    def close(self):
        self.closed = True


class FakeSpeech:
    def __init__(self) -> None:
        self.spoken: list[str] = []

    def prepare(self):
        pass

    def speak(self, text):
        self.spoken.append(text)

    def cancel(self):
        pass

    def close(self):
        pass


class FakeTranscriber:
    notice = ""

    def transcribe(self, *_args):
        return ""


class AssistantIntegrationTests(unittest.TestCase):
    @staticmethod
    def _wait(assistant: Assistant) -> None:
        for worker in tuple(assistant._workers):
            worker.join(timeout=3)

    @staticmethod
    def _hash(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_json_default_assistant_uses_bounded_adapter_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "memory"
            root.mkdir()
            (root / "memory.json").write_text(
                json.dumps({"user": {}, "projects": [{"key": "project", "value": "Roman assistant"}]}),
                encoding="utf-8",
            )
            llm = FakeLLM()
            assistant = Assistant(
                llm=llm,
                speech=FakeSpeech(),
                transcriber=FakeTranscriber(),
                memory_root=root,
            )
            try:
                self.assertIsInstance(assistant.memory, MemoryBackendAdapter)
                self.assertEqual(assistant.memory.backend_name, "json")
                self.assertTrue(assistant.send_message("Tell me about the project"))
                self._wait(assistant)
                self.assertEqual(len(llm.calls), 1)
                self.assertIn("Roman assistant", llm.calls[0]["memory"])
                self.assertLessEqual(len(llm.calls[0]["history"]), 12)
                saved = assistant.memory.list_conversations()[0]["messages"]
                self.assertEqual([row["role"] for row in saved], ["user", "assistant"])
            finally:
                assistant.shutdown()

    def test_commands_are_local_and_ordinary_messages_still_use_llm(self):
        with tempfile.TemporaryDirectory() as tmp:
            llm = FakeLLM()
            assistant = Assistant(
                llm=llm,
                speech=FakeSpeech(),
                transcriber=FakeTranscriber(),
                memory_root=Path(tmp) / "memory",
            )
            try:
                self.assertTrue(assistant.send_message('/remember category=user key=name value="Prashan"'))
                self.assertEqual(llm.calls, [])
                item = next(item for item in assistant.memory.list_memory_items() if item.key == "name")
                self.assertTrue(assistant.send_message("/forget key=name"))
                self.assertGreaterEqual(len(assistant.memory.find_memory_candidates("name")), 2)
                self.assertTrue(assistant.send_message(f"/forget id={item.id}"))
                self.assertNotIn(item.id, {candidate.id for candidate in assistant.memory.find_memory_candidates("name")})
                self.assertTrue(assistant.send_message("ordinary message"))
                self._wait(assistant)
                self.assertEqual(len(llm.calls), 1)
            finally:
                assistant.shutdown()

    def test_sqlite_is_explicit_opt_in_and_invalid_selection_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "memory.sqlite3"
            llm = FakeLLM()
            assistant = Assistant(
                llm=llm,
                speech=FakeSpeech(),
                transcriber=FakeTranscriber(),
                memory_backend="sqlite",
                sqlite_path=db,
            )
            try:
                self.assertEqual(assistant.memory.backend_name, "sqlite")
                self.assertTrue(db.exists())
            finally:
                assistant.shutdown()

            invalid_target = root / "not-a-database"
            invalid_target.mkdir()
            with self.assertRaises(MemoryBackendError) as raised:
                Assistant(
                    llm=FakeLLM(),
                    speech=FakeSpeech(),
                    transcriber=FakeTranscriber(),
                    memory_backend="sqlite",
                    sqlite_path=invalid_target,
                    memory_root=root / "json-fallback-must-not-exist",
                )
            self.assertIn("SQLite memory backend is unavailable", str(raised.exception))
            self.assertFalse((root / "json-fallback-must-not-exist" / "memory.json").exists())

    def test_integration_does_not_write_real_user_memory(self):
        project_root = Path(__file__).parents[1]
        memory_path = project_root / "memory" / "memory.json"
        conversation_paths = sorted((project_root / "memory" / "conversations").glob("*.json"))
        before = {path: self._hash(path) for path in [memory_path, *conversation_paths]}
        with tempfile.TemporaryDirectory() as tmp:
            assistant = Assistant(
                llm=FakeLLM(),
                speech=FakeSpeech(),
                transcriber=FakeTranscriber(),
                memory_root=Path(tmp) / "isolated",
            )
            try:
                self.assertTrue(assistant.send_message('/remember category=user key=isolated value=yes'))
            finally:
                assistant.shutdown()
        self.assertEqual(before, {path: self._hash(path) for path in before})


if __name__ == "__main__":
    unittest.main()

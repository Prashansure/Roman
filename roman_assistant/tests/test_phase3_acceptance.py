import json
import tempfile
import unittest
from pathlib import Path

from config import settings
from core.assistant import Assistant
from memory.backend_adapter import MemoryBackendAdapter
from memory.retrieval import RetrievalLimits


class AcceptanceLLM:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.closed = False

    def stream(self, query, history=None, memory="", cancel_event=None):
        self.calls.append({"query": query, "history": list(history or []), "memory": memory})
        if cancel_event and cancel_event.is_set():
            return
        yield "accepted response"

    def extract_memories(self, *_args):
        return {"should_remember": False, "memories": []}

    def cancel(self):
        pass

    def close(self):
        self.closed = True


class AcceptanceSpeech:
    def prepare(self):
        pass

    def speak(self, _text):
        pass

    def cancel(self):
        pass

    def close(self):
        pass


class AcceptanceTranscriber:
    notice = ""


class Phase3AcceptanceTests(unittest.TestCase):
    @staticmethod
    def _assistant(root: Path, llm: AcceptanceLLM, memory=None) -> Assistant:
        return Assistant(
            memory=memory,
            llm=llm,
            speech=AcceptanceSpeech(),
            transcriber=AcceptanceTranscriber(),
            memory_root=root,
        )

    @staticmethod
    def _wait(assistant: Assistant) -> None:
        for worker in tuple(assistant._workers):
            worker.join(timeout=3)

    @staticmethod
    def _events(assistant: Assistant) -> list[tuple]:
        events = []
        queue = assistant.events.events
        while not queue.empty():
            events.append(queue.get_nowait())
        return events

    def test_command_lifecycle_ambiguity_and_restart_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "isolated-memory"
            llm = AcceptanceLLM()
            assistant = self._assistant(root, llm)
            try:
                self.assertTrue(assistant.send_message('/remember category=user key=acceptance value="saved fact"'))
                listed = assistant.send_message("/memory list")
                self.assertTrue(listed)
                list_events = self._events(assistant)
                self.assertTrue(any("acceptance" in event[1] for event in list_events if event[0] == "generation_end"))

                self.assertTrue(assistant.send_message('/remember category=user key=target value="one"'))
                self.assertTrue(assistant.send_message('/remember category=projects key=target value="two"'))
                before = assistant.memory.find_memory_candidates("target")
                self.assertEqual(len(before), 2)
                self.assertTrue(assistant.send_message("/forget key=target"))
                ambiguous_events = self._events(assistant)
                self.assertTrue(any("exact id" in event[1] for event in ambiguous_events if event[0] == "generation_end"))
                self.assertEqual({item.id for item in before}, {item.id for item in assistant.memory.find_memory_candidates("target")})

                selected = next(item for item in before if item.category == "user")
                self.assertTrue(assistant.send_message(f"/forget id={selected.id}"))
                self.assertNotIn(selected.id, {item.id for item in assistant.memory.find_memory_candidates("target")})
                self.assertEqual(len(assistant.memory.find_memory_candidates("target")), 1)

                self.assertTrue(assistant.send_message("ordinary acceptance message"))
                self._wait(assistant)
                self.assertEqual(len(llm.calls), 1)
            finally:
                assistant.shutdown()

            restarted_llm = AcceptanceLLM()
            restarted = self._assistant(root, restarted_llm)
            try:
                items = restarted.memory.list_memory_items()
                self.assertTrue(any(item.category == "projects" and item.key == "target" and item.value == "two" for item in items))
                self.assertFalse(any(item.category == "user" and item.key == "target" for item in items))
                self.assertTrue(restarted.send_message("/memory list"))
                restart_events = self._events(restarted)
                self.assertTrue(any("projects.target" in event[1] for event in restart_events if event[0] == "generation_end"))
            finally:
                restarted.shutdown()

    def test_retrieval_is_relevant_and_within_configured_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "isolated-memory"
            root.mkdir()
            (root / "memory.json").write_text(
                json.dumps(
                    {
                        "user": {},
                        "projects": [
                            {"key": f"project-{index}", "value": "unrelated background text"}
                            for index in range(10)
                        ]
                        + [{"key": "needle", "value": "relevant acceptance fact"}],
                    }
                ),
                encoding="utf-8",
            )
            limits = RetrievalLimits(
                max_facts=2,
                max_messages=1,
                max_recent_messages=2,
                max_fact_chars=40,
                max_message_chars=40,
                max_memory_context_chars=120,
                max_total_prompt_chars=220,
            )
            memory = MemoryBackendAdapter(root, limits=limits)
            llm = AcceptanceLLM()
            assistant = self._assistant(root, llm, memory=memory)
            try:
                self.assertTrue(assistant.send_message("Explain the needle"))
                self._wait(assistant)
                self.assertEqual(len(llm.calls), 1)
                self.assertIn("relevant acceptance fact", llm.calls[0]["memory"])
                self.assertNotIn("unrelated background text", llm.calls[0]["memory"])
                self.assertLessEqual(len(llm.calls[0]["memory"]), limits.max_memory_context_chars)
                self.assertLessEqual(len(llm.calls[0]["history"]), limits.max_recent_messages)
            finally:
                assistant.shutdown()

    def test_json_default_and_no_accidental_sqlite_activation(self):
        self.assertEqual(settings.MEMORY_BACKEND, "json")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "isolated-memory"
            llm = AcceptanceLLM()
            assistant = self._assistant(root, llm)
            try:
                self.assertEqual(assistant.memory.backend_name, "json")
                self.assertFalse((root / "roman_memory.sqlite3").exists())
                self.assertFalse((Path(tmp) / "memory.sqlite3").exists())
            finally:
                assistant.shutdown()


if __name__ == "__main__":
    unittest.main()

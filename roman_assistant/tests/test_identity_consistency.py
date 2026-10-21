import json
import re
import tempfile
import unittest
from pathlib import Path

from config.settings import APP_NAME
from core.assistant import Assistant
from llm.ollama_client import SYSTEM_PROMPT
from memory.identity import extract_explicit_user_name, is_user_identity_question


class IdentityLLM:
    """Fake model that answers only from the prepared identity context."""

    def __init__(self):
        self.calls = []

    def stream(self, query, history=None, memory="", cancel_event=None):
        call = {"query": query, "history": list(history or []), "memory": memory}
        self.calls.append(call)
        lowered = query.casefold()
        if "your name" in lowered:
            answer = APP_NAME
        elif is_user_identity_question(query):
            match = re.search(r"user name is ([^.\n]+)", memory, re.IGNORECASE)
            answer = match.group(1) if match else "I don't know your name yet."
        elif extract_explicit_user_name(query):
            answer = "I'll remember that."
        else:
            answer = "Unrelated answer."
        if not (cancel_event and cancel_event.is_set()):
            yield answer

    def extract_memories(self, *_args):
        # Deliberately false identity candidates must be ignored. The unrelated
        # project fact remains eligible for normal memory extraction.
        return {
            "should_remember": True,
            "memories": [
                {"category": "user", "key": "name", "value": APP_NAME},
                {"category": "assistant", "key": "name", "value": "A different name"},
                {"category": "projects", "key": "active", "value": "Roman assistant"},
            ],
        }

    def cancel(self):
        pass

    close = cancel


class FakeSpeech:
    def prepare(self):
        pass

    def speak(self, _text):
        pass

    def cancel(self):
        pass

    def close(self):
        pass


class FakeTranscriber:
    notice = ""


class IdentityConsistencyTests(unittest.TestCase):
    @staticmethod
    def _assistant(root: Path, llm: IdentityLLM) -> Assistant:
        return Assistant(
            llm=llm,
            speech=FakeSpeech(),
            transcriber=FakeTranscriber(),
            memory_root=root,
        )

    @staticmethod
    def _wait(assistant: Assistant) -> None:
        for worker in tuple(assistant._workers):
            worker.join(timeout=3)
            if worker.is_alive():
                raise AssertionError("assistant worker did not finish")

    @staticmethod
    def _responses(assistant: Assistant) -> list[str]:
        responses = []
        while not assistant.events.events.empty():
            event = assistant.events.events.get_nowait()
            if event[0] == "generation_end" and event[2]:
                responses.append(event[1])
        return responses

    def test_identity_stays_separate_persists_and_accepts_explicit_correction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "memory"
            root.mkdir()
            (root / "memory.json").write_text(
                json.dumps({"user": {"name": "Old Name"}, "assistant": {"name": APP_NAME}}),
                encoding="utf-8",
            )
            llm = IdentityLLM()
            assistant = self._assistant(root, llm)
            try:
                self.assertTrue(assistant.send_message("What is your name?"))
                self._wait(assistant)
                self.assertEqual(self._responses(assistant), [f"I'm {APP_NAME}."])
                self.assertTrue(assistant.send_message("My name is Prashan."))
                self._wait(assistant)
                self.assertEqual(self._responses(assistant), ["Your name is Prashan."])
                self.assertEqual(assistant.memory.memory["user"]["name"], "Prashan")

                self.assertTrue(assistant.send_message("What is my name?"))
                self._wait(assistant)
                self.assertEqual(self._responses(assistant), ["Your name is Prashan."])
                prepared = assistant.memory.prepare_prompt(
                    "What is my name?",
                    history=[
                        {"role": "user", "content": "My name is Prashan."},
                        {"role": "assistant", "content": "Your name is Roman."},
                    ],
                )
                self.assertIn("assistant name is Roman; user name is Prashan", prepared.memory_context)
                self.assertTrue(all(row["role"] == "user" for row in prepared.history))
                retrieved = assistant.memory.retrieve_relevant("What is my name?")
                self.assertEqual([(fact.category, fact.property_name, fact.fact_text) for fact in retrieved.facts],
                                 [("user", "name", "Prashan")])

                self.assertTrue(assistant.send_message("Tell me about the Roman project."))
                self._wait(assistant)
                self.assertEqual(self._responses(assistant), ["Unrelated answer."])
                saved = json.loads((root / "memory.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["user"]["name"], "Prashan")
                self.assertEqual(saved["assistant"]["name"], APP_NAME)
                self.assertEqual(saved["projects"][0], {"key": "active", "value": "Roman assistant"})
            finally:
                assistant.shutdown()

            later_llm = IdentityLLM()
            later = self._assistant(root, later_llm)
            try:
                self.assertTrue(later.send_message("What is my name?"))
                self._wait(later)
                self.assertEqual(self._responses(later), ["Your name is Prashan."])

                self.assertTrue(later.send_message("Actually, my name is Priya."))
                self._wait(later)
                self.assertEqual(self._responses(later), ["Your name is Priya."])
                self.assertEqual(later.memory.memory["user"]["name"], "Priya")
                self.assertTrue(later.send_message("What is my name?"))
                self._wait(later)
                self.assertEqual(self._responses(later), ["Your name is Priya."])
                self.assertEqual(later.memory.memory["assistant"]["name"], APP_NAME)
            finally:
                later.shutdown()

    def test_unknown_identity_is_explicit_and_non_identity_memory_still_works(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "memory"
            llm = IdentityLLM()
            assistant = self._assistant(root, llm)
            try:
                self.assertTrue(assistant.send_message("What is my name?"))
                self._wait(assistant)
                self.assertEqual(self._responses(assistant), ["I don't know your name yet."])
                self.assertEqual(assistant.memory.memory["user"].get("name"), None)
                self.assertTrue(assistant.send_message("Tell me something unrelated."))
                IdentityConsistencyTests._wait(assistant)
                self.assertTrue(any(item.category == "projects" for item in assistant.memory.list_memory_items()))
            finally:
                assistant.shutdown()

    def test_system_prompt_and_statement_parser_have_clear_identity_rules(self):
        self.assertIn(f"Your name is {APP_NAME}", SYSTEM_PROMPT)
        self.assertIn("never present it as the user's name", SYSTEM_PROMPT)
        self.assertEqual(extract_explicit_user_name("My name is Prashan."), "Prashan")
        self.assertEqual(extract_explicit_user_name("Actually, my name is Priya."), "Priya")
        self.assertEqual(extract_explicit_user_name("My name is not Roman; it is Prashan."), "Prashan")
        self.assertIsNone(extract_explicit_user_name("What is my name?"))
        self.assertFalse(is_user_identity_question("What is your name?"))


if __name__ == "__main__":
    unittest.main()

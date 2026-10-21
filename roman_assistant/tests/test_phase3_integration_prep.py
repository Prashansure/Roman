import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from config import settings
from memory.backend_adapter import MemoryBackendAdapter
from memory.commands import MemoryCommandProcessor
from memory.retrieval import RetrievalLimits
from memory.shadow_compare import compare_retrieval


class IntegrationPreparationTests(unittest.TestCase):
    @staticmethod
    def _json_root(tmp: str) -> Path:
        root = Path(tmp) / "json-memory"
        root.mkdir()
        (root / "memory.json").write_text(
            json.dumps(
                {
                    "user": {"name": "Prashan", "editor": "VS Code"},
                    "assistant": {"name": "Roman"},
                    "projects": [{"key": "project", "value": "Roman assistant"}],
                }
            ),
            encoding="utf-8",
        )
        return root

    def test_json_default_and_interface_compatibility(self):
        self.assertEqual(settings.MEMORY_BACKEND, "json")
        with tempfile.TemporaryDirectory() as tmp:
            root = self._json_root(tmp)
            with MemoryBackendAdapter(root) as adapter:
                self.assertEqual(adapter.backend_name, "json")
                self.assertIsNone(adapter.session_id)
                session = adapter.start_new_conversation()
                self.assertTrue(adapter.add_user_message("hello", session))
                self.assertTrue(adapter.add_assistant_message("hi", session))
                self.assertEqual(len(adapter.load_conversation(session)), 2)
                self.assertEqual(adapter.list_conversations()[0]["id"], session)
            self.assertFalse((Path(tmp) / "roman_memory.sqlite3").exists())

    def test_relevant_retrieval_and_prompt_budgets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._json_root(tmp)
            limits = RetrievalLimits(
                max_facts=2,
                max_messages=2,
                max_recent_messages=3,
                max_fact_chars=40,
                max_message_chars=50,
                max_memory_context_chars=120,
                max_total_prompt_chars=260,
            )
            with MemoryBackendAdapter(root, limits=limits) as adapter:
                for index in range(8):
                    adapter.add_user_message(f"project context {index}")
                result = adapter.retrieve_relevant("project", recent_history=adapter.recent_history)
                self.assertLessEqual(len(result.facts), 2)
                self.assertLessEqual(len(result.messages), 2)
                self.assertLessEqual(len(result.context), 120)
                prepared = adapter.prepare_prompt("project", adapter.recent_history)
                self.assertLessEqual(len(prepared.history), 3)
                estimated = len(prepared.memory_context) + len("project")
                estimated += sum(len(row["role"]) + len(row["content"]) + 16 for row in prepared.history)
                self.assertLessEqual(estimated, 260)

    def test_cancellation_and_backend_errors_are_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            with MemoryBackendAdapter(backend_name="sqlite", sqlite_path=Path(tmp) / "test.sqlite3") as adapter:
                cancelled = threading.Event()
                cancelled.set()
                self.assertTrue(adapter.retrieve_relevant("anything", cancel_event=cancelled).cancelled)
                with patch.object(adapter._sqlite, "search", side_effect=RuntimeError("database unavailable")):
                    result = adapter.retrieve_relevant("anything")
                self.assertEqual(result.facts, ())
                self.assertIn("database unavailable", result.error or "")
                self.assertEqual(adapter.memory_for_prompt("anything"), "")

    def test_explicit_remember_list_and_forget_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            with MemoryBackendAdapter(self._json_root(tmp)) as adapter:
                commands = MemoryCommandProcessor(adapter)
                self.assertFalse(commands.handle("please forget my name").handled)
                saved = commands.handle('/remember category=user key=nickname value="The Builder"')
                self.assertTrue(saved.changed)
                duplicate = commands.handle('/remember category=user key=nickname value="The Builder"')
                self.assertEqual(duplicate.kind, "duplicate")
                conflict = commands.handle('/remember category=user key=nickname value="Another name"')
                self.assertEqual(conflict.kind, "conflict")
                self.assertEqual(len(adapter.find_memory_candidates("nickname")), 1)
                listed = commands.handle("/memory list")
                self.assertIn(saved.response.split()[-1].rstrip("."), listed.response)
                ambiguous = commands.handle("/forget key=nickname")
                self.assertTrue(ambiguous.requires_confirmation)
                self.assertEqual(len(adapter.find_memory_candidates("nickname")), 1)
                memory_id = saved.response.split()[-1].rstrip(".")
                forgotten = commands.handle(f"/forget id={memory_id}")
                self.assertTrue(forgotten.changed)
                self.assertEqual(adapter.find_memory_candidates("nickname"), [])

    def test_shadow_comparison_is_read_only_and_canonical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._json_root(tmp)
            source_bytes = (root / "memory.json").read_bytes()
            with MemoryBackendAdapter(root) as json_adapter, MemoryBackendAdapter(
                backend_name="sqlite", sqlite_path=Path(tmp) / "synthetic.sqlite3"
            ) as sqlite_adapter:
                sqlite_adapter.remember_fact("user", "name", "Prashan", source="synthetic")
                sqlite_adapter.remember_fact("assistant", "name", "Roman", source="synthetic")
                sqlite_adapter.remember_fact("projects", "project", "Roman assistant", source="synthetic")
                comparison = compare_retrieval(json_adapter, sqlite_adapter, "Roman project")
                self.assertTrue(comparison.common)
                self.assertEqual(comparison.json_only, ())
                self.assertEqual(comparison.sqlite_only, ())
            self.assertEqual((root / "memory.json").read_bytes(), source_bytes)
            self.assertFalse((Path(tmp) / "roman_memory.sqlite3").exists())

    def test_ui_fix_file_is_not_modified_by_prototype_operations(self):
        ui_path = Path(__file__).parents[1] / "ui" / "main_window.py"
        before = ui_path.read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            with MemoryBackendAdapter(self._json_root(tmp)) as adapter:
                adapter.remember_fact("user", "test", "temporary")
                adapter.retrieve_relevant("temporary")
        self.assertEqual(ui_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()

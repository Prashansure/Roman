import json
import tempfile
import threading
import unittest
from pathlib import Path

from memory.migration_validator import MigrationValidator
from memory.migrate_json import JsonMigrationCommand, MigrationValidationError
from memory.sqlite_backend import MigrationIntegrityError, SQLiteMemoryBackend


class SQLiteMemoryBackendTests(unittest.TestCase):
    def test_schema_crud_duplicates_conflicts_and_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "memory.sqlite3"
            with SQLiteMemoryBackend(db) as backend:
                inserted = backend.add_fact("projects", "project", "Roman assistant", "user")
                self.assertEqual(inserted.action, "inserted")
                self.assertEqual(backend.add_fact("projects", "PROJECT", "roman   assistant", "user").action, "duplicate")

                ambiguous = backend.add_fact("projects", "project", "SQLite memory backend", "user")
                self.assertEqual(ambiguous.action, "conflict_preserved")
                self.assertEqual(ambiguous.conflict_id, inserted.fact_id)
                self.assertTrue(backend.get_fact(inserted.fact_id).active)
                self.assertEqual(len(backend.list_facts()), 2)
                corrected = backend.add_fact(
                    "projects",
                    "project",
                    "SQLite memory backend",
                    "explicit correction",
                    explicit_correction=True,
                    correction_reason="User explicitly corrected the project description",
                )
                self.assertEqual(corrected.action, "duplicate")
                corrected = backend.add_fact(
                    "projects",
                    "project",
                    "SQLite and FTS5 memory backend",
                    "explicit correction",
                    explicit_correction=True,
                    correction_reason="User explicitly corrected the project description",
                )
                self.assertEqual(corrected.action, "superseded")
                self.assertEqual(len(backend.list_facts()), 1)
                self.assertEqual(len(backend.list_facts(active_only=False)), 3)

                backend.add_fact("preferences", "editor", "VS Code", "explicit user statement")
                results = backend.search("code editor")
                self.assertEqual([row.fact_text for row in results], ["VS Code"])
                self.assertEqual(backend.search("code", category="projects"), [])

    def test_invalid_fact_is_rejected_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            with SQLiteMemoryBackend(Path(tmp) / "memory.sqlite3") as backend:
                with self.assertRaises(ValueError):
                    backend.add_fact("", "key", "value", "test")
                self.assertEqual(backend.list_facts(), [])

    def test_transaction_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            with SQLiteMemoryBackend(Path(tmp) / "memory.sqlite3") as backend:
                with self.assertRaises(RuntimeError):
                    with backend.transaction() as connection:
                        connection.execute(
                            """
                            INSERT INTO memory_facts(
                                id, category, subject, property_name, fact_text,
                                normalized_subject, normalized_property, normalized_text,
                                created_at, updated_at, source
                            ) VALUES ('rollback', 'test', 'test', 'key', 'value', 'test', 'key', 'value', 'now', 'now', 'test')
                            """
                        )
                        raise RuntimeError("force rollback")
                self.assertIsNone(backend.get_fact("rollback"))

    def test_concurrent_connections_preserve_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "memory.sqlite3"
            SQLiteMemoryBackend(db).close()
            errors = []

            def add(index: int) -> None:
                try:
                    with SQLiteMemoryBackend(db) as backend:
                        backend.add_fact("facts", f"key-{index}", f"value-{index}", "test")
                except Exception as exc:  # pragma: no cover - diagnostic path
                    errors.append(exc)

            threads = [threading.Thread(target=add, args=(index,)) for index in range(20)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertEqual(errors, [])
            with SQLiteMemoryBackend(db) as backend:
                self.assertEqual(len(backend.list_facts()), 20)

    def test_conversation_crud_and_message_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            with SQLiteMemoryBackend(Path(tmp) / "memory.sqlite3") as backend:
                backend.add_message("session-1", "user", "I am debugging SQLite", "json:2026-10-15#0")
                backend.add_message("session-1", "assistant", "SQLite uses transactions", "json:2026-10-15#1")
                backend.add_message("session-2", "user", "I am cooking pasta", "json:2026-10-15#2")
                self.assertEqual(len(backend.list_messages("session-1")), 2)
                results = backend.search_messages("SQLite transaction")
                self.assertEqual({row.session_id for row in results}, {"session-1"})
                self.assertEqual(backend.search_messages("pasta", session_id="session-1"), [])


class MigrationValidatorTests(unittest.TestCase):
    def test_validator_is_read_only_and_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory_path = root / "memory.json"
            conversations = root / "conversations"
            conversations.mkdir()
            memory_path.write_text(
                json.dumps(
                    {
                        "user": {"name": "Prashan"},
                        "projects": [
                            {"key": "project", "value": "Roman"},
                            {"key": "project", "value": "SQLite"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            conversation_path = conversations / "2026-10-15.json"
            conversation_path.write_text(
                json.dumps(
                    [
                        {"role": "user", "content": "Remember this", "session_id": "s1", "timestamp": "2026-10-15T00:00:00Z"},
                        {"role": "invalid", "content": "skip"},
                    ]
                ),
                encoding="utf-8",
            )
            before_memory = memory_path.read_bytes()
            before_conversation = conversation_path.read_bytes()

            first = MigrationValidator.validate(memory_path, conversations)
            second = MigrationValidator.validate(memory_path, conversations)

            self.assertEqual(first.proposed_facts, second.proposed_facts)
            self.assertEqual(first.proposed_messages, second.proposed_messages)
            self.assertEqual(len(first.proposed_facts), 3)
            self.assertEqual(len(first.proposed_messages), 1)
            self.assertEqual(first.conflict_group_count, 1)
            self.assertEqual(first.invalid_message_count, 1)
            self.assertTrue(any("no per-fact timestamps" in notice for notice in first.notices))
            self.assertEqual(memory_path.read_bytes(), before_memory)
            self.assertEqual(conversation_path.read_bytes(), before_conversation)

    def test_malformed_json_is_reported_without_database_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory_path = root / "memory.json"
            conversations = root / "conversations"
            conversations.mkdir()
            memory_path.write_text("{bad", encoding="utf-8")
            report = MigrationValidator.validate(memory_path, conversations)
            self.assertFalse(report.valid)
            self.assertTrue(any("memory file could not be read" in issue for issue in report.issues))
            self.assertEqual(list(root.glob("*.sqlite3")), [])


class MigrationCommandTests(unittest.TestCase):
    @staticmethod
    def _sources(root: Path) -> tuple[Path, Path]:
        memory_path = root / "memory.json"
        conversations = root / "conversations"
        conversations.mkdir()
        memory_path.write_text(
            json.dumps({"user": {"name": "Prashan"}, "projects": [{"key": "project", "value": "Roman"}]}),
            encoding="utf-8",
        )
        (conversations / "2026-10-15.json").write_text(
            json.dumps([
                {"role": "user", "content": "I am building Roman", "session_id": "s1"},
                {"role": "assistant", "content": "That is a durable project", "session_id": "s1"},
            ]),
            encoding="utf-8",
        )
        return memory_path, conversations

    def test_dry_run_is_non_mutating_and_apply_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory_path, conversations = self._sources(root)
            database = root / "target.sqlite3"
            before = {
                path: path.read_bytes()
                for path in [memory_path, *conversations.glob("*.json")]
            }
            command = JsonMigrationCommand(memory_path, conversations, database)
            dry_run = command.run(dry_run=True)
            self.assertTrue(dry_run.dry_run)
            self.assertFalse(database.exists())
            applied = command.run(dry_run=False)
            repeated = command.run(dry_run=False)
            self.assertEqual(applied.stats["facts_inserted"], 2)
            self.assertEqual(applied.stats["messages_inserted"], 2)
            self.assertEqual(repeated.stats["facts_skipped"], 2)
            self.assertEqual(repeated.stats["messages_skipped"], 2)
            self.assertEqual(
                before,
                {path: path.read_bytes() for path in before},
            )
            with SQLiteMemoryBackend(database) as backend:
                facts = backend.list_facts()
                messages = backend.list_messages("s1")
                self.assertEqual(len(facts), 2)
                self.assertTrue(all(fact.created_at is None for fact in facts))
                self.assertTrue(all(fact.updated_at is None for fact in facts))
                self.assertTrue(all(fact.imported_at for fact in facts))
                self.assertEqual(len(messages), 2)
                self.assertTrue(all(message.timestamp is None for message in messages))
                self.assertTrue(all(message.imported_at for message in messages))
                self.assertEqual(len(backend.search("Roman")), 1)
                self.assertEqual(len(backend.search_messages("building")), 1)

    def test_rollback_on_stable_id_collision_and_safe_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory_path, conversations = self._sources(root)
            database = root / "target.sqlite3"
            command = JsonMigrationCommand(memory_path, conversations, database)
            report = command.plan()
            second = report.proposed_facts[1]
            with SQLiteMemoryBackend(database) as backend:
                backend.add_fact(
                    second.category,
                    second.property_name,
                    "different content",
                    "preexisting",
                    fact_id=second.id,
                    subject=second.subject,
                )
            with self.assertRaises(MigrationIntegrityError):
                command.run(dry_run=False)
            with SQLiteMemoryBackend(database) as backend:
                self.assertEqual(len(backend.list_facts()), 1)
                self.assertEqual(backend.list_facts()[0].fact_text, "different content")

            database.unlink()
            result = command.run(dry_run=False)
            self.assertEqual(result.stats["facts_inserted"], 2)

    def test_invalid_source_is_rejected_before_target_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory_path = root / "memory.json"
            conversations = root / "conversations"
            conversations.mkdir()
            memory_path.write_text("{bad", encoding="utf-8")
            database = root / "target.sqlite3"
            with self.assertRaises(MigrationValidationError):
                JsonMigrationCommand(memory_path, conversations, database).run(dry_run=False)
            self.assertFalse(database.exists())

    def test_migration_preserves_ambiguous_legacy_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory_path, conversations = self._sources(root)
            memory_path.write_text(
                json.dumps({
                    "projects": [
                        {"key": "project", "value": "Roman"},
                        {"key": "project", "value": "A different project"},
                    ]
                }),
                encoding="utf-8",
            )
            database = root / "target.sqlite3"
            result = JsonMigrationCommand(memory_path, conversations, database).run(dry_run=False)
            self.assertEqual(result.report.conflict_group_count, 1)
            with SQLiteMemoryBackend(database) as backend:
                active = backend.list_facts()
                self.assertEqual(len(active), 2)
                self.assertEqual(len({fact.conflict_group_id for fact in active}), 1)
                self.assertTrue(all(fact.supersedes_id is None for fact in active))


if __name__ == "__main__":
    unittest.main()

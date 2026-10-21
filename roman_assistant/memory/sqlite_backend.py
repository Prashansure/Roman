"""Isolated SQLite/FTS5 prototype for Roman's structured memory.

This module is intentionally not wired into :mod:`core.assistant`.  It provides
the transactional primitives needed for a later migration while leaving the
current JSON backend as the live implementation.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 2
_TOKEN_RE = re.compile(r"[\w]+", flags=re.UNICODE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class FactRecord:
    id: str
    category: str
    subject: str
    property_name: str
    fact_text: str
    created_at: str | None
    updated_at: str | None
    imported_at: str | None
    source: str
    source_session_id: str | None = None
    source_message_id: str | None = None
    active: bool = True
    conflict_group_id: str | None = None
    supersedes_id: str | None = None
    rank: float | None = None

    @property
    def fact_key(self) -> str:
        """Compatibility alias for the pre-policy prototype."""

        return self.property_name


@dataclass(frozen=True)
class FactWriteResult:
    action: str
    fact_id: str
    conflict_id: str | None = None
    conflict_group_id: str | None = None
    superseded_ids: tuple[str, ...] = ()


class MigrationIntegrityError(RuntimeError):
    """Raised when an import cannot prove that its target is consistent."""


@dataclass(frozen=True)
class MessageRecord:
    id: str
    session_id: str
    ordinal: int
    timestamp: str | None
    role: str
    content: str
    source: str
    imported_at: str | None = None
    rank: float | None = None


class SQLiteMemoryBackend:
    """Small transactional fact store with keyword retrieval through FTS5."""

    def __init__(self, db_path: Path | str, timeout: float = 5.0) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.db_path,
            timeout=timeout,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = NORMAL")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_facts (
                    id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    property_name TEXT NOT NULL,
                    fact_text TEXT NOT NULL,
                    normalized_subject TEXT NOT NULL,
                    normalized_property TEXT NOT NULL,
                    normalized_text TEXT NOT NULL,
                    created_at TEXT,
                    updated_at TEXT,
                    imported_at TEXT,
                    source TEXT NOT NULL,
                    source_session_id TEXT,
                    source_message_id TEXT,
                    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                    conflict_group_id TEXT,
                    supersedes_id TEXT REFERENCES memory_facts(id)
                );

                CREATE INDEX IF NOT EXISTS ix_memory_facts_active_category
                    ON memory_facts(active, category);

                CREATE INDEX IF NOT EXISTS ix_memory_facts_subject_property
                    ON memory_facts(category, normalized_subject, normalized_property, active);

                CREATE TABLE IF NOT EXISTS fact_relations (
                    old_fact_id TEXT NOT NULL REFERENCES memory_facts(id),
                    new_fact_id TEXT NOT NULL REFERENCES memory_facts(id),
                    relation TEXT NOT NULL CHECK (relation = 'supersedes'),
                    PRIMARY KEY (old_fact_id, new_fact_id, relation)
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS memory_facts_fts USING fts5(
                    fact_id UNINDEXED,
                    category,
                    fact_key,
                    fact_text
                );

                CREATE TABLE IF NOT EXISTS conversation_sessions (
                    id TEXT PRIMARY KEY,
                    started_at TEXT,
                    updated_at TEXT NOT NULL,
                    source TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversation_messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES conversation_sessions(id),
                    ordinal INTEGER NOT NULL,
                    timestamp TEXT,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    imported_at TEXT,
                    UNIQUE(session_id, ordinal)
                );

                CREATE INDEX IF NOT EXISTS ix_conversation_messages_session
                    ON conversation_messages(session_id, ordinal);

                CREATE VIRTUAL TABLE IF NOT EXISTS conversation_messages_fts USING fts5(
                    message_id UNINDEXED,
                    session_id UNINDEXED,
                    role,
                    content
                );

                CREATE TABLE IF NOT EXISTS migration_sources (
                    source_path TEXT PRIMARY KEY,
                    sha256 TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    fact_count INTEGER NOT NULL,
                    message_count INTEGER NOT NULL
                );
                """
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a caller-supplied operation as one rollback-safe transaction."""

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    @staticmethod
    def _validate_fact_fields(
        category: str,
        subject: str,
        property_name: str,
        fact_text: str,
        source: str,
    ) -> None:
        values = {
            "category": category,
            "subject": subject,
            "property_name": property_name,
            "fact_text": fact_text,
            "source": source,
        }
        if any(not isinstance(value, str) or not value.strip() for value in values.values()):
            raise ValueError("fact identity, text, and source fields must be non-empty strings")
        if len(category) > 100 or len(subject) > 200 or len(property_name) > 200 or len(fact_text) > 4000:
            raise ValueError("fact fields exceed the prototype size limits")

    @staticmethod
    def _row_to_fact(row: sqlite3.Row) -> FactRecord:
        return FactRecord(
            id=row["id"],
            category=row["category"],
            subject=row["subject"],
            property_name=row["property_name"],
            fact_text=row["fact_text"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            imported_at=row["imported_at"],
            source=row["source"],
            source_session_id=row["source_session_id"],
            source_message_id=row["source_message_id"],
            active=bool(row["active"]),
            conflict_group_id=row["conflict_group_id"],
            supersedes_id=row["supersedes_id"],
            rank=row["rank"] if "rank" in row.keys() else None,
        )

    def add_fact(
        self,
        category: str,
        fact_key: str,
        fact_text: str,
        source: str,
        *,
        subject: str | None = None,
        property_name: str | None = None,
        source_session_id: str | None = None,
        source_message_id: str | None = None,
        created_at: str | None = None,
        updated_at: str | None = None,
        imported_at: str | None = None,
        fact_id: str | None = None,
        explicit_correction: bool = False,
        correction_reason: str | None = None,
    ) -> FactWriteResult:
        """Insert a fact, preserving ambiguous conflicts by default.

        The legacy ``fact_key`` argument remains accepted as the property name.
        Similarity search is never used to decide writes. Supersession requires
        an explicit correction and a non-empty reason.
        """

        subject = subject or category
        property_name = property_name or fact_key
        self._validate_fact_fields(category, subject, property_name, fact_text, source)
        category = category.strip()
        subject = subject.strip()
        property_name = property_name.strip()
        fact_text = fact_text.strip()
        source = source.strip()
        normalized_subject = _normalize(subject)
        normalized_property = _normalize(property_name)
        normalized_text = _normalize(fact_text)
        now = _now()
        created_at = created_at.strip() if isinstance(created_at, str) and created_at.strip() else created_at
        updated_at = updated_at.strip() if isinstance(updated_at, str) and updated_at.strip() else updated_at
        imported_at = imported_at.strip() if isinstance(imported_at, str) and imported_at.strip() else imported_at
        if explicit_correction and not isinstance(correction_reason, str) or explicit_correction and not correction_reason.strip():
            raise ValueError("explicit corrections require a non-empty correction_reason")

        with self.transaction() as connection:
            if fact_id:
                existing_id = connection.execute(
                    "SELECT * FROM memory_facts WHERE id = ?", (fact_id,)
                ).fetchone()
                if existing_id:
                    if (
                        existing_id["category"] != category
                        or existing_id["normalized_subject"] != normalized_subject
                        or existing_id["normalized_property"] != normalized_property
                        or existing_id["normalized_text"] != normalized_text
                    ):
                        raise ValueError(f"stable fact ID already exists with different content: {fact_id}")
                    return FactWriteResult("already_imported", fact_id, existing_id["id"], existing_id["conflict_group_id"])

            existing_rows = connection.execute(
                """
                SELECT * FROM memory_facts
                WHERE active = 1 AND category = ? AND normalized_subject = ?
                  AND normalized_property = ?
                """,
                (category, normalized_subject, normalized_property),
            ).fetchall()
            duplicate = next(
                (row for row in existing_rows if row["normalized_text"] == normalized_text),
                None,
            )
            if duplicate:
                return FactWriteResult("duplicate", duplicate["id"], duplicate["id"], duplicate["conflict_group_id"])

            conflict_group_id = next(
                (row["conflict_group_id"] for row in existing_rows if row["conflict_group_id"]),
                None,
            )
            conflict_id = existing_rows[0]["id"] if existing_rows else None
            superseded_ids: tuple[str, ...] = ()
            if existing_rows and explicit_correction:
                conflict_group_id = conflict_group_id or str(uuid.uuid4())
                superseded_ids = tuple(row["id"] for row in existing_rows)
                connection.execute(
                    """
                    UPDATE memory_facts
                    SET active = 0, updated_at = ?, conflict_group_id = ?
                    WHERE id IN ({})
                    """.format(",".join("?" for _ in superseded_ids)),
                    (now, conflict_group_id, *superseded_ids),
                )
            elif existing_rows:
                conflict_group_id = conflict_group_id or str(uuid.uuid4())
                connection.execute(
                    """
                    UPDATE memory_facts SET conflict_group_id = ?
                    WHERE id IN ({})
                    """.format(",".join("?" for _ in existing_rows)),
                    (conflict_group_id, *(row["id"] for row in existing_rows)),
                )

            fact_id = fact_id or str(uuid.uuid4())
            effective_created_at = created_at if created_at is not None else (None if imported_at else now)
            effective_updated_at = updated_at if updated_at is not None else (None if imported_at else now)

            connection.execute(
                """
                INSERT INTO memory_facts(
                    id, category, subject, property_name, fact_text,
                    normalized_subject, normalized_property, normalized_text,
                    created_at, updated_at, imported_at, source,
                    source_session_id, source_message_id, active,
                    conflict_group_id, supersedes_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    fact_id,
                    category,
                    subject,
                    property_name,
                    fact_text,
                    normalized_subject,
                    normalized_property,
                    normalized_text,
                    effective_created_at,
                    effective_updated_at,
                    imported_at,
                    source,
                    source_session_id,
                    source_message_id,
                    conflict_group_id,
                    superseded_ids[0] if superseded_ids else None,
                ),
            )
            connection.execute(
                "INSERT INTO memory_facts_fts(fact_id, category, fact_key, fact_text) VALUES (?, ?, ?, ?)",
                (fact_id, category, property_name, fact_text),
            )
            for old_id in superseded_ids:
                connection.execute(
                    "INSERT INTO fact_relations(old_fact_id, new_fact_id, relation) VALUES (?, ?, 'supersedes')",
                    (old_id, fact_id),
                )
            action = "superseded" if superseded_ids else "conflict_preserved" if existing_rows else "inserted"
            return FactWriteResult(action, fact_id, conflict_id, conflict_group_id, superseded_ids)

    def get_fact(self, fact_id: str) -> FactRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM memory_facts WHERE id = ?", (fact_id,)
            ).fetchone()
        return self._row_to_fact(row) if row else None

    def _import_fact_locked(self, connection: sqlite3.Connection, record: Any, imported_at: str) -> str:
        category = str(record.category)
        subject = str(getattr(record, "subject", category))
        property_name = str(getattr(record, "property_name", record.fact_key))
        fact_text = str(record.fact_text)
        source = str(record.source)
        normalized_subject = _normalize(subject)
        normalized_property = _normalize(property_name)
        normalized_text = _normalize(fact_text)
        existing_id = connection.execute(
            "SELECT * FROM memory_facts WHERE id = ?", (record.id,)
        ).fetchone()
        if existing_id:
            if (
                existing_id["category"] != category
                or existing_id["normalized_subject"] != normalized_subject
                or existing_id["normalized_property"] != normalized_property
                or existing_id["normalized_text"] != normalized_text
                or existing_id["source"] != source
            ):
                raise MigrationIntegrityError(f"stable fact ID collision: {record.id}")
            return "already_imported"

        existing_rows = connection.execute(
            """
            SELECT * FROM memory_facts
            WHERE active = 1 AND category = ? AND normalized_subject = ?
              AND normalized_property = ?
            """,
            (category, normalized_subject, normalized_property),
        ).fetchall()
        duplicate = next(
            (row for row in existing_rows if row["normalized_text"] == normalized_text),
            None,
        )
        if duplicate:
            return "duplicate"
        conflict_group_id = next(
            (row["conflict_group_id"] for row in existing_rows if row["conflict_group_id"]),
            None,
        )
        if existing_rows and conflict_group_id is None:
            conflict_group_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"conflict:{category}:{normalized_subject}:{normalized_property}"))
            connection.execute(
                """
                UPDATE memory_facts SET conflict_group_id = ?
                WHERE id IN ({})
                """.format(",".join("?" for _ in existing_rows)),
                (conflict_group_id, *(row["id"] for row in existing_rows)),
            )
        connection.execute(
            """
            INSERT INTO memory_facts(
                id, category, subject, property_name, fact_text,
                normalized_subject, normalized_property, normalized_text,
                created_at, updated_at, imported_at, source,
                source_session_id, source_message_id, active,
                conflict_group_id, supersedes_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, NULL)
            """,
            (
                record.id,
                category,
                subject,
                property_name,
                fact_text,
                normalized_subject,
                normalized_property,
                normalized_text,
                record.created_at,
                record.updated_at,
                imported_at,
                source,
                None,
                None,
                conflict_group_id,
            ),
        )
        connection.execute(
            "INSERT INTO memory_facts_fts(fact_id, category, fact_key, fact_text) VALUES (?, ?, ?, ?)",
            (record.id, category, property_name, fact_text),
        )
        return "conflict_preserved" if existing_rows else "inserted"

    def _import_message_locked(self, connection: sqlite3.Connection, record: Any, imported_at: str) -> str:
        existing_id = connection.execute(
            "SELECT * FROM conversation_messages WHERE id = ?", (record.id,)
        ).fetchone()
        if existing_id:
            if (
                existing_id["session_id"] != record.session_id
                or existing_id["role"] != record.role
                or existing_id["content"] != record.content
                or existing_id["timestamp"] != record.timestamp
                or existing_id["source"] != record.source
            ):
                raise MigrationIntegrityError(f"stable message ID collision: {record.id}")
            return "already_imported"
        session_id = str(record.session_id)
        connection.execute(
            """
            INSERT INTO conversation_sessions(id, started_at, updated_at, source)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                started_at = COALESCE(conversation_sessions.started_at, excluded.started_at),
                updated_at = excluded.updated_at
            """,
            (session_id, record.timestamp, imported_at, record.source),
        )
        ordinal = connection.execute(
            "SELECT COALESCE(MAX(ordinal), -1) + 1 FROM conversation_messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO conversation_messages(
                id, session_id, ordinal, timestamp, role, content, source, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                session_id,
                ordinal,
                record.timestamp,
                record.role,
                record.content,
                record.source,
                imported_at,
            ),
        )
        connection.execute(
            "INSERT INTO conversation_messages_fts(message_id, session_id, role, content) VALUES (?, ?, ?, ?)",
            (record.id, session_id, record.role, record.content),
        )
        return "inserted"

    def import_records(
        self,
        facts: list[Any] | tuple[Any, ...],
        messages: list[Any] | tuple[Any, ...],
        source_hashes: dict[str, str],
        source_counts: dict[str, tuple[int, int]],
        *,
        imported_at: str | None = None,
        source_paths: dict[str, Path] | None = None,
    ) -> dict[str, int]:
        """Atomically import deterministic records and verify FTS/index counts."""

        imported_at = imported_at or _now()
        fact_actions: list[str] = []
        message_actions: list[str] = []
        with self.transaction() as connection:
            for record in facts:
                fact_actions.append(self._import_fact_locked(connection, record, imported_at))
            for record in messages:
                message_actions.append(self._import_message_locked(connection, record, imported_at))
            for source_path, digest in sorted(source_hashes.items()):
                fact_count, message_count = source_counts.get(source_path, (0, 0))
                prior = connection.execute(
                    "SELECT sha256, fact_count, message_count FROM migration_sources WHERE source_path = ?",
                    (source_path,),
                ).fetchone()
                if prior and (
                    prior["sha256"] != digest
                    or prior["fact_count"] != fact_count
                    or prior["message_count"] != message_count
                ):
                    raise MigrationIntegrityError(f"source manifest mismatch: {source_path}")
                if prior is None:
                    connection.execute(
                        """
                        INSERT INTO migration_sources(
                            source_path, sha256, imported_at, fact_count, message_count
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (source_path, digest, imported_at, fact_count, message_count),
                    )

            if source_paths:
                for source_path, expected_hash in sorted(source_hashes.items()):
                    path = source_paths.get(source_path)
                    if path is None or _sha256(path) != expected_hash:
                        raise MigrationIntegrityError(f"source changed during import: {source_path}")

            fact_ids = [record.id for record in facts]
            message_ids = [record.id for record in messages]
            if fact_ids:
                fact_placeholders = ",".join("?" for _ in fact_ids)
                fact_count = connection.execute(
                    f"SELECT COUNT(*) FROM memory_facts WHERE id IN ({fact_placeholders})",
                    fact_ids,
                ).fetchone()[0]
                fts_count = connection.execute(
                    f"SELECT COUNT(*) FROM memory_facts_fts WHERE fact_id IN ({fact_placeholders})",
                    fact_ids,
                ).fetchone()[0]
                if fact_count != len(fact_ids) or fts_count != len(fact_ids):
                    raise MigrationIntegrityError("fact rows and FTS rows failed integrity checks")
            if message_ids:
                message_placeholders = ",".join("?" for _ in message_ids)
                message_count = connection.execute(
                    f"SELECT COUNT(*) FROM conversation_messages WHERE id IN ({message_placeholders})",
                    message_ids,
                ).fetchone()[0]
                fts_count = connection.execute(
                    f"SELECT COUNT(*) FROM conversation_messages_fts WHERE message_id IN ({message_placeholders})",
                    message_ids,
                ).fetchone()[0]
                if message_count != len(message_ids) or fts_count != len(message_ids):
                    raise MigrationIntegrityError("message rows and FTS rows failed integrity checks")
        return {
            "facts_inserted": fact_actions.count("inserted") + fact_actions.count("conflict_preserved"),
            "facts_skipped": fact_actions.count("already_imported") + fact_actions.count("duplicate"),
            "messages_inserted": message_actions.count("inserted"),
            "messages_skipped": message_actions.count("already_imported"),
        }

    def list_facts(self, *, active_only: bool = True) -> list[FactRecord]:
        query = "SELECT * FROM memory_facts"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY category, normalized_subject, normalized_property, created_at"
        with self._lock:
            rows = self._connection.execute(query).fetchall()
        return [self._row_to_fact(row) for row in rows]

    def delete_fact(self, fact_id: str) -> bool:
        now = _now()
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE memory_facts SET active = 0, updated_at = ? WHERE id = ? AND active = 1",
                (now, fact_id),
            )
            return cursor.rowcount == 1

    def search(
        self, query: str, *, category: str | None = None, limit: int = 5
    ) -> list[FactRecord]:
        if not isinstance(query, str) or not query.strip():
            return []
        if limit < 1:
            return []
        tokens = _TOKEN_RE.findall(query.casefold())
        if not tokens:
            return []
        match_query = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
        sql = """
            SELECT f.*, bm25(memory_facts_fts) AS rank
            FROM memory_facts_fts
            JOIN memory_facts AS f ON f.id = memory_facts_fts.fact_id
            WHERE memory_facts_fts MATCH ? AND f.active = 1
        """
        params: list[Any] = [match_query]
        if category is not None:
            sql += " AND f.category = ?"
            params.append(category)
        sql += " ORDER BY rank ASC, f.updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._connection.execute(sql, params).fetchall()
        return [self._row_to_fact(row) for row in rows]

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> MessageRecord:
        return MessageRecord(
            id=row["id"],
            session_id=row["session_id"],
            ordinal=row["ordinal"],
            timestamp=row["timestamp"],
            role=row["role"],
            content=row["content"],
            source=row["source"],
            imported_at=row["imported_at"],
            rank=row["rank"] if "rank" in row.keys() else None,
        )

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        source: str,
        *,
        timestamp: str | None = None,
        imported_at: str | None = None,
    ) -> MessageRecord:
        """Append one message to a session transactionally."""

        values = (session_id, role, content, source)
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("session_id, role, content, and source must be non-empty strings")
        if role not in ("user", "assistant"):
            raise ValueError("role must be user or assistant")
        timestamp = timestamp or _now()
        if not isinstance(timestamp, str) or not timestamp.strip():
            raise ValueError("timestamp must be a non-empty string")
        now = _now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO conversation_sessions(id, started_at, updated_at, source)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (session_id.strip(), timestamp, now, source.strip()),
            )
            ordinal = connection.execute(
                "SELECT COALESCE(MAX(ordinal), -1) + 1 FROM conversation_messages WHERE session_id = ?",
                (session_id.strip(),),
            ).fetchone()[0]
            message_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO conversation_messages(
                    id, session_id, ordinal, timestamp, role, content, source, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    session_id.strip(),
                    ordinal,
                    timestamp.strip(),
                    role,
                    content,
                    source.strip(),
                    imported_at,
                ),
            )
            connection.execute(
                "INSERT INTO conversation_messages_fts(message_id, session_id, role, content) VALUES (?, ?, ?, ?)",
                (message_id, session_id.strip(), role, content),
            )
        return MessageRecord(
            id=message_id,
            session_id=session_id.strip(),
            ordinal=ordinal,
            timestamp=timestamp.strip(),
            role=role,
            content=content,
            source=source.strip(),
            imported_at=imported_at,
        )

    def list_messages(self, session_id: str) -> list[MessageRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM conversation_messages WHERE session_id = ? ORDER BY ordinal",
                (session_id,),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def list_conversations(self) -> list[dict[str, Any]]:
        """Return conversation records in the shape expected by the JSON manager."""

        with self._lock:
            sessions = self._connection.execute(
                "SELECT id, started_at, updated_at FROM conversation_sessions ORDER BY updated_at DESC, id"
            ).fetchall()
        result: list[dict[str, Any]] = []
        for session in sessions:
            messages = self.list_messages(session["id"])
            result.append(
                {
                    "id": session["id"],
                    "started_at": session["started_at"],
                    "messages": [
                        {
                            "timestamp": message.timestamp,
                            "role": message.role,
                            "content": message.content,
                        }
                        for message in messages
                    ],
                }
            )
        return result

    def load_conversation(self, session_id: str) -> list[dict[str, str]]:
        return [
            {"role": message.role, "content": message.content}
            for message in self.list_messages(session_id)
        ]

    def delete_conversation(self, session_id: str) -> bool:
        """Delete one SQLite conversation and its FTS rows atomically."""

        with self.transaction() as connection:
            ids = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM conversation_messages WHERE session_id = ?", (session_id,)
                ).fetchall()
            ]
            if not ids:
                return False
            placeholders = ",".join("?" for _ in ids)
            connection.execute(
                f"DELETE FROM conversation_messages_fts WHERE message_id IN ({placeholders})", ids
            )
            connection.execute("DELETE FROM conversation_messages WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM conversation_sessions WHERE id = ?", (session_id,))
            return True

    def search_messages(
        self, query: str, *, session_id: str | None = None, limit: int = 5
    ) -> list[MessageRecord]:
        if not isinstance(query, str) or not query.strip() or limit < 1:
            return []
        tokens = _TOKEN_RE.findall(query.casefold())
        if not tokens:
            return []
        match_query = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
        sql = """
            SELECT m.*, bm25(conversation_messages_fts) AS rank
            FROM conversation_messages_fts
            JOIN conversation_messages AS m ON m.id = conversation_messages_fts.message_id
            WHERE conversation_messages_fts MATCH ?
        """
        params: list[Any] = [match_query]
        if session_id is not None:
            sql += " AND m.session_id = ?"
            params.append(session_id)
        sql += " ORDER BY rank ASC, m.ordinal DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._connection.execute(sql, params).fetchall()
        return [self._row_to_message(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SQLiteMemoryBackend":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

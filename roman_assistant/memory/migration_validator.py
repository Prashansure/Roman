"""Read-only validator for the current JSON memory representation.

The validator never creates a database and never writes to the input files. It
builds deterministic proposed records in memory and reports data that would
need an explicit migration decision.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProposedFact:
    id: str
    category: str
    subject: str
    property_name: str
    fact_text: str
    created_at: str | None
    updated_at: str | None
    source: str

    @property
    def fact_key(self) -> str:
        return self.property_name


@dataclass(frozen=True)
class ProposedMessage:
    id: str
    session_id: str
    role: str
    content: str
    timestamp: str | None
    source: str


@dataclass(frozen=True)
class MigrationReport:
    memory_path: Path
    conversations_path: Path
    source_hashes: dict[str, str]
    source_counts: dict[str, tuple[int, int]]
    proposed_facts: tuple[ProposedFact, ...]
    proposed_messages: tuple[ProposedMessage, ...]
    issues: tuple[str, ...]
    notices: tuple[str, ...]
    duplicate_fact_count: int
    conflict_group_count: int
    invalid_message_count: int

    @property
    def valid(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict[str, Any]:
        return {
            "memory_path": str(self.memory_path),
            "conversations_path": str(self.conversations_path),
            "source_hashes": dict(self.source_hashes),
            "source_counts": dict(self.source_counts),
            "proposed_fact_count": len(self.proposed_facts),
            "proposed_message_count": len(self.proposed_messages),
            "issues": list(self.issues),
            "notices": list(self.notices),
            "duplicate_fact_count": self.duplicate_fact_count,
            "conflict_group_count": self.conflict_group_count,
            "invalid_message_count": self.invalid_message_count,
            "valid": self.valid,
        }


class MigrationValidator:
    """Compare current JSON files with deterministic target records."""

    _ID_NAMESPACE = uuid.UUID("f5f4d9b0-220e-4f5c-90c6-04dbb5e05e48")

    @classmethod
    def _stable_id(cls, kind: str, source: str) -> str:
        return str(uuid.uuid5(cls._ID_NAMESPACE, f"{kind}:{source}"))

    @staticmethod
    def _hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _nonempty_string(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip())

    @classmethod
    def validate(cls, memory_path: Path | str, conversations_path: Path | str) -> MigrationReport:
        memory_path = Path(memory_path)
        conversations_path = Path(conversations_path)
        hashes: dict[str, str] = {}
        issues: list[str] = []
        notices: list[str] = []
        facts: list[ProposedFact] = []
        messages: list[ProposedMessage] = []

        try:
            hashes[str(memory_path)] = cls._hash(memory_path)
            memory_data = json.loads(memory_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(f"memory file could not be read: {exc}")
            memory_data = None

        if isinstance(memory_data, dict):
            for category, bucket in memory_data.items():
                if isinstance(bucket, dict):
                    entries = bucket.items()
                    for key, value in entries:
                        source = f"memory.json#{category}.{key}"
                        if not cls._nonempty_string(key) or not cls._nonempty_string(value):
                            issues.append(f"invalid memory fact at {source}")
                            continue
                        facts.append(
                            ProposedFact(
                                id=cls._stable_id("fact", source),
                                category=str(category),
                                subject=str(category),
                                property_name=key.strip(),
                                fact_text=value.strip(),
                                created_at=None,
                                updated_at=None,
                                source=source,
                            )
                        )
                elif isinstance(bucket, list):
                    for index, item in enumerate(bucket):
                        source = f"memory.json#{category}[{index}]"
                        if isinstance(item, dict):
                            key, value = item.get("key"), item.get("value")
                            if not cls._nonempty_string(key) or not cls._nonempty_string(value):
                                issues.append(f"invalid memory fact at {source}")
                                continue
                            fact_key, fact_text = key.strip(), value.strip()
                        elif cls._nonempty_string(item):
                            fact_key = item.strip()
                            fact_text = item.strip()
                        else:
                            issues.append(f"invalid memory fact at {source}")
                            continue
                        facts.append(
                            ProposedFact(
                                id=cls._stable_id("fact", source),
                                category=str(category),
                                subject=str(category),
                                property_name=fact_key,
                                fact_text=fact_text,
                                created_at=None,
                                updated_at=None,
                                source=source,
                            )
                        )
                else:
                    issues.append(f"unsupported memory category shape at memory.json#{category}")
            if facts:
                notices.append("existing memory facts have no per-fact timestamps; imported records will retain NULL timestamps")
        elif memory_data is not None:
            issues.append("memory root is not a JSON object")

        seen_facts: set[tuple[str, str, str]] = set()
        values_by_key: dict[tuple[str, str], set[str]] = {}
        duplicate_count = 0
        for fact in facts:
            normalized = (
                " ".join(fact.category.casefold().split()),
                " ".join(fact.fact_key.casefold().split()),
                " ".join(fact.fact_text.casefold().split()),
            )
            key = normalized[:2]
            if normalized in seen_facts:
                duplicate_count += 1
            seen_facts.add(normalized)
            values_by_key.setdefault(key, set()).add(normalized[2])
        conflict_keys = {key for key, values in values_by_key.items() if len(values) > 1}
        if duplicate_count:
            notices.append(f"{duplicate_count} exact duplicate fact records will be deduplicated")
        if conflict_keys:
            notices.append(f"{len(conflict_keys)} ambiguous subject/property conflict groups will retain all facts")

        invalid_messages = 0
        if not conversations_path.exists():
            issues.append(f"conversation directory does not exist: {conversations_path}")
        else:
            for path in sorted(conversations_path.glob("????-??-??.json")):
                try:
                    hashes[str(path)] = cls._hash(path)
                    entries = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    issues.append(f"conversation file could not be read: {path.name}: {exc}")
                    continue
                if not isinstance(entries, list):
                    issues.append(f"conversation root is not an array: {path.name}")
                    continue
                for index, entry in enumerate(entries):
                    source = f"{path.name}#entry[{index}]"
                    if not (
                        isinstance(entry, dict)
                        and entry.get("role") in ("user", "assistant")
                        and isinstance(entry.get("content"), str)
                    ):
                        invalid_messages += 1
                        issues.append(f"invalid conversation entry at {source}")
                        continue
                    timestamp = entry.get("timestamp")
                    if timestamp is not None and not isinstance(timestamp, str):
                        issues.append(f"non-string timestamp at {source}")
                        timestamp = None
                    if timestamp is None:
                        notices.append(f"missing timestamp at {source}; imported message timestamp will remain NULL")
                    session_id = entry.get("session_id") or f"legacy:{path.stem}"
                    messages.append(
                        ProposedMessage(
                            id=cls._stable_id("message", source),
                            session_id=str(session_id),
                            role=entry["role"],
                            content=entry["content"],
                            timestamp=timestamp,
                            source=source,
                        )
                    )

        source_counts: dict[str, tuple[int, int]] = {
            str(memory_path): (len(facts), 0)
        }
        for path in sorted(conversations_path.glob("????-??-??.json")) if conversations_path.exists() else []:
            count = sum(1 for message in messages if message.source.startswith(f"{path.name}#"))
            source_counts[str(path)] = (0, count)

        return MigrationReport(
            memory_path=memory_path,
            conversations_path=conversations_path,
            source_hashes=hashes,
            source_counts=source_counts,
            proposed_facts=tuple(facts),
            proposed_messages=tuple(messages),
            issues=tuple(issues),
            notices=tuple(notices),
            duplicate_fact_count=duplicate_count,
            conflict_group_count=len(conflict_keys),
            invalid_message_count=invalid_messages,
        )

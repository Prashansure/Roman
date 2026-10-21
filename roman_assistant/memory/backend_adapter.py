"""Compatibility adapter for bounded retrieval and explicit memory commands.

The live assistant still constructs :class:`MemoryManager` directly.  This
adapter is intentionally opt-in so the new retrieval and command behavior can
be tested without changing the current JSON workflow or creating a live
SQLite database.
"""

from __future__ import annotations

import copy
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Iterable

from config import settings
from core.json_io import JSON_WRITE_LOCK
from memory.memory_manager import ALLOWED_CATEGORIES, DEFAULT_MEMORY, MemoryManager
from memory.identity import is_user_identity_question
from memory.retrieval import (
    RetrievalLimits,
    RetrievalResult,
    RetrievedFact,
    RetrievedMessage,
    bound_history,
    json_facts,
    render_context,
    retrieve_json,
)
from memory.sqlite_backend import SQLiteMemoryBackend


class MemoryBackendError(RuntimeError):
    """Raised when the explicitly selected memory backend cannot start."""


@dataclass(frozen=True)
class MemoryItem:
    id: str
    category: str
    key: str
    value: str
    source: str


@dataclass(frozen=True)
class MemoryMutation:
    action: str
    memory_id: str | None = None
    candidates: tuple[MemoryItem, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class PreparedPrompt:
    history: tuple[dict[str, str], ...]
    memory_context: str
    retrieval: RetrievalResult


class MemoryBackendAdapter:
    """Expose the current manager's surface while adding safe opt-in APIs."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        backend_name: str | None = None,
        sqlite_path: Path | str | None = None,
        limits: RetrievalLimits | None = None,
    ) -> None:
        selected_value = backend_name or settings.MEMORY_BACKEND
        if not isinstance(selected_value, str):
            raise MemoryBackendError("configured memory backend must be 'json' or 'sqlite'")
        selected = selected_value.casefold()
        if selected not in {"json", "sqlite"}:
            raise MemoryBackendError("configured memory backend must be 'json' or 'sqlite'")
        self.backend_name = selected
        self.limits = limits or RetrievalLimits(
            max_facts=settings.MEMORY_MAX_FACTS,
            max_messages=settings.MEMORY_MAX_MESSAGES,
            max_recent_messages=settings.MEMORY_MAX_RECENT_MESSAGES,
            max_fact_chars=settings.MEMORY_MAX_FACT_CHARS,
            max_message_chars=settings.MEMORY_MAX_MESSAGE_CHARS,
            max_memory_context_chars=settings.MEMORY_MAX_CONTEXT_CHARS,
            max_total_prompt_chars=settings.MEMORY_MAX_PROMPT_CHARS,
        )
        self._json: MemoryManager | None = None
        self._sqlite: SQLiteMemoryBackend | None = None
        self._session_id: str | None = None
        self._recent_history: list[dict[str, str]] = []
        if selected == "json":
            self._json = MemoryManager(root)
        else:
            target = Path(sqlite_path or settings.SQLITE_MEMORY_PATH)
            try:
                self._sqlite = SQLiteMemoryBackend(target)
            except Exception as exc:
                raise MemoryBackendError(f"SQLite memory backend is unavailable at {target}: {exc}") from exc

    @property
    def session_id(self) -> str | None:
        return self._json.session_id if self._json else self._session_id

    @property
    def recent_history(self) -> list[dict[str, str]]:
        if self._json:
            return self._json.recent_history
        return list(self._recent_history)

    @property
    def memory(self) -> dict[str, Any]:
        if self._json:
            return self._json.memory
        result = copy.deepcopy(DEFAULT_MEMORY)
        assert self._sqlite is not None
        for fact in self._sqlite.list_facts(active_only=True):
            if fact.category in ("user", "assistant"):
                result.setdefault(fact.category, {})[fact.property_name] = fact.fact_text
            elif fact.category in result and isinstance(result[fact.category], list):
                result[fact.category].append({"key": fact.property_name, "value": fact.fact_text})
        return result

    def start_new_conversation(self) -> str:
        if self._json:
            return self._json.start_new_conversation()
        self._session_id = str(uuid.uuid4())
        self._recent_history = []
        return self._session_id

    def list_conversations(self) -> list[dict[str, Any]]:
        if self._json:
            return self._json.list_conversations()
        assert self._sqlite is not None
        return self._sqlite.list_conversations()

    def load_conversation(self, session_id: str) -> list[dict[str, str]]:
        if self._json:
            return self._json.load_conversation(session_id)
        assert self._sqlite is not None
        messages = self._sqlite.load_conversation(session_id)
        if messages:
            self._session_id = session_id
            self._recent_history = messages[-self.limits.max_recent_messages :]
        return messages

    def delete_conversation(self, session_id: str) -> bool:
        if self._json:
            return self._json.delete_conversation(session_id)
        assert self._sqlite is not None
        changed = self._sqlite.delete_conversation(session_id)
        if self._session_id == session_id:
            self._session_id = None
            self._recent_history = []
        return changed

    def add_message(self, role: str, content: str, session_id: str | None = None) -> bool:
        if self._json:
            return self._json.add_message(role, content, session_id)
        assert self._sqlite is not None
        target = session_id or self._session_id or self.start_new_conversation()
        try:
            self._sqlite.add_message(target, role, content, source="adapter")
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            return False
        if self._session_id == target:
            self._recent_history = (self._recent_history + [{"role": role, "content": content}])[-self.limits.max_recent_messages :]
        return True

    def add_user_message(self, content: str, session_id: str | None = None) -> bool:
        return self.add_message("user", content, session_id)

    def add_assistant_message(self, content: str, session_id: str | None = None) -> bool:
        return self.add_message("assistant", content, session_id)

    def add_turn(self, user_text: str, assistant_text: str, session_id: str | None = None) -> bool:
        target = session_id or self.session_id or self.start_new_conversation()
        return self.add_user_message(user_text, target) and self.add_assistant_message(assistant_text, target)

    def update_memories(self, extraction: Any) -> int:
        if self._json:
            return self._json.update_memories(extraction)
        if not isinstance(extraction, dict) or extraction.get("should_remember") is not True:
            return 0
        items = extraction.get("memories")
        if not isinstance(items, list):
            return 0
        changes = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            category, key, value = item.get("category"), item.get("key"), item.get("value")
            if category not in ALLOWED_CATEGORIES or not isinstance(key, str) or not isinstance(value, str):
                continue
            mutation = self.remember_fact(category, key, value, source="ollama_extraction")
            if mutation.action == "inserted":
                changes += 1
        return changes

    def retrieve_relevant(
        self,
        query: str,
        *,
        recent_history: Iterable[dict[str, str]] | None = None,
        cancel_event: Event | None = None,
    ) -> RetrievalResult:
        if cancel_event and cancel_event.is_set():
            return RetrievalResult(cancelled=True)
        if self._json:
            return retrieve_json(
                self._json.memory,
                query,
                recent_history if recent_history is not None else self._json.recent_history,
                limits=self.limits,
                cancel_event=cancel_event,
            )
        assert self._sqlite is not None
        try:
            facts = [
                RetrievedFact(f.id, f.category, f.subject, f.property_name, f.fact_text, f.source)
                for f in self._sqlite.search(query, limit=self.limits.max_facts)
            ]
            messages = [
                RetrievedMessage(m.id, m.session_id, m.role, m.content, m.source)
                for m in self._sqlite.search_messages(query, limit=self.limits.max_messages)
            ]
            if is_user_identity_question(query):
                facts = [fact for fact in facts if fact.category != "assistant"]
                messages = [message for message in messages if message.role == "user"]
            return RetrievalResult(
                facts=tuple(facts),
                messages=tuple(messages),
                context=render_context(facts, messages, self.limits),
            )
        except Exception as exc:
            return RetrievalResult(error=str(exc))

    def memory_for_prompt(
        self,
        query: str = "",
        *,
        recent_history: Iterable[dict[str, str]] | None = None,
        cancel_event: Event | None = None,
    ) -> str:
        result = self.retrieve_relevant(query, recent_history=recent_history, cancel_event=cancel_event)
        return "" if result.cancelled or result.error else result.context

    def prepare_prompt(
        self,
        query: str,
        history: Iterable[dict[str, str]] | None = None,
        *,
        cancel_event: Event | None = None,
    ) -> PreparedPrompt:
        result = self.retrieve_relevant(query, recent_history=history, cancel_event=cancel_event)
        context = "" if result.cancelled or result.error else result.context
        prompt_history = list(history if history is not None else self.recent_history)
        if is_user_identity_question(query):
            prompt_history = [
                row for row in prompt_history
                if isinstance(row, dict) and row.get("role") != "assistant"
            ]
            user = self.memory.get("user", {})
            name = user.get("name") if isinstance(user, dict) else None
            known_name = name.strip() if isinstance(name, str) and name.strip() else "unknown"
            identity = (
                "Authoritative identity facts: "
                f"assistant name is {settings.APP_NAME}; user name is {known_name}. "
                "These identities are separate; never use the assistant name as the user name. "
                "If the user name is unknown, say it is not known."
            )
            context = f"{identity}\n{context}" if context else identity
        bounded = bound_history(
            prompt_history,
            context,
            query,
            limits=self.limits,
        )
        return PreparedPrompt(tuple(bounded), context, result)

    def remember_user_name(self, name: str, *, source: str = "explicit user statement") -> MemoryMutation:
        """Store the user's explicit name, replacing a prior name deterministically."""

        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
            return MemoryMutation("invalid", error="a valid user name is required")
        name = name.strip()
        if self._sqlite:
            try:
                current = next(
                    (fact for fact in self._sqlite.list_facts(active_only=True)
                     if fact.category == "user" and fact.property_name.casefold() == "name"),
                    None,
                )
                result = self._sqlite.add_fact(
                    "user", "name", name, source,
                    explicit_correction=bool(current and current.fact_text.casefold() != name.casefold()),
                    correction_reason="User explicitly stated or corrected their name",
                )
                return MemoryMutation(result.action, result.fact_id)
            except Exception as exc:
                return MemoryMutation("error", error=str(exc))

        assert self._json is not None
        with JSON_WRITE_LOCK:
            bucket = self._json.memory.setdefault("user", {})
            if not isinstance(bucket, dict):
                return MemoryMutation("invalid", error="user memory category has unsupported shape")
            previous = bucket.get("name")
            if isinstance(previous, str) and previous.casefold() == name.casefold():
                return MemoryMutation("duplicate", "json:user.name")
            bucket["name"] = name
            try:
                self._json._write_json(self._json.memory_path, self._json.memory)
            except OSError as exc:
                if previous is None:
                    bucket.pop("name", None)
                else:
                    bucket["name"] = previous
                return MemoryMutation("error", error=str(exc))
        return MemoryMutation("corrected" if previous is not None else "inserted", "json:user.name")

    def list_memory_items(self, category: str | None = None) -> list[MemoryItem]:
        if self._json:
            facts = json_facts(self._json.memory)
            items = [MemoryItem(f.id, f.category, f.property_name, f.fact_text, f.source) for f in facts]
        else:
            assert self._sqlite is not None
            items = [MemoryItem(f.id, f.category, f.property_name, f.fact_text, f.source) for f in self._sqlite.list_facts()]
        return [item for item in items if category is None or item.category == category]

    def find_memory_candidates(self, query: str) -> list[MemoryItem]:
        normalized = query.casefold().strip()
        if not normalized:
            return []
        return [
            item for item in self.list_memory_items()
            if normalized in f"{item.category} {item.key} {item.value}".casefold()
        ]

    def remember_fact(self, category: str, key: str, value: str, *, source: str = "user command") -> MemoryMutation:
        if category not in ALLOWED_CATEGORIES or not all(isinstance(v, str) and v.strip() for v in (key, value, source)):
            return MemoryMutation("invalid", error="category, key, value, and source are required")
        key, value, source = key.strip(), value.strip(), source.strip()
        if len(key) > 100 or len(value) > 1000:
            return MemoryMutation("invalid", error="key or value is too long")
        if self._sqlite:
            try:
                result = self._sqlite.add_fact(category, key, value, source)
                return MemoryMutation(result.action, result.fact_id)
            except Exception as exc:
                return MemoryMutation("error", error=str(exc))

        assert self._json is not None
        with JSON_WRITE_LOCK:
            items = self.list_memory_items(category)
            exact = next((item for item in items if item.key.casefold() == key.casefold() and item.value.casefold() == value.casefold()), None)
            if exact:
                return MemoryMutation("duplicate", exact.id)
            same_key = tuple(item for item in items if item.key.casefold() == key.casefold())
            if same_key and category in ("user", "assistant"):
                return MemoryMutation("conflict_preserved", candidates=same_key)
            bucket = self._json.memory[category]
            if isinstance(bucket, dict):
                bucket[key] = value
            elif isinstance(bucket, list):
                bucket.append({"key": key, "value": value})
            else:
                return MemoryMutation("invalid", error="memory category has unsupported shape")
            try:
                self._json._write_json(self._json.memory_path, self._json.memory)
            except OSError as exc:
                return MemoryMutation("error", error=str(exc))
        item = next(item for item in self.list_memory_items(category) if item.key == key and item.value == value)
        return MemoryMutation("inserted", item.id)

    def forget_fact(self, memory_id: str) -> bool:
        if not isinstance(memory_id, str) or not memory_id.strip():
            return False
        if self._sqlite:
            try:
                return self._sqlite.delete_fact(memory_id.strip())
            except (OSError, RuntimeError, sqlite3.Error):
                return False
        assert self._json is not None
        target = next((item for item in self.list_memory_items() if item.id == memory_id.strip()), None)
        if target is None:
            return False
        with JSON_WRITE_LOCK:
            bucket = self._json.memory[target.category]
            if isinstance(bucket, dict):
                if bucket.get(target.key) != target.value:
                    return False
                del bucket[target.key]
            elif isinstance(bucket, list):
                before = len(bucket)
                bucket[:] = [
                    row for row in bucket
                    if not (isinstance(row, dict) and row.get("key") == target.key and row.get("value") == target.value)
                ]
                if len(bucket) == before:
                    return False
            else:
                return False
            try:
                self._json._write_json(self._json.memory_path, self._json.memory)
            except OSError:
                return False
        return True

    def close(self) -> None:
        if self._sqlite:
            self._sqlite.close()

    def __enter__(self) -> "MemoryBackendAdapter":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

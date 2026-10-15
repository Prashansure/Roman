"""File-backed local memory with bounded context and safe JSON handling."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from config.settings import BASE_DIR
from core.json_io import JSON_WRITE_LOCK, write_json_atomic


DEFAULT_MEMORY: dict[str, Any] = {
    "user": {},
    "assistant": {"name": "Roman"},
    "preferences": [],
    "projects": [],
    "important_facts": [],
}
ALLOWED_CATEGORIES = {"user", "assistant", "preferences", "projects", "important_facts"}
MAX_RECENT_MESSAGES = 12


class MemoryManager:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or (BASE_DIR / "memory")
        self.memory_path = self.root / "memory.json"
        self.conversations_path = self.root / "conversations"
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.conversations_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._log(f"Memory storage unavailable: {exc}")
        self.memory = self.load_memory()
        self.session_id: str | None = None
        self.recent_history = self.load_recent_conversation()

    @staticmethod
    def _log(message: str) -> None:
        print(f"[MEMORY] {message}")

    @staticmethod
    def _default_copy() -> dict[str, Any]:
        return json.loads(json.dumps(DEFAULT_MEMORY))

    def _write_json(self, path: Path, data: Any) -> None:
        write_json_atomic(path, data)

    def load_memory(self) -> dict[str, Any]:
        if not self.memory_path.exists():
            data = self._default_copy()
            try:
                self._write_json(self.memory_path, data)
                self._log("Created memory.json")
            except OSError as exc:
                self._log(f"Failed to create memory: {exc}")
            return data
        try:
            with self.memory_path.open("r", encoding="utf-8") as handle:
                parsed = json.load(handle)
            if not isinstance(parsed, dict):
                raise ValueError("memory root must be an object")
            data = self._default_copy()
            data.update(parsed)
            if not isinstance(data["user"], dict):
                data["user"] = {}
            if not isinstance(data["assistant"], dict):
                data["assistant"] = {"name": "Roman"}
            for category in ("preferences", "projects", "important_facts"):
                if not isinstance(data[category], list):
                    data[category] = []
            count = sum(len(v) for v in data.values() if isinstance(v, (dict, list)))
            self._log(f"Loaded {count} memories")
            return data
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            self._log(f"Failed to load memory: {exc}")
            # Keep the damaged file for recovery before writing a clean default.
            try:
                backup = self.memory_path.with_name(f"memory.corrupt.{datetime.now():%Y%m%d%H%M%S}.json")
                os.replace(self.memory_path, backup)
                self._log(f"Preserved corrupted memory as {backup.name}")
            except OSError:
                pass
            data = self._default_copy()
            try:
                self._write_json(self.memory_path, data)
            except OSError as write_exc:
                self._log(f"Failed to initialize memory: {write_exc}")
            return data

    def load_recent_conversation(self) -> list[dict[str, str]]:
        conversations = self.list_conversations()
        if not conversations:
            self._log("No recent conversation found")
            return []
        latest = conversations[0]
        self.session_id = latest["id"]
        recent = latest["messages"][-MAX_RECENT_MESSAGES:]
        self._log("Loaded recent conversation")
        return [{"role": row["role"], "content": row["content"]} for row in recent]

    def list_conversations(self) -> list[dict[str, Any]]:
        """Return saved chats in newest-first order, keeping legacy logs readable."""
        try:
            files = sorted(self.conversations_path.glob("????-??-??.json"))
            sessions: dict[str, dict[str, Any]] = {}
            for path in files:
                try:
                    with path.open("r", encoding="utf-8") as handle:
                        entries = json.load(handle)
                except (OSError, json.JSONDecodeError) as exc:
                    self._log(f"Failed to load conversation {path.name}: {exc}")
                    continue
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not (isinstance(entry, dict) and entry.get("role") in ("user", "assistant") and isinstance(entry.get("content"), str)):
                        continue
                    session_id = entry.get("session_id") or f"legacy:{path.stem}"
                    timestamp = entry.get("timestamp", f"{path.stem}T00:00:00")
                    session = sessions.setdefault(session_id, {
                        "id": session_id, "started_at": timestamp, "messages": [],
                    })
                    session["messages"].append({
                        "timestamp": timestamp, "role": entry["role"], "content": entry["content"],
                    })
            return sorted(sessions.values(), key=lambda row: row["messages"][-1]["timestamp"], reverse=True)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            self._log(f"Failed to load recent conversation: {exc}")
            return []

    def start_new_conversation(self) -> str:
        self.session_id = str(uuid.uuid4())
        self.recent_history = []
        return self.session_id

    def load_conversation(self, session_id: str) -> list[dict[str, str]]:
        conversation = next((row for row in self.list_conversations() if row["id"] == session_id), None)
        if conversation is None:
            return []
        self.session_id = session_id
        messages = conversation["messages"]
        self.recent_history = [
            {"role": row["role"], "content": row["content"]}
            for row in messages[-MAX_RECENT_MESSAGES:]
        ]
        return messages

    def delete_conversation(self, session_id: str) -> bool:
        """Remove one saved conversation while preserving other sessions."""
        changed = False
        try:
            with JSON_WRITE_LOCK:
                for path in self.conversations_path.glob("????-??-??.json"):
                    try:
                        with path.open("r", encoding="utf-8") as handle:
                            entries = json.load(handle)
                    except (OSError, json.JSONDecodeError):
                        continue
                    if not isinstance(entries, list):
                        continue
                    kept = [
                        row for row in entries
                        if not (
                            isinstance(row, dict)
                            and (row.get("session_id") or f"legacy:{path.stem}") == session_id
                        )
                    ]
                    if len(kept) != len(entries):
                        self._write_json(path, kept)
                        changed = True
                if self.session_id == session_id:
                    self.session_id = None
                    self.recent_history = []
            return changed
        except OSError as exc:
            self._log(f"Failed to delete conversation: {exc}")
            return False

    def add_message(
        self, role: str, content: str, session_id: str | None = None
    ) -> bool:
        """Append one user or assistant message immediately and update context."""
        if role not in ("user", "assistant") or not isinstance(content, str):
            return False
        with JSON_WRITE_LOCK:
            if session_id is None:
                session_id = self.session_id or self.start_new_conversation()
            now = datetime.now().astimezone()
            path = self.conversations_path / f"{now:%Y-%m-%d}.json"
            entry = {
                "timestamp": now.isoformat(),
                "role": role,
                "content": content,
                "session_id": session_id,
            }
            try:
                existing: list[dict[str, Any]] = []
                if path.exists():
                    with path.open("r", encoding="utf-8") as handle:
                        parsed = json.load(handle)
                    if not isinstance(parsed, list):
                        raise ValueError("conversation root must be an array")
                    existing = parsed
                existing.append(entry)
                self._write_json(path, existing)
                if self.session_id == session_id:
                    self.recent_history = (
                        self.recent_history + [{"role": role, "content": content}]
                    )[-MAX_RECENT_MESSAGES:]
                self._log(f"Saved {role} message")
                return True
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                self._log(f"Failed to save {role} message: {exc}")
                return False

    def add_user_message(self, content: str, session_id: str | None = None) -> bool:
        return self.add_message("user", content, session_id)

    def add_assistant_message(self, content: str, session_id: str | None = None) -> bool:
        return self.add_message("assistant", content, session_id)

    def add_turn(self, user_text: str, assistant_text: str, session_id: str | None = None) -> bool:
        with JSON_WRITE_LOCK:
            if session_id is None:
                session_id = self.session_id or self.start_new_conversation()
            return (
                self.add_user_message(user_text, session_id)
                and self.add_assistant_message(assistant_text, session_id)
            )

    def update_memories(self, extraction: Any) -> int:
        if not isinstance(extraction, dict) or extraction.get("should_remember") is not True:
            self._log("No new memory")
            return 0
        items = extraction.get("memories")
        if not isinstance(items, list):
            self._log("No new memory")
            return 0

        changes = 0
        with JSON_WRITE_LOCK:
            return self._update_memories_locked(items)

    def _update_memories_locked(self, items: list[Any]) -> int:
        changes = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            category = item.get("category")
            key, value = item.get("key"), item.get("value")
            if category not in ALLOWED_CATEGORIES or not isinstance(key, str) or not isinstance(value, str):
                continue
            key, value = key.strip(), value.strip()
            if not key or not value or len(key) > 100 or len(value) > 1000:
                continue
            if category in ("user", "assistant"):
                previous = self.memory[category].get(key)
                if previous == value:
                    continue
                self.memory[category][key] = value
            else:
                bucket = self.memory[category]
                normalized_key = key.casefold()
                match = next((row for row in bucket if isinstance(row, dict) and str(row.get("key", "")).casefold() == normalized_key), None)
                if match:
                    if match.get("value") == value:
                        continue
                    match["value"] = value
                elif not any(
                    (isinstance(row, str) and row.casefold() == value.casefold())
                    or (isinstance(row, dict) and str(row.get("value", "")).casefold() == value.casefold())
                    for row in bucket
                ):
                    bucket.append({"key": key, "value": value})
                else:
                    continue
            changes += 1

        if changes:
            try:
                self._write_json(self.memory_path, self.memory)
                self._log("New memory detected")
                self._log("Memory saved")
            except OSError as exc:
                self._log(f"Failed to save memory: {exc}")
        else:
            self._log("No new memory")
        return changes

    def memory_for_prompt(self) -> str:
        parts = []
        for category, value in self.memory.items():
            if isinstance(value, dict):
                parts.extend(f"{category}.{key}: {item}" for key, item in value.items())
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and item.get("value"):
                        parts.append(f"{category}: {item['value']}")
                    elif isinstance(item, str):
                        parts.append(f"{category}: {item}")
        return "\n".join(parts)


def parse_extraction(content: str) -> dict[str, Any]:
    """Parse structured JSON from Ollama, tolerating common Markdown fences."""
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("extractor returned no JSON object")
    result = json.loads(text[start : end + 1])
    if not isinstance(result, dict):
        raise ValueError("extractor JSON must be an object")
    return result

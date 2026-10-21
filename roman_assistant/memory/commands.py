"""Explicit, user-controlled memory commands.

Only slash-prefixed commands are handled here.  Ordinary messages never enter
the mutation path, and an ambiguous forget request only returns candidates.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from memory.backend_adapter import MemoryBackendAdapter, MemoryItem


@dataclass(frozen=True)
class CommandResult:
    handled: bool
    kind: str = "ignored"
    response: str = ""
    changed: bool = False
    requires_confirmation: bool = False
    candidates: tuple[MemoryItem, ...] = ()


def _fields(tokens: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for token in tokens:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        if key and key not in fields:
            fields[key.casefold()] = value
    return fields


class MemoryCommandProcessor:
    """Parse and execute the deliberately narrow memory command grammar."""

    def __init__(self, adapter: MemoryBackendAdapter) -> None:
        self.adapter = adapter

    def handle(self, text: str) -> CommandResult:
        if not isinstance(text, str) or not text.lstrip().startswith(("/remember", "/memory", "/forget")):
            return CommandResult(False)
        try:
            tokens = shlex.split(text.strip())
        except ValueError as exc:
            return CommandResult(True, "invalid", f"Invalid memory command: {exc}")
        if not tokens:
            return CommandResult(False)
        command = tokens[0].casefold()
        if command == "/remember":
            return self._remember(tokens[1:])
        if command == "/memory":
            return self._list(tokens[1:])
        if command == "/forget":
            return self._forget(tokens[1:])
        return CommandResult(False)

    def _remember(self, tokens: list[str]) -> CommandResult:
        fields = _fields(tokens)
        if set(fields) != {"category", "key", "value"} or any(not fields[field].strip() for field in fields):
            return CommandResult(True, "invalid", "Use /remember category=<category> key=<key> value=<value>")
        mutation = self.adapter.remember_fact(fields["category"], fields["key"], fields["value"])
        if mutation.action == "inserted":
            return CommandResult(True, "remembered", f"Saved memory {mutation.memory_id}.", True)
        if mutation.action == "duplicate":
            return CommandResult(True, "duplicate", f"That fact is already saved as {mutation.memory_id}.")
        if mutation.action == "conflict_preserved":
            ids = ", ".join(item.id for item in mutation.candidates)
            return CommandResult(
                True,
                "conflict",
                f"Kept both facts; this conflicts with {ids}. Use an explicit correction workflow.",
                candidates=mutation.candidates,
            )
        return CommandResult(True, "error", mutation.error or "Could not save memory.")

    def _list(self, tokens: list[str]) -> CommandResult:
        if not tokens or tokens[0].casefold() != "list":
            return CommandResult(True, "invalid", "Use /memory list [category=<category>]")
        fields = _fields(tokens[1:])
        if set(fields) - {"category"}:
            return CommandResult(True, "invalid", "Use /memory list [category=<category>]")
        items = self.adapter.list_memory_items(fields.get("category"))
        if not items:
            return CommandResult(True, "list", "No saved memories.")
        lines = [f"{item.id}: {item.category}.{item.key} = {item.value}" for item in items]
        return CommandResult(True, "list", "\n".join(lines), candidates=tuple(items))

    def _forget(self, tokens: list[str]) -> CommandResult:
        if tokens and tokens[0].casefold() == "confirm":
            tokens = tokens[1:]
        fields = _fields(tokens)
        if set(fields) == {"id"} and fields["id"].strip():
            memory_id = fields["id"].strip()
            if self.adapter.forget_fact(memory_id):
                return CommandResult(True, "forgotten", f"Forgot memory {memory_id}.", True)
            return CommandResult(True, "not_found", f"No saved memory has id {memory_id}.")
        if set(fields) == {"key"} and fields["key"].strip():
            candidates = tuple(self.adapter.find_memory_candidates(fields["key"]))
            if not candidates:
                return CommandResult(True, "not_found", "No matching saved memory found.")
            lines = ", ".join(f"{item.id} ({item.category}.{item.key} = {item.value})" for item in candidates)
            return CommandResult(
                True,
                "confirmation_required",
                f"Choose an exact id before forgetting: {lines}",
                requires_confirmation=True,
                candidates=candidates,
            )
        return CommandResult(True, "invalid", "Use /forget id=<exact-id> or /forget key=<text>")

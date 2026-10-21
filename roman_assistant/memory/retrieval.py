"""Bounded, deterministic memory retrieval for future prompt integration."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from threading import Event
from typing import Any, Iterable

from memory.identity import is_user_identity_question


@dataclass(frozen=True)
class RetrievalLimits:
    max_facts: int = 8
    max_messages: int = 6
    max_recent_messages: int = 12
    max_fact_chars: int = 500
    max_message_chars: int = 1000
    max_memory_context_chars: int = 6000
    max_total_prompt_chars: int = 16000


@dataclass(frozen=True)
class RetrievedFact:
    id: str
    category: str
    subject: str
    property_name: str
    fact_text: str
    source: str


@dataclass(frozen=True)
class RetrievedMessage:
    id: str
    session_id: str | None
    role: str
    content: str
    source: str


@dataclass(frozen=True)
class RetrievalResult:
    facts: tuple[RetrievedFact, ...] = ()
    messages: tuple[RetrievedMessage, ...] = ()
    context: str = ""
    cancelled: bool = False
    error: str | None = None


_TOKEN_RE = re.compile(r"[\w]+", flags=re.UNICODE)
_ID_NAMESPACE = uuid.UUID("e1b2d7f5-3a66-4e04-9f40-3f1d7a2ee89d")


def normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def tokens(value: str) -> tuple[str, ...]:
    return tuple(_TOKEN_RE.findall(normalize(value)))


def _stable_json_id(category: str, key: str, value: str) -> str:
    return "json:" + str(uuid.uuid5(_ID_NAMESPACE, f"{category}:{normalize(key)}:{normalize(value)}"))


def json_facts(memory: dict[str, Any]) -> list[RetrievedFact]:
    result: list[RetrievedFact] = []
    for category, bucket in memory.items():
        if isinstance(bucket, dict):
            for key, value in bucket.items():
                if isinstance(key, str) and isinstance(value, str) and key.strip() and value.strip():
                    result.append(
                        RetrievedFact(
                            id=_stable_json_id(category, key, value),
                            category=category,
                            subject=category,
                            property_name=key.strip(),
                            fact_text=value.strip(),
                            source="json:memory.json",
                        )
                    )
        elif isinstance(bucket, list):
            for index, item in enumerate(bucket):
                if isinstance(item, dict):
                    key, value = item.get("key"), item.get("value")
                    if not isinstance(key, str) or not isinstance(value, str) or not key.strip() or not value.strip():
                        continue
                    property_name, fact_text = key.strip(), value.strip()
                elif isinstance(item, str) and item.strip():
                    property_name = f"item-{index}"
                    fact_text = item.strip()
                else:
                    continue
                result.append(
                    RetrievedFact(
                        id=_stable_json_id(category, property_name, fact_text),
                        category=category,
                        subject=category,
                        property_name=property_name,
                        fact_text=fact_text,
                        source="json:memory.json",
                    )
                )
    return result


def _rank_facts(facts: Iterable[RetrievedFact], query: str, limit: int) -> list[RetrievedFact]:
    query_tokens = set(tokens(query))
    if not query_tokens:
        return []
    ranked: list[tuple[int, RetrievedFact]] = []
    identity_question = is_user_identity_question(query)
    for fact in facts:
        # The assistant's configured name is not a fact about the user.
        if identity_question and fact.category == "assistant":
            continue
        haystack = set(tokens(f"{fact.category} {fact.subject} {fact.property_name} {fact.fact_text}"))
        score = len(query_tokens & haystack)
        if identity_question and fact.category == "user" and fact.property_name.casefold() == "name":
            score += len(query_tokens) + 1
        if score:
            ranked.append((score, fact))
    ranked.sort(key=lambda item: (-item[0], item[1].category, item[1].property_name, item[1].fact_text))
    return [fact for _score, fact in ranked[: max(0, limit)]]


def _rank_messages(messages: Iterable[RetrievedMessage], query: str, limit: int) -> list[RetrievedMessage]:
    query_tokens = set(tokens(query))
    candidates: list[tuple[int, int, RetrievedMessage]] = []
    for index, message in enumerate(messages):
        haystack = set(tokens(message.content))
        score = len(query_tokens & haystack) if query_tokens else 0
        if score or not query_tokens:
            candidates.append((score, index, message))
    candidates.sort(key=lambda item: (-item[0], -item[1]))
    return [message for _score, _index, message in candidates[: max(0, limit)]]


def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def render_context(
    facts: Iterable[RetrievedFact],
    messages: Iterable[RetrievedMessage],
    limits: RetrievalLimits,
) -> str:
    lines: list[str] = []
    facts = list(facts)
    messages = list(messages)
    if facts:
        lines.append("Relevant long-term facts:")
        lines.extend(
            f"- {fact.category}.{fact.property_name}: {_clip(fact.fact_text, limits.max_fact_chars)}"
            for fact in facts[: limits.max_facts]
        )
    if messages:
        if lines:
            lines.append("")
        lines.append("Relevant conversation:")
        lines.extend(
            f"- {message.role}: {_clip(message.content, limits.max_message_chars)}"
            for message in messages[: limits.max_messages]
        )
    context = "\n".join(lines)
    return _clip(context, limits.max_memory_context_chars)


def retrieve_json(
    memory: dict[str, Any],
    query: str,
    recent_history: Iterable[dict[str, str]] = (),
    *,
    limits: RetrievalLimits = RetrievalLimits(),
    cancel_event: Event | None = None,
) -> RetrievalResult:
    if cancel_event and cancel_event.is_set():
        return RetrievalResult(cancelled=True)
    try:
        facts = _rank_facts(json_facts(memory), query, limits.max_facts)
        recent = [
            RetrievedMessage(
                id=f"recent:{index}",
                session_id=None,
                role=row.get("role", ""),
                content=row.get("content", ""),
                source="json:recent_history",
            )
            for index, row in enumerate(list(recent_history)[-limits.max_recent_messages :])
            if isinstance(row, dict) and row.get("role") in ("user", "assistant") and isinstance(row.get("content"), str)
        ]
        if is_user_identity_question(query):
            # Old assistant claims are conversational output, not identity evidence.
            recent = [message for message in recent if message.role == "user"]
        messages = _rank_messages(recent, query, limits.max_messages)
        return RetrievalResult(
            facts=tuple(facts),
            messages=tuple(messages),
            context=render_context(facts, messages, limits),
        )
    except Exception as exc:
        return RetrievalResult(error=str(exc))


def bound_history(
    history: Iterable[dict[str, str]],
    memory_context: str,
    query: str,
    *,
    limits: RetrievalLimits = RetrievalLimits(),
) -> list[dict[str, str]]:
    """Keep recent history plus memory and query within a character budget."""

    remaining = max(0, limits.max_total_prompt_chars - len(memory_context) - len(query))
    selected: list[dict[str, str]] = []
    used = 0
    for row in list(history)[-limits.max_recent_messages :][::-1]:
        if not isinstance(row, dict) or row.get("role") not in ("user", "assistant"):
            continue
        content = row.get("content")
        if not isinstance(content, str):
            continue
        clipped = _clip(content, limits.max_message_chars)
        cost = len(clipped) + len(row["role"]) + 16
        if selected and used + cost > remaining:
            break
        if not selected and cost > remaining:
            clipped = _clip(clipped, max(0, remaining - len(row["role"]) - 16))
            cost = len(clipped) + len(row["role"]) + 16
        selected.append({"role": row["role"], "content": clipped})
        used += cost
    return list(reversed(selected))

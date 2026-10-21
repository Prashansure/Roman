"""Small, deterministic rules for separating Roman and user identity."""

from __future__ import annotations

import re
from typing import Any


_NAME_CORRECTION = re.compile(
    r"\bmy\s+name\s+(?:is\s+not|isn't)\b.*?\b(?:it\s+is|it's)\s+(.+?)(?=[.!?,;]|\s+(?:and|but|because)\b|$)",
    re.IGNORECASE,
)
_NAME_STATEMENT = re.compile(
    r"\bmy\s+name\s+is\s+(.+?)(?=[.!?,;]|\s+(?:and|but|because)\b|$)",
    re.IGNORECASE,
)
_USER_IDENTITY_QUESTION = re.compile(
    r"\b(?:my\s+name|who\s+am\s+i|do\s+you\s+know\s+my\s+name)\b",
    re.IGNORECASE,
)
_ASSISTANT_IDENTITY_QUESTION = re.compile(r"\b(?:your\s+name|who\s+are\s+you)\b", re.IGNORECASE)


def extract_explicit_user_name(text: str) -> str | None:
    """Return a name only when the user explicitly says "my name is ..."."""

    if not isinstance(text, str):
        return None
    match = _NAME_CORRECTION.search(text) or _NAME_STATEMENT.search(text)
    if not match:
        return None
    name = match.group(1).strip().strip("\"'“”‘’ ")
    if not name or len(name) > 100 or any(char.isdigit() for char in name):
        return None
    if name.casefold() in {"what", "unknown", "not", "isn't", "is not"} or name.casefold().startswith("not "):
        return None
    return name


def is_user_identity_question(text: str) -> bool:
    """Identify personal-name questions without matching assistant-name asks."""

    return isinstance(text, str) and bool(_USER_IDENTITY_QUESTION.search(text))


def identity_response(text: str, *, user_name: str | None, assistant_name: str) -> str | None:
    """Answer direct identity questions from configured/profile facts only."""

    if not isinstance(text, str):
        return None
    if _ASSISTANT_IDENTITY_QUESTION.search(text):
        return f"I'm {assistant_name}."
    if is_user_identity_question(text):
        if isinstance(user_name, str) and user_name.strip():
            return f"Your name is {user_name.strip()}."
        return "I don't know your name yet."
    return None


def filter_identity_extraction(extraction: Any) -> Any:
    """Prevent model-generated identity guesses from changing profile facts."""

    if not isinstance(extraction, dict) or not isinstance(extraction.get("memories"), list):
        return extraction
    filtered = dict(extraction)
    filtered["memories"] = [
        item for item in extraction["memories"]
        if not (
            isinstance(item, dict)
            and item.get("category") in ("user", "assistant")
            and isinstance(item.get("key"), str)
            and item["key"].strip().casefold().replace("_", " ").replace("-", " ")
            in {"name", "user name", "assistant name", "full name", "first name"}
        )
    ]
    if not filtered["memories"]:
        filtered["should_remember"] = False
    return filtered

"""Read-only comparison of JSON and SQLite retrieval on synthetic data."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event

from memory.backend_adapter import MemoryBackendAdapter
from memory.retrieval import RetrievalResult, normalize


@dataclass(frozen=True)
class ShadowComparison:
    query: str
    json_keys: tuple[tuple[str, str, str], ...]
    sqlite_keys: tuple[tuple[str, str, str], ...]
    common: tuple[tuple[str, str, str], ...]
    json_only: tuple[tuple[str, str, str], ...]
    sqlite_only: tuple[tuple[str, str, str], ...]
    json_context: str
    sqlite_context: str
    json_error: str | None = None
    sqlite_error: str | None = None


def _keys(result: RetrievalResult) -> tuple[tuple[str, str, str], ...]:
    return tuple(sorted((normalize(f.category), normalize(f.property_name), normalize(f.fact_text)) for f in result.facts))


def compare_retrieval(
    json_adapter: MemoryBackendAdapter,
    sqlite_adapter: MemoryBackendAdapter,
    query: str,
    *,
    cancel_event: Event | None = None,
) -> ShadowComparison:
    """Compare canonical fact results without writing either backend."""

    json_result = json_adapter.retrieve_relevant(query, cancel_event=cancel_event)
    sqlite_result = sqlite_adapter.retrieve_relevant(query, cancel_event=cancel_event)
    json_keys = _keys(json_result)
    sqlite_keys = _keys(sqlite_result)
    json_set, sqlite_set = set(json_keys), set(sqlite_keys)
    return ShadowComparison(
        query=query,
        json_keys=json_keys,
        sqlite_keys=sqlite_keys,
        common=tuple(sorted(json_set & sqlite_set)),
        json_only=tuple(sorted(json_set - sqlite_set)),
        sqlite_only=tuple(sorted(sqlite_set - json_set)),
        json_context=json_result.context,
        sqlite_context=sqlite_result.context,
        json_error=json_result.error,
        sqlite_error=sqlite_result.error,
    )

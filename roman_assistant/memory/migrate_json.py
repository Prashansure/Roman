"""Explicit, idempotent JSON-to-SQLite migration command.

The command defaults to a read-only dry run. Applying a migration requires the
explicit ``--apply`` flag and a separate target database path. The live JSON
backend is never replaced by this module.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from memory.migration_validator import MigrationReport, MigrationValidator
from memory.sqlite_backend import MigrationIntegrityError, SQLiteMemoryBackend, _now


class MigrationValidationError(RuntimeError):
    """Raised when source data is not safe to import."""


@dataclass(frozen=True)
class MigrationResult:
    dry_run: bool
    database_path: Path
    imported_at: str | None
    report: MigrationReport
    stats: dict[str, int]

    def as_dict(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "database_path": str(self.database_path),
            "imported_at": self.imported_at,
            "report": self.report.as_dict(),
            "stats": dict(self.stats),
        }


class JsonMigrationCommand:
    """Plan or apply one deterministic migration into a separate database."""

    def __init__(
        self,
        memory_path: Path | str,
        conversations_path: Path | str,
        database_path: Path | str,
    ) -> None:
        self.memory_path = Path(memory_path)
        self.conversations_path = Path(conversations_path)
        self.database_path = Path(database_path)

    def plan(self) -> MigrationReport:
        return MigrationValidator.validate(self.memory_path, self.conversations_path)

    def _validate_target_location(self) -> None:
        memory_resolved = self.memory_path.resolve()
        database_resolved = self.database_path.resolve()
        conversation_resolved = self.conversations_path.resolve()
        if database_resolved == memory_resolved:
            raise MigrationValidationError("target database cannot replace memory.json")
        try:
            database_resolved.relative_to(conversation_resolved)
        except ValueError:
            return
        raise MigrationValidationError("target database cannot be inside the JSON conversations directory")

    def run(self, *, dry_run: bool = True) -> MigrationResult:
        report = self.plan()
        if not report.valid:
            raise MigrationValidationError("source validation failed: " + "; ".join(report.issues))
        if dry_run:
            return MigrationResult(True, self.database_path, None, report, {})

        self._validate_target_location()
        imported_at = _now()
        source_paths = {
            str(self.memory_path): self.memory_path,
            **{
                str(path): path
                for path in sorted(self.conversations_path.glob("????-??-??.json"))
            },
        }
        with SQLiteMemoryBackend(self.database_path) as backend:
            stats = backend.import_records(
                report.proposed_facts,
                report.proposed_messages,
                report.source_hashes,
                report.source_counts,
                imported_at=imported_at,
                source_paths=source_paths,
            )

        after = self.plan()
        if after.source_hashes != report.source_hashes:
            raise MigrationIntegrityError("source hashes changed after migration")
        if len(after.proposed_facts) != len(report.proposed_facts) or len(after.proposed_messages) != len(report.proposed_messages):
            raise MigrationIntegrityError("source counts changed after migration")
        return MigrationResult(False, self.database_path, imported_at, after, stats)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory", required=True, type=Path, help="path to memory.json")
    parser.add_argument("--conversations", required=True, type=Path, help="path to conversation JSON directory")
    parser.add_argument("--database", required=True, type=Path, help="separate target SQLite database")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="validate only; this is the default")
    mode.add_argument("--apply", action="store_true", help="apply to the separate target database")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command = JsonMigrationCommand(args.memory, args.conversations, args.database)
    result = command.run(dry_run=not args.apply)
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

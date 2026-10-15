"""Process-wide, atomic JSON writes for Roman's local configuration and data."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


JSON_WRITE_LOCK = threading.RLock()


def write_json_atomic(path: Path, data: Any) -> None:
    """Serialize JSON writes and replace the target only after a full write."""
    path = Path(path)
    temp_path = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    with JSON_WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

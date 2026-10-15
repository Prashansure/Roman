"""One-process guard for the desktop application."""

from __future__ import annotations

import ctypes
import hashlib
import os
import tempfile
from pathlib import Path


ERROR_ALREADY_EXISTS = 183


class SingleInstanceGuard:
    """Hold an OS lock for the lifetime of one Roman process."""

    def __init__(self, name: str = "Local\\RomanAssistant.SingleInstance") -> None:
        self.name = name
        self._handle = None
        self._kernel32 = None
        self._file = None

    def acquire(self) -> bool:
        if os.name == "nt":
            from ctypes import wintypes

            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_mutex = self._kernel32.CreateMutexW
            create_mutex.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
            create_mutex.restype = wintypes.HANDLE
            close_handle = self._kernel32.CloseHandle
            close_handle.argtypes = (wintypes.HANDLE,)
            close_handle.restype = wintypes.BOOL

            handle = create_mutex(None, False, self.name)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
                close_handle(handle)
                return False
            self._handle = handle
            return True

        import fcntl

        token = hashlib.sha256(self.name.encode("utf-8")).hexdigest()[:20]
        path = Path(tempfile.gettempdir()) / f"roman-{token}.lock"
        self._file = path.open("a+b")
        try:
            if path.stat().st_size == 0:
                self._file.write(b"\0")
                self._file.flush()
            self._file.seek(0)
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self._file.close()
            self._file = None
            return False

    def release(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None
        if self._file is not None:
            import fcntl

            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            finally:
                self._file.close()
                self._file = None

    def __enter__(self) -> "SingleInstanceGuard":
        if not self.acquire():
            raise RuntimeError("Roman is already running")
        return self

    def __exit__(self, *_exc) -> None:
        self.release()

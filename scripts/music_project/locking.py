from __future__ import annotations

import math
import os
import time
from pathlib import Path
from typing import BinaryIO, Self


class LedgerBusyError(RuntimeError):
    """Raised when the ledger lock cannot be acquired within the timeout."""


class LedgerLock:
    """Exclusive OS advisory lock for one local filesystem ledger."""

    def __init__(self, root: Path, timeout: float = 5.0) -> None:
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout):
            raise ValueError("lock timeout must be finite and non-negative")
        if timeout < 0:
            raise ValueError("lock timeout must be finite and non-negative")
        self.path = root / ".music-ledger.lock"
        self.timeout = timeout
        self._handle: BinaryIO | None = None

    def __enter__(self) -> Self:
        if not self.path.parent.is_dir():
            raise FileNotFoundError(f"ledger root does not exist: {self.path.parent}")
        handle = self.path.open("a+b")
        if os.name == "nt":
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._acquire(handle)
                self._handle = handle
                return self
            except (BlockingIOError, OSError) as exc:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise LedgerBusyError(
                        f"ledger is busy after {self.timeout:g} seconds"
                    ) from exc
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._handle is None:
            return
        try:
            self._release(self._handle)
        finally:
            self._handle.close()
            self._handle = None

    @staticmethod
    def _acquire(handle: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _release(handle: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

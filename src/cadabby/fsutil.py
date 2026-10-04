"""Filesystem durability utilities: atomic replace, advisory locks, and append-only ledgers.

Conforms to Cadabby Technical Specification §8.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import time
import uuid
from pathlib import Path
from typing import Generator


class VaultConflictError(Exception):
    """Raised when an atomic write detects a concurrent file modification."""


class LockTimeoutError(Exception):
    """Raised when an advisory lock cannot be acquired within the timeout."""


def compute_file_sha256(path: Path | str) -> str:
    """Compute sha256 hex digest of a file's raw bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def compute_bytes_sha256(data: bytes) -> str:
    """Compute sha256 hex digest of in-memory bytes."""
    return hashlib.sha256(data).hexdigest()


def atomic_write(target_path: Path | str, content: str | bytes, encoding: str = "utf-8") -> None:
    """Atomically write content to target_path using same-directory temp file and fsync.

    Guarantees no file is ever left in a truncated or half-written state.
    """
    target = Path(target_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    data = content.encode(encoding) if isinstance(content, str) else content
    tmp_path = target.parent / f".tmp_{os.getpid()}_{uuid.uuid4().hex[:8]}_{target.name}"

    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            total_written = 0
            while total_written < len(data):
                written = os.write(fd, data[total_written:])
                if written == 0:
                    raise OSError("Failed to write bytes to temporary file")
                total_written += written
            os.fsync(fd)
        finally:
            os.close(fd)

        os.replace(tmp_path, target)
    except Exception:
        if tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()
        raise


def atomic_replace_checked(
    target_path: Path | str,
    new_content: str | bytes,
    expected_hash: str | None = None,
    encoding: str = "utf-8",
) -> None:
    """Atomically replace target_path with read-modify-write concurrency safety.

    If expected_hash is provided, checks that the existing file's sha256 matches.
    If the file has changed on disk since expected_hash was computed, raises VaultConflictError.
    """
    target = Path(target_path).resolve()

    if expected_hash is not None and target.exists():
        normalized_expected = expected_hash.removeprefix("sha256:")
        current_hash = compute_file_sha256(target)
        if current_hash != normalized_expected:
            raise VaultConflictError(
                f"VAULT_CONFLICT: {target.name} was modified concurrently on disk "
                f"(expected {normalized_expected[:12]}, found {current_hash[:12]})"
            )

    atomic_write(target, new_content, encoding=encoding)


def append_ledger(ledger_path: Path | str, entry: str, encoding: str = "utf-8") -> None:
    """Append a single line to an append-only ledger using atomic O_APPEND.

    Guarantees atomicity for single-line appends below pipe-buffer size without file locking.
    """
    target = Path(ledger_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    if not entry.endswith("\n"):
        entry += "\n"

    data = entry.encode(encoding)

    fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        total_written = 0
        while total_written < len(data):
            written = os.write(fd, data[total_written:])
            if written == 0:
                raise OSError("Failed to append bytes to ledger")
            total_written += written
    finally:
        os.close(fd)


def _is_process_alive(pid: int) -> bool:
    """Check if process with pid is currently running."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@contextlib.contextmanager
def advisory_lock(
    lock_path: Path | str,
    timeout: float = 60.0,
    stale_age: float = 60.0,
    poll_interval: float = 0.05,
) -> Generator[Path, None, None]:
    """Context manager acquiring an advisory vault lock with stale-lock detection.

    Lock file stores 'pid:start_timestamp'.
    If the lock is older than stale_age or the owning pid is dead, the stale lock is broken.
    """
    target = Path(lock_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    start_time = time.monotonic()
    acquired = False

    while not acquired:
        now = time.time()
        lock_content = f"{os.getpid()}:{now:.3f}\n"

        try:
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                os.write(fd, lock_content.encode("utf-8"))
            finally:
                os.close(fd)
            acquired = True
            break
        except FileExistsError:
            # Inspect existing lock for staleness
            try:
                raw_info = target.read_text("utf-8").strip()
                if ":" in raw_info:
                    pid_str, ts_str = raw_info.split(":", 1)
                    lock_pid = int(pid_str)
                    lock_ts = float(ts_str)

                    is_stale = (now - lock_ts > stale_age) or not _is_process_alive(lock_pid)
                    if is_stale:
                        # Break stale lock
                        with contextlib.suppress(FileNotFoundError, OSError):
                            target.unlink()
                        continue
            except (ValueError, OSError):
                # Unreadable or corrupt lock file; treat as stale
                with contextlib.suppress(FileNotFoundError, OSError):
                    target.unlink()
                continue

            if time.monotonic() - start_time >= timeout:
                raise LockTimeoutError(f"Could not acquire vault lock at {target} within {timeout}s")

            time.sleep(poll_interval)

    try:
        yield target
    finally:
        with contextlib.suppress(FileNotFoundError, OSError):
            target.unlink()

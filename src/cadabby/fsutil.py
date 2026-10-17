"""Filesystem durability utilities: atomic replace, advisory locks, and append-only ledgers.

Conforms to Cadabby Technical Specification §8.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import time
import uuid
from collections.abc import Generator
from pathlib import Path

# Imported rather than defined here: the taxonomy lives in one place (§5.4),
# and every existing `from cadabby.fsutil import VaultConflictError` still
# resolves.
from cadabby.constants import BODY_HASH_PREFIX
from cadabby.errors import LockTimeoutError, VaultConflictError


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
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
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

        # A replace swaps in a new inode, so the target's permission bits would
        # otherwise reset to the temp file's: a 0600 private note or a 0664
        # shared one silently becomes 0644 on every edit.
        with contextlib.suppress(FileNotFoundError):
            os.chmod(tmp_path, target.stat().st_mode & 0o7777)

        os.replace(tmp_path, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise


def check_expected_hash(
    current_hash: str,
    expected_hash: str,
    label: str,
    *,
    where: str = "on disk",
) -> None:
    """Raise VaultConflictError if current_hash disagrees with expected_hash (§8)."""
    expected = expected_hash.removeprefix(BODY_HASH_PREFIX)
    if current_hash != expected:
        raise VaultConflictError(
            f"VAULT_CONFLICT: {label} was modified concurrently {where} "
            f"(expected {expected[:12]}, found {current_hash[:12]})"
        )


def atomic_replace_checked(
    target_path: Path | str,
    new_content: str | bytes,
    expected_hash: str | None = None,
    encoding: str = "utf-8",
) -> None:
    """Atomically replace target_path with read-modify-write concurrency safety.

    If expected_hash is provided, checks that the existing file's sha256 matches.
    If the file has changed on disk since expected_hash was computed, or has been
    deleted, raises VaultConflictError.
    """
    target = Path(target_path).resolve()

    if expected_hash is not None:
        if not target.exists():
            # The caller read this file; it is gone now. Recreating it would undo
            # someone else's delete -- a change since the operation began (§8).
            raise VaultConflictError(f"VAULT_CONFLICT: {target.name} was deleted concurrently")
        check_expected_hash(compute_file_sha256(target), expected_hash, target.name)

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
    except PermissionError:
        # EPERM: the process exists but belongs to another user (a shared vault,
        # a container with a shared pid namespace). That is a live holder.
        return True
    except OSError:
        return False


def _remove_if_unchanged(target: Path, expected: bytes) -> bool:
    """Remove `target` only if it still holds `expected`; True if it is gone afterwards.

    Moving the file aside is atomic, so what we then read is exactly what we
    took. If those are not the bytes we judged -- another process broke the
    stale lock and re-acquired in between -- it is put back where it was. A
    read-then-unlink check could delete a live lock in the gap between calls.
    Raises OSError if the file cannot be moved at all (an unremovable lock).
    """
    grave = target.with_name(f"{target.name}.{uuid.uuid4().hex}.stale")
    try:
        os.rename(target, grave)
    except FileNotFoundError:
        return True
    try:
        if grave.read_bytes() == expected:
            return True
        try:
            os.link(grave, target)  # restore, unless someone has since taken the slot
        except OSError:
            pass
        return False
    finally:
        try:
            grave.unlink()
        except OSError:
            pass


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
    A lock whose content cannot be read as 'pid:timestamp' -- empty because its
    creator has not written it yet, or garbage -- is stale only once its mtime
    is older than stale_age; breaking it at once broke locks still being created.
    """
    target = Path(lock_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    start_time = time.monotonic()
    acquired = False
    lock_bytes = b""

    while not acquired:
        now = time.time()
        lock_bytes = f"{os.getpid()}:{now:.3f}\n".encode("utf-8")

        try:
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                os.write(fd, lock_bytes)
            finally:
                os.close(fd)
            acquired = True
            break
        except FileExistsError:
            # Inspect existing lock for staleness
            try:
                raw_bytes = target.read_bytes()
                raw_info = raw_bytes.decode("utf-8", errors="replace").strip()
                try:
                    pid_str, ts_str = raw_info.split(":", 1)
                    lock_pid = int(pid_str)
                    lock_ts = float(ts_str)
                    is_stale = (now - lock_ts > stale_age) or not _is_process_alive(lock_pid)
                except ValueError:
                    is_stale = now - target.stat().st_mtime > stale_age

                if is_stale:
                    # Break it only if it still holds what we judged, then let
                    # O_EXCL alone decide the winner. A lock we cannot move
                    # falls through to the timeout check rather than spinning.
                    try:
                        _remove_if_unchanged(target, raw_bytes)
                        continue
                    except OSError:
                        pass
            except FileNotFoundError:
                continue  # released between our O_EXCL attempt and the read
            except (ValueError, OSError):
                pass

            if time.monotonic() - start_time >= timeout:
                raise LockTimeoutError(f"Could not acquire vault lock at {target} within {timeout}s")

            time.sleep(poll_interval)

    try:
        yield target
    finally:
        # Only remove the lock this holder wrote, byte for byte: matching the
        # pid prefix alone also matched a later lock by the same process.
        try:
            _remove_if_unchanged(target, lock_bytes)
        except OSError:
            pass

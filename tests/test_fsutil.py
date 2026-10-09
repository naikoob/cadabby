"""Unit tests for fsutil durability primitives, atomic operations, and locks."""

from __future__ import annotations

import os
import stat
import tempfile
import time
import unittest
from pathlib import Path

from cadabby.fsutil import (
    LockTimeoutError,
    VaultConflictError,
    advisory_lock,
    append_ledger,
    atomic_replace_checked,
    atomic_write,
    compute_file_sha256,
)


class TestFsutil(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_atomic_write_and_hash(self):
        target = self.dir / "sub" / "note.md"
        content = "# Hello World\n"
        atomic_write(target, content)

        self.assertTrue(target.exists())
        self.assertEqual(target.read_text("utf-8"), content)
        # Check no temporary files linger
        tmp_files = list(target.parent.glob(".tmp_*"))
        self.assertEqual(len(tmp_files), 0)

    def test_atomic_write_preserves_existing_mode(self):
        """Replacing a file keeps its permission bits rather than resetting them (§8)."""
        target = self.dir / "private.md"
        atomic_write(target, "v1\n")
        os.chmod(target, 0o600)
        atomic_write(target, "v2\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(target.read_text("utf-8"), "v2\n")

    def test_atomic_replace_checked_conflict(self):
        target = self.dir / "conflict.md"
        atomic_write(target, "Version 1\n")
        h1 = compute_file_sha256(target)

        # Modify on disk
        atomic_write(target, "Version 2\n")

        # Attempting replace expecting version 1 hash must raise VaultConflictError
        with self.assertRaises(VaultConflictError):
            atomic_replace_checked(target, "Version 3\n", expected_hash=h1)

        # Content must remain Version 2
        self.assertEqual(target.read_text("utf-8"), "Version 2\n")

        # Passing correct hash succeeds
        h2 = compute_file_sha256(target)
        atomic_replace_checked(target, "Version 3\n", expected_hash=h2)
        self.assertEqual(target.read_text("utf-8"), "Version 3\n")

    def test_append_ledger(self):
        ledger = self.dir / "log.md"
        append_ledger(ledger, "## [2026-10-03] Line 1")
        append_ledger(ledger, "## [2026-10-03] Line 2")

        lines = ledger.read_text("utf-8").splitlines()
        self.assertEqual(lines, ["## [2026-10-03] Line 1", "## [2026-10-03] Line 2"])

    def test_advisory_lock_and_timeout(self):
        lock_file = self.dir / ".cadabby" / "vault.lock"

        # Acquire lock
        with advisory_lock(lock_file):
            self.assertTrue(lock_file.exists())
            # Second attempt within timeout must raise LockTimeoutError
            with (
                self.assertRaises(LockTimeoutError),
                advisory_lock(lock_file, timeout=0.1, poll_interval=0.02),
            ):
                pass

        # Lock file must be unlinked after context exit
        self.assertFalse(lock_file.exists())

    def test_stale_lock_recovery(self):
        lock_file = self.dir / ".cadabby" / "vault.lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        # Write a stale lock: pid=99999999 (dead process), timestamp from an hour ago
        lock_file.write_text(f"99999999:{time.time() - 3600:.3f}\n", "utf-8")

        # Acquiring lock should break stale lock and succeed
        with advisory_lock(lock_file, stale_age=1.0, timeout=0.5):
            self.assertTrue(lock_file.exists())

        self.assertFalse(lock_file.exists())

    def test_unbreakable_stale_lock_times_out(self):
        """C32: a stale lock we cannot remove raises LockTimeoutError, never spins."""
        from unittest import mock

        lock_file = self.dir / ".cadabby" / "vault.lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file.write_text(f"99999999:{time.time() - 3600:.3f}\n", "utf-8")

        started = time.monotonic()
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("read-only")):
            with self.assertRaises(LockTimeoutError):
                with advisory_lock(lock_file, stale_age=1.0, timeout=0.3, poll_interval=0.01):
                    self.fail("acquired a lock that could not be broken")
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertTrue(lock_file.exists())

    def test_lock_release_preserves_foreign_lock(self):
        lock_file = self.dir / ".cadabby" / "vault.lock"
        with advisory_lock(lock_file):
            # Simulate another process having taken over the lock file
            lock_file.write_text("12345:9999999999.000\n", "utf-8")

        # Exiting context manager must not delete the lock since pid doesn't match
        self.assertTrue(lock_file.exists())
        self.assertEqual(lock_file.read_text("utf-8").strip(), "12345:9999999999.000")


if __name__ == "__main__":
    unittest.main()


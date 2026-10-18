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
        # Breaking moves the lock aside first (os.rename), then deletes it; a
        # read-only directory refuses both.
        with (
            mock.patch.object(Path, "unlink", side_effect=PermissionError("read-only")),
            mock.patch("cadabby.fsutil.os.rename", side_effect=PermissionError("read-only")),
        ):
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

    def test_corrupt_lock_recovery(self):
        lock_file = self.dir / ".cadabby" / "vault.lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        # Write corrupted lock content with colon but non-numeric data, old enough to be stale
        lock_file.write_text("corrupt:data\n", "utf-8")
        old = time.time() - 3600
        os.utime(lock_file, (old, old))

        with advisory_lock(lock_file, stale_age=1.0, timeout=0.5):
            self.assertTrue(lock_file.exists())

        self.assertFalse(lock_file.exists())

    def test_fresh_empty_lock_is_not_broken(self):
        """§8, §10 C61: a lock its creator has not written yet is live, not stale."""
        lock_file = self.dir / ".cadabby" / "vault.lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file.write_bytes(b"")
        with self.assertRaises(LockTimeoutError):
            with advisory_lock(lock_file, stale_age=60.0, timeout=0.2, poll_interval=0.01):
                self.fail("broke a lock that was still being created")
        self.assertEqual(lock_file.read_bytes(), b"")

    def test_old_empty_lock_is_broken(self):
        lock_file = self.dir / ".cadabby" / "vault.lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file.write_bytes(b"")
        old = time.time() - 3600
        os.utime(lock_file, (old, old))
        with advisory_lock(lock_file, stale_age=60.0, timeout=0.5):
            self.assertNotEqual(lock_file.read_bytes(), b"")

    def test_eperm_owner_counts_as_alive(self):
        """§8, §10 C61: EPERM from os.kill means the owner exists under another user."""
        from unittest import mock

        from cadabby.fsutil import _is_process_alive

        with mock.patch("cadabby.fsutil.os.kill", side_effect=PermissionError("EPERM")):
            self.assertTrue(_is_process_alive(4242))
        with mock.patch("cadabby.fsutil.os.kill", side_effect=ProcessLookupError("ESRCH")):
            self.assertFalse(_is_process_alive(4242))

        lock_file = self.dir / ".cadabby" / "vault.lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file.write_text(f"4242:{time.time():.3f}\n", "utf-8")
        with mock.patch("cadabby.fsutil.os.kill", side_effect=PermissionError("EPERM")):
            with self.assertRaises(LockTimeoutError):
                with advisory_lock(lock_file, timeout=0.2, poll_interval=0.01):
                    self.fail("broke a lock held by another user's live process")

    def test_remove_if_unchanged_restores_a_swapped_lock(self):
        """§8, §10 C61: a lock replaced between judging and breaking survives the break."""
        from cadabby.fsutil import _remove_if_unchanged

        lock_file = self.dir / ".cadabby" / "vault.lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file.write_bytes(b"777:1.000\n")  # someone else's fresh lock
        self.assertFalse(_remove_if_unchanged(lock_file, b"99999999:0.000\n"))  # judged a different, stale one
        self.assertEqual(lock_file.read_bytes(), b"777:1.000\n")
        self.assertEqual(sorted(p.name for p in lock_file.parent.iterdir()), ["vault.lock"])

        self.assertTrue(_remove_if_unchanged(lock_file, b"777:1.000\n"))
        self.assertFalse(lock_file.exists())

    def test_release_only_removes_the_exact_lock_written(self):
        """A later lock by the same pid (same prefix, different start time) is not released."""
        lock_file = self.dir / ".cadabby" / "vault.lock"
        with advisory_lock(lock_file):
            lock_file.write_text(f"{os.getpid()}:9999999999.000\n", "utf-8")
        self.assertTrue(lock_file.exists())

    def test_atomic_write_cleans_up_on_base_exception(self):
        from unittest.mock import patch

        target = self.dir / "interrupt.md"
        with patch("os.write", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                atomic_write(target, "content")

        self.assertFalse(target.exists())
        tmp_files = list(self.dir.glob(".tmp_*"))
        self.assertEqual(len(tmp_files), 0)


class TestAtomicWriteDurability(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_a_name_near_name_max_can_still_be_rewritten(self):
        target = self.dir / ("n" * 250 + ".md")
        target.write_text("old\n", "utf-8")
        atomic_write(target, "new\n")
        self.assertEqual(target.read_text("utf-8"), "new\n")
        self.assertEqual(list(self.dir.glob(".tmp_*")), [])

    def test_multibyte_name_is_truncated_on_a_character_boundary(self):
        target = self.dir / ("é" * 125 + ".md")  # 254 bytes
        atomic_write(target, "x\n")
        self.assertEqual(target.read_text("utf-8"), "x\n")

    def test_the_parent_directory_is_fsynced_after_the_rename(self):
        from unittest import mock

        import cadabby.fsutil as fsutil

        target = self.dir / "note.md"
        with mock.patch.object(fsutil, "fsync_dir", wraps=fsutil.fsync_dir) as spy:
            atomic_write(target, "x\n")
        spy.assert_called_once_with(target.resolve().parent)


class TestWriteHardening(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp_dir.name).resolve()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_contained_refuses_links_out_and_allows_links_in(self):
        from cadabby.errors import VaultEscapeError
        from cadabby.fsutil import contained

        root = self.dir / "root"
        (root / "real").mkdir(parents=True)
        (root / "inside").symlink_to(root / "real", target_is_directory=True)
        (root / "out").symlink_to(self.dir, target_is_directory=True)
        self.assertEqual(contained(root, root / "inside" / "x.md"), root / "real" / "x.md")
        with self.assertRaises(VaultEscapeError):
            contained(root, root / "out" / "x.md")
        with self.assertRaises(VaultEscapeError):
            contained(root, root / ".." / "x.md")

    def test_the_temp_file_is_never_more_permissive_than_the_target(self):
        """S10: a 0600 secret is not world-readable while it is being rewritten."""
        from unittest import mock

        import cadabby.fsutil as fsutil

        target = self.dir / "secret.json"
        target.write_text("{}", "utf-8")
        os.chmod(target, 0o600)
        modes = []
        real_fsync = os.fsync

        def spy(fd):
            modes.extend(stat.S_IMODE(p.stat().st_mode) for p in self.dir.glob(".tmp_*"))
            return real_fsync(fd)

        with mock.patch.object(fsutil.os, "fsync", side_effect=spy):
            atomic_write(target, '{"k": 1}')
        self.assertTrue(modes)
        self.assertTrue(all(m & 0o077 == 0 for m in modes), [oct(m) for m in modes])
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_a_new_file_gets_the_ordinary_mode(self):
        target = self.dir / "new.md"
        atomic_write(target, "x")
        umask = os.umask(0)
        os.umask(umask)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644 & ~umask)

    def test_a_future_dated_lock_is_stale(self):
        """S9: a planted `1:<far future>` lock never blocks writers."""
        lock = self.dir / "vault.lock"
        lock.write_text(f"1:{time.time() + 10**9:.3f}\n", "utf-8")
        start = time.monotonic()
        with advisory_lock(lock, timeout=0.5):
            pass
        self.assertLess(time.monotonic() - start, 0.5)


if __name__ == "__main__":
    unittest.main()


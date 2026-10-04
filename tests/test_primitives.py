"""Unit tests for fsutil durability primitives and vault discovery/mapping."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from cadabby.constants import FILE_CONFIG
from cadabby.fsutil import (
    LockTimeoutError,
    VaultConflictError,
    advisory_lock,
    append_ledger,
    atomic_replace_checked,
    atomic_write,
    compute_file_sha256,
)
from cadabby.vault import (
    Vault,
    cid_to_path,
    find_vault_root,
    load_vault_config,
    path_to_cid,
    path_to_layer,
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
            with self.assertRaises(LockTimeoutError):
                with advisory_lock(lock_file, timeout=0.1, poll_interval=0.02):
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


class TestVault(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_find_vault_root_walkup(self):
        # Create vault structure
        (self.dir / FILE_CONFIG).write_text('{"vault_name": "test-vault"}', "utf-8")
        nested_dir = self.dir / "wiki" / "concepts" / "deep"
        nested_dir.mkdir(parents=True, exist_ok=True)

        found = find_vault_root(nested_dir)
        self.assertEqual(found, self.dir.resolve())

    def test_load_vault_config_deep_merge(self):
        cfg_file = self.dir / FILE_CONFIG
        cfg_file.write_text(
            '{"vault_name": "custom", "ranking": {"trust": {"human-reviewed": 3.0}}}',
            "utf-8",
        )
        cfg = load_vault_config(self.dir)
        self.assertEqual(cfg["vault_name"], "custom")
        # Custom value applied
        self.assertEqual(cfg["ranking"]["trust"]["human-reviewed"], 3.0)
        # Default value preserved
        self.assertEqual(cfg["ranking"]["trust"]["machine-confirmed"], 1.2)

    def test_path_and_cid_mappings(self):
        # Wiki note
        wiki_rel = "wiki/concepts/Epistemic-Trust-Tiers.md"
        cid = path_to_cid(wiki_rel)
        self.assertEqual(cid, "wiki/concepts/Epistemic-Trust-Tiers")
        self.assertEqual(cid_to_path(cid), wiki_rel)
        self.assertEqual(path_to_layer(wiki_rel), "wiki")

        # Raw file
        raw_rel = "raw/papers/attention.pdf"
        self.assertEqual(path_to_cid(raw_rel), raw_rel)
        self.assertEqual(cid_to_path(raw_rel), raw_rel)
        self.assertEqual(path_to_layer(raw_rel), "raw")


if __name__ == "__main__":
    unittest.main()

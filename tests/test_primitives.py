"""Unit tests for fsutil durability primitives and vault discovery/mapping."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from cadabby.cache import VaultCache
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
from cadabby.graph import LinkTargetIndex, resolve_link_target
from cadabby.indexer import rotate_vault_log
from cadabby.vault import (
    Vault,
    VaultConfigError,
    cid_to_path,
    default_vault_config,
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

    def test_lock_release_preserves_foreign_lock(self):
        lock_file = self.dir / ".cadabby" / "vault.lock"
        with advisory_lock(lock_file):
            # Simulate another process having taken over the lock file
            lock_file.write_text("12345:9999999999.000\n", "utf-8")

        # Exiting context manager must not delete the lock since pid doesn't match
        self.assertTrue(lock_file.exists())
        self.assertEqual(lock_file.read_text("utf-8").strip(), "12345:9999999999.000")


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

    def test_load_vault_config_malformed_json_raises(self):
        cfg_file = self.dir / FILE_CONFIG
        cfg_file.write_text('{"vault_name": "broken", broken json...}', "utf-8")
        with self.assertRaises(VaultConfigError):
            load_vault_config(self.dir)

    def test_load_vault_config_nondict_raises(self):
        cfg_file = self.dir / FILE_CONFIG
        cfg_file.write_text('["not", "a", "dict"]', "utf-8")
        with self.assertRaises(VaultConfigError):
            load_vault_config(self.dir)

    def test_load_vault_config_mistyped_value_raises(self):
        # A null or mistyped value must fail at the config file rather than as
        # an opaque TypeError inside whichever consumer reads the key.
        cfg_file = self.dir / FILE_CONFIG
        cases = [
            ('{"raw_text_extensions": null}', "raw_text_extensions"),
            ('{"identities": null}', "identities"),
            ('{"log_rotate_bytes": "262144"}', "log_rotate_bytes"),
            ('{"log_rotate_bytes": true}', "log_rotate_bytes"),
            ('{"ranking": null}', "ranking"),
            ('{"ranking": {"trust": null}}', "ranking.trust"),
        ]
        for content, key in cases:
            with self.subTest(key=key):
                cfg_file.write_text(content, "utf-8")
                with self.assertRaises(VaultConfigError) as ctx:
                    load_vault_config(self.dir)
                self.assertIn(f"'{key}'", str(ctx.exception))

    def test_load_vault_config_passes_through_unknown_keys(self):
        # Type checking is scoped to keys with defaults, so forward-compatible
        # additions are not rejected by an older build.
        cfg_file = self.dir / FILE_CONFIG
        cfg_file.write_text('{"future_key": null, "log_rotate_bytes": 999}', "utf-8")
        cfg = load_vault_config(self.dir)
        self.assertIsNone(cfg["future_key"])
        self.assertEqual(cfg["log_rotate_bytes"], 999)

    def test_every_default_config_key_is_read_by_something(self):
        """A shipped key that no code reads is a promise the engine breaks.

        The pass-through above is what makes this necessary: unknown keys do
        not raise, so a key the engine ships but never consults behaves
        exactly like one it does, and nothing tells the user their setting is
        inert. `obsidian.materialize_trust_tags` lived that way through
        0.3.1 -- defaulted, type-checked, documented in two spec sections,
        and read by no code path -- and was removed rather than implemented
        (§7.5).
        """
        src = Path(__file__).resolve().parent.parent / "src" / "cadabby"
        sources = "\n".join(p.read_text("utf-8") for p in src.rglob("*.py"))

        # `schema` is a format marker written for a future migration to read,
        # not a knob, so it has no consumer by design. It is listed here
        # rather than skipped silently because the migration story it implies
        # does not exist yet either.
        exempt = {"schema"}

        for key in default_vault_config("v"):
            if key in exempt:
                continue
            with self.subTest(key=key):
                self.assertIn(
                    f'config.get("{key}"',
                    sources,
                    f"'{key}' is shipped in .cadabby.json defaults but nothing reads it",
                )

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

        # Vault.rel_path resolution regardless of cwd
        vault = Vault(self.dir)
        old_cwd = os.getcwd()
        try:
            # Change cwd to /tmp to ensure cwd != vault.root
            os.chdir(tempfile.gettempdir())
            # Relative path within vault
            self.assertEqual(vault.rel_path("wiki/concepts/Foo.md"), "wiki/concepts/Foo.md")
            # Absolute path within vault
            abs_note = self.dir / "wiki" / "concepts" / "Foo.md"
            self.assertEqual(vault.rel_path(abs_note), "wiki/concepts/Foo.md")
        finally:
            os.chdir(old_cwd)


class TestGraphAndLinkResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault_dir = Path(self.tmp.name) / "vault"
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        self.vault = Vault(self.vault_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_link_target_index(self):
        cids = [
            "wiki/concepts/Epistemic-Trust-Tiers",
            "wiki/entities/SQLite",
            "wiki/guides/Getting-Started",
            "raw/vaswani2017.pdf",
        ]
        resolver = LinkTargetIndex(cids)

        # 1. Exact match
        self.assertEqual(resolver.resolve("wiki/concepts/Epistemic-Trust-Tiers"), "wiki/concepts/Epistemic-Trust-Tiers")
        self.assertEqual(resolver.resolve("raw/vaswani2017.pdf"), "raw/vaswani2017.pdf")

        # 2. Match without "wiki/" prefix
        self.assertEqual(resolver.resolve("concepts/Epistemic-Trust-Tiers"), "wiki/concepts/Epistemic-Trust-Tiers")
        self.assertEqual(resolver.resolve("entities/SQLite"), "wiki/entities/SQLite")

        # 3. Path suffix match
        self.assertEqual(resolver.resolve("guides/Getting-Started"), "wiki/guides/Getting-Started")

        # 4. Note stem match
        self.assertEqual(resolver.resolve("Epistemic-Trust-Tiers"), "wiki/concepts/Epistemic-Trust-Tiers")
        self.assertEqual(resolver.resolve("SQLite"), "wiki/entities/SQLite")

        # 5. Non-existent target returns None
        self.assertIsNone(resolver.resolve("NonExistentNote"))

        # Verify resolve_link_target backward-compatible wrapper
        self.assertEqual(resolve_link_target("SQLite", cids), "wiki/entities/SQLite")

    def test_cache_scan_zero_change_skips_link_resolution(self):
        note_a = self.vault.wiki_dir / "concepts" / "Note-A.md"
        note_a.parent.mkdir(parents=True, exist_ok=True)
        note_a.write_text(
            "---\ntype: concept\ntitle: Note A\ndescription: Test\nstatus: active\n---\n# Note A\nLinks to [[Note-B]].\n",
            "utf-8",
        )

        note_b = self.vault.wiki_dir / "concepts" / "Note-B.md"
        note_b.write_text(
            "---\ntype: concept\ntitle: Note B\ndescription: Test\nstatus: active\n---\n# Note B\nTarget.\n",
            "utf-8",
        )

        with VaultCache(self.vault) as cache:
            # First scan: populates DB and resolves links
            ins, _, _, total = cache.scan()
            self.assertEqual(ins, 2)
            self.assertEqual(total, 2)
            conn = cache.get_connection()
            row = conn.execute("SELECT target_cid FROM links WHERE target_raw = 'Note-B';").fetchone()
            self.assertEqual(row["target_cid"], "wiki/concepts/Note-B")

            # Second scan: no changes on disk
            ins2, upd2, deleted2, total2 = cache.scan()
            self.assertEqual(ins2, 0)
            self.assertEqual(upd2, 0)
            self.assertEqual(deleted2, 0)
            self.assertEqual(total2, 2)

    def test_rotate_vault_log_duplicate_headers(self):
        # Configure small rotation threshold
        self.vault.config["log_rotate_bytes"] = 10

        # Create active log
        self.vault.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.vault.log_path.write_text("# Activity Ledger (2026)\n\nEntry 1 with extra padding\n", "utf-8")

        # First rotation creates 2026.md
        rot1 = rotate_vault_log(self.vault)
        self.assertIsNotNone(rot1)
        assert rot1 is not None
        self.assertTrue(rot1.exists())
        self.assertEqual(rot1.read_text("utf-8").count("# Activity Ledger (2026)"), 1)

        # Append more entries to active log and rotate again into existing 2026.md
        self.vault.log_path.write_text("# Activity Ledger (2026)\n\nEntry 2 with extra padding\n", "utf-8")
        rot2 = rotate_vault_log(self.vault)
        self.assertEqual(rot1, rot2)

        # Rotated file should still only have exactly one top-level title header
        content = rot2.read_text("utf-8")
        self.assertEqual(content.count("# Activity Ledger (2026)"), 1)
        self.assertIn("Entry 1", content)
        self.assertIn("Entry 2", content)

    def test_vault_cache_search_default_ranking_multipliers(self):
        # Even with empty config, search applies default ranking multipliers without error
        note = self.vault.wiki_dir / "concepts" / "SearchTest.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(
            "---\ntype: concept\ntitle: Search Test\ndescription: Testing ranking\nstatus: active\n---\n# Search Test\nQuery target.\n",
            "utf-8",
        )
        with VaultCache(self.vault) as cache:
            results = cache.search("Query")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].title, "Search Test")
            self.assertGreater(results[0].score, 0.0)

    def test_vault_cache_search_sanitized_ranking_cases(self):
        # Even with custom malicious single quotes or non-numeric values in ranking config, search works
        note = self.vault.wiki_dir / "concepts" / "SanitizeTest.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(
            "---\ntype: concept\ntitle: Sanitize Test\ndescription: Testing ranking\nstatus: active\n---\n# Sanitize Test\nQuery target.\n",
            "utf-8",
        )
        self.vault.config["ranking"] = {
            "trust": {"malicious' OR 1=1 --": 2.0, "bad_num": "not_a_float"},
            "status": {"active'; DROP TABLE notes; --": 1.5},
        }
        with VaultCache(self.vault) as cache:
            cache.scan()
            results = cache.search("Query")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].title, "Sanitize Test")

    def test_vault_cache_search_malformed_ranking_config(self):
        # Non-finite, empty, null, and non-mapping ranking config must neither
        # produce invalid SQL nor raise; search falls back to neutral multipliers.
        note = self.vault.wiki_dir / "concepts" / "NonFiniteTest.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(
            "---\ntype: concept\ntitle: Non Finite Test\ndescription: Testing ranking\nstatus: active\n---\n# Non Finite Test\nQuery target.\n",
            "utf-8",
        )
        for ranking in (
            {"trust": {"human-reviewed": 1e999}, "status": {"active": float("nan")}},
            {"trust": {}, "status": {}},
            {"trust": None, "status": None},
            None,
            "not-a-mapping",
        ):
            with self.subTest(ranking=ranking):
                self.vault.config["ranking"] = ranking
                with VaultCache(self.vault) as cache:
                    cache.scan()
                    results = cache.search("Query")
                    self.assertEqual(len(results), 1)
                    self.assertEqual(results[0].title, "Non Finite Test")

    def test_find_vault_root_rejects_invalid_env_vault(self):
        # A set-but-invalid CADABBY_VAULT fails loudly rather than silently
        # falling back to the cwd or resolving to an enclosing ancestor vault.
        marked_ancestor = self.vault_dir / "marked_ancestor"
        unmarked_child = marked_ancestor / "child"
        unmarked_child.mkdir(parents=True, exist_ok=True)
        (marked_ancestor / FILE_CONFIG).write_text('{"vault_name": "ancestor"}', "utf-8")

        for label, env_value in (
            ("nonexistent", str(self.vault_dir / "gone")),
            ("unmarked child of a vault", str(unmarked_child)),
        ):
            with self.subTest(case=label), mock.patch.dict(os.environ, {"CADABBY_VAULT": env_value}):
                with self.assertRaises(FileNotFoundError) as ctx:
                    find_vault_root(None)
                self.assertIn("CADABBY_VAULT", str(ctx.exception))

    def test_find_vault_root_precedence(self):
        env_vault = self.vault_dir / "env_vault"
        env_vault.mkdir(parents=True, exist_ok=True)
        (env_vault / FILE_CONFIG).write_text('{"vault_name": "env"}', "utf-8")

        explicit_vault = self.vault_dir / "explicit_vault"
        explicit_vault.mkdir(parents=True, exist_ok=True)
        (explicit_vault / FILE_CONFIG).write_text('{"vault_name": "explicit"}', "utf-8")

        with mock.patch.dict(os.environ, {"CADABBY_VAULT": str(env_vault)}):
            # When explicit path is passed, it must take precedence over CADABBY_VAULT
            found_explicit = find_vault_root(explicit_vault)
            self.assertEqual(found_explicit, explicit_vault.resolve())

            # When explicit path is None, CADABBY_VAULT is used
            found_env = find_vault_root(None)
            self.assertEqual(found_env, env_vault.resolve())


if __name__ == "__main__":
    unittest.main()


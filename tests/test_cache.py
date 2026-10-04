"""Unit tests for SQLite ephemeral cache, FTS5 retraction, deletion reconciliation, and ranking.

Conforms to acceptance criteria from §4 and §10.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from cadabby.cache import VaultCache
from cadabby.vault import Vault


class TestVaultCache(unittest.TestCase):
    def setUp(self):
        # Create a temporary working copy of examples/demo-vault
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.vault_root = Path(self.tmp_dir.name) / "demo-vault"
        demo_src = Path(__file__).resolve().parent.parent / "examples" / "demo-vault"
        shutil.copytree(demo_src, self.vault_root, ignore=shutil.ignore_patterns(".cadabby", "*.pyc"))
        self.vault = Vault(self.vault_root)
        self.cache = VaultCache(self.vault)

    def tearDown(self):
        self.cache.close()
        self.tmp_dir.cleanup()

    def test_initial_scan_and_trust_tiers(self):
        inserted, updated, deleted, total = self.cache.scan()
        self.assertEqual(inserted, 7)  # 4 wiki notes + 3 raw files
        self.assertEqual(deleted, 0)
        self.assertEqual(total, 7)

        conn = self.cache.get_connection()

        # Check trust tiers
        cur = conn.execute("SELECT cid, trust_tier, status FROM notes WHERE layer = 'wiki' ORDER BY cid;")
        rows = {r["cid"]: (r["trust_tier"], r["status"]) for r in cur.fetchall()}

        self.assertEqual(rows["wiki/concepts/Epistemic-Trust-Tiers"], ("human-reviewed", "active"))
        self.assertEqual(rows["wiki/entities/SQLite"], ("machine-confirmed", "evergreen"))
        self.assertEqual(rows["wiki/concepts/Flash-Attention"], ("unverified", "active"))
        self.assertEqual(rows["wiki/comparisons/SQLite-vs-DuckDB"], ("unverified", "deprecated"))

        # Verify FTS5 integrity
        self.assertTrue(self.cache.check_fts_integrity())

    def test_disposable_cache_reproducibility(self):
        # Run scan and perform search
        self.cache.scan()
        results1 = self.cache.search("SQLite")
        self.cache.close()

        # Delete the .cadabby directory completely
        dot_cadabby = self.vault.dot_cadabby_dir
        self.assertTrue(dot_cadabby.exists())
        shutil.rmtree(dot_cadabby)
        self.assertFalse(dot_cadabby.exists())

        # Re-run search: must reconstruct cache and produce identical result order
        cache2 = VaultCache(self.vault)
        results2 = cache2.search("SQLite")
        cache2.close()

        self.assertEqual(len(results1), len(results2))
        for r1, r2 in zip(results1, results2):
            self.assertEqual(r1.cid, r2.cid)
            self.assertAlmostEqual(r1.score, r2.score, places=4)

    def test_deletion_reconciliation_and_fts_retraction(self):
        self.cache.scan()
        conn = self.cache.get_connection()

        # Note exists in FTS
        cur = conn.execute("SELECT COUNT(*) FROM notes_fts WHERE notes_fts MATCH 'DuckDB';")
        self.assertEqual(cur.fetchone()[0], 1)

        # Delete note file from disk
        note_path = self.vault.wiki_dir / "comparisons" / "SQLite-vs-DuckDB.md"
        note_path.unlink()

        # Next scan must reconcile deletion
        ins, upd, deleted, total = self.cache.scan()
        self.assertEqual(deleted, 1)

        # Verify term is retracted from FTS
        cur = conn.execute("SELECT COUNT(*) FROM notes_fts WHERE notes_fts MATCH 'DuckDB';")
        self.assertEqual(cur.fetchone()[0], 0)

        # Verify FTS5 integrity check passes after retraction
        self.assertTrue(self.cache.check_fts_integrity())

    def test_drift_downgrades_to_stale_verified(self):
        self.cache.scan()
        conn = self.cache.get_connection()

        # Initial state: human-reviewed
        cur = conn.execute("SELECT trust_tier FROM notes WHERE cid = 'wiki/concepts/Epistemic-Trust-Tiers';")
        self.assertEqual(cur.fetchone()[0], "human-reviewed")

        # Edit note body on disk without updating 'of:' hash
        note_path = self.vault.wiki_dir / "concepts" / "Epistemic-Trust-Tiers.md"
        content = note_path.read_text("utf-8")
        modified = content + "\n\nExtra drifted paragraph added by an agent."
        note_path.write_text(modified, "utf-8")

        # Scan should re-index and detect content drift
        self.cache.scan()
        cur = conn.execute("SELECT trust_tier FROM notes WHERE cid = 'wiki/concepts/Epistemic-Trust-Tiers';")
        self.assertEqual(cur.fetchone()[0], "stale-verified")

    def test_epistemic_ranking_boosts_and_status_penalty(self):
        self.cache.scan()
        results = self.cache.search("SQLite")

        # Both 'wiki/entities/SQLite' and 'wiki/comparisons/SQLite-vs-DuckDB' match 'SQLite'
        # 'wiki/entities/SQLite' is machine-confirmed (1.2x) and evergreen (1.0x)
        # 'wiki/comparisons/SQLite-vs-DuckDB' is deprecated (0.4x penalty)
        cids = [r.cid for r in results]
        self.assertIn("wiki/entities/SQLite", cids)
        self.assertIn("wiki/comparisons/SQLite-vs-DuckDB", cids)

        sqlite_res = next(r for r in results if r.cid == "wiki/entities/SQLite")
        duckdb_res = next(r for r in results if r.cid == "wiki/comparisons/SQLite-vs-DuckDB")

        # Positive normalized score
        self.assertGreater(sqlite_res.score, 0)
        self.assertGreater(duckdb_res.score, 0)

        # Deprecated note should be heavily demoted
        self.assertGreater(sqlite_res.score, duckdb_res.score)


if __name__ == "__main__":
    unittest.main()

"""Unit tests for SQLite ephemeral cache, FTS5 retraction, deletion reconciliation, and ranking.

Conforms to acceptance criteria from §4 and §10.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from cadabby.cache import VaultCache, sanitize_fts5_query
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
        inserted, _, deleted, total = self.cache.scan()
        self.assertEqual(inserted, 9)  # 6 wiki notes (including 2 MOCs) + 3 raw files
        self.assertEqual(deleted, 0)
        self.assertEqual(total, 9)

        conn = self.cache.get_connection()

        # Check trust tiers
        cur = conn.execute("SELECT cid, trust_tier, status FROM notes WHERE layer = 'wiki' ORDER BY cid;")
        rows = {r["cid"]: (r["trust_tier"], r["status"]) for r in cur.fetchall()}

        self.assertEqual(rows["wiki/Epistemic-Trust-Tiers"], ("human-reviewed", "active"))
        self.assertEqual(rows["wiki/SQLite"], ("machine-confirmed", "evergreen"))
        self.assertEqual(rows["wiki/Flash-Attention"], ("unverified", "active"))
        self.assertEqual(rows["wiki/SQLite-vs-DuckDB"], ("unverified", "deprecated"))
        self.assertEqual(rows["wiki/AI-Systems-MOC"], ("unverified", "active"))
        self.assertEqual(rows["wiki/Storage-MOC"], ("unverified", "active"))

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
        note_path = self.vault.wiki_dir / "SQLite-vs-DuckDB.md"
        note_path.unlink()

        # Next scan must reconcile deletion
        _, _, deleted, _ = self.cache.scan()
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
        cur = conn.execute("SELECT trust_tier FROM notes WHERE cid = 'wiki/Epistemic-Trust-Tiers';")
        self.assertEqual(cur.fetchone()[0], "human-reviewed")

        # Edit note body on disk without updating 'of:' hash
        note_path = self.vault.wiki_dir / "Epistemic-Trust-Tiers.md"
        content = note_path.read_text("utf-8")
        modified = content + "\n\nExtra drifted paragraph added by an agent."
        note_path.write_text(modified, "utf-8")

        # Scan should re-index and detect content drift
        self.cache.scan()
        cur = conn.execute("SELECT trust_tier FROM notes WHERE cid = 'wiki/Epistemic-Trust-Tiers';")
        self.assertEqual(cur.fetchone()[0], "stale-verified")

    def test_epistemic_ranking_boosts_and_status_penalty(self):
        self.cache.scan()
        results = self.cache.search("SQLite")

        # Both 'wiki/SQLite' and 'wiki/SQLite-vs-DuckDB' match 'SQLite'
        # 'wiki/SQLite' is machine-confirmed (1.2x) and evergreen (1.0x)
        # 'wiki/SQLite-vs-DuckDB' is deprecated (0.4x penalty)
        cids = [r.cid for r in results]
        self.assertIn("wiki/SQLite", cids)
        self.assertIn("wiki/SQLite-vs-DuckDB", cids)

        sqlite_res = next(r for r in results if r.cid == "wiki/SQLite")
        duckdb_res = next(r for r in results if r.cid == "wiki/SQLite-vs-DuckDB")

        # Positive normalized score
        self.assertGreater(sqlite_res.score, 0)
        self.assertGreater(duckdb_res.score, 0)

        # Deprecated note should be heavily demoted
        self.assertGreater(sqlite_res.score, duckdb_res.score)

    def test_search_tag_filtering(self):
        self.cache.scan()

        # Search matching tag
        results = self.cache.search("SQLite", tag="database")
        cids = [r.cid for r in results]
        self.assertIn("wiki/SQLite", cids)

        # Search non-matching tag
        results_none = self.cache.search("SQLite", tag="nonexistent-tag")
        self.assertEqual(len(results_none), 0)

        # Test SearchResult.to_dict()
        res_dict = results[0].to_dict()
        self.assertEqual(res_dict["cid"], results[0].cid)
        self.assertIn("score", res_dict)
        self.assertIn("snippet", res_dict)

    def test_search_tag_filter_is_whole_tag_and_case_folded(self):
        """`--tag` matches whole canonical tags only (§3.1, §4.2)."""
        self.cache.scan()

        # Stored tags are canonically lowercase, so the probe is folded to match.
        self.assertIn("wiki/SQLite", [r.cid for r in self.cache.search("SQLite", tag="DataBase")])

        # A prefix of a hyphenated tag is not a match: the demo note carries
        # 'c-library', and a substring probe must not satisfy the filter.
        self.assertIn("wiki/SQLite", [r.cid for r in self.cache.search("SQLite", tag="c-library")])
        self.assertEqual(self.cache.search("SQLite", tag="library"), [])

    def test_sanitize_fts5_query(self):
        # Bare hyphenated words get quoted
        self.assertEqual(sanitize_fts5_query("B-tree"), '"B-tree"')
        self.assertEqual(sanitize_fts5_query("LSM B-tree write heavy"), 'LSM "B-tree" write heavy')

        # Quoted phrases are preserved
        self.assertEqual(sanitize_fts5_query('"write heavy" B-tree'), '"write heavy" "B-tree"')

        # Dangling quote is closed
        self.assertEqual(sanitize_fts5_query('"write heavy'), '"write heavy"')

        # Wildcards
        self.assertEqual(sanitize_fts5_query("consens*"), "consens*")
        self.assertEqual(sanitize_fts5_query("write-heav*"), '"write-heav"*')

        # Punctuation and standalone operators stripped
        self.assertEqual(sanitize_fts5_query("---"), "")
        self.assertEqual(sanitize_fts5_query("AND"), "")
        self.assertEqual(sanitize_fts5_query("SQLite AND"), "SQLite")
        self.assertEqual(sanitize_fts5_query("SQLite OR DuckDB"), "SQLite OR DuckDB")

    def test_hyphenated_and_special_queries(self):
        self.cache.scan()

        # Hyphenated searches must not throw sqlite3.OperationalError
        results_hyphen = self.cache.search("SQLite-vs-DuckDB")
        self.assertGreater(len(results_hyphen), 0)
        self.assertEqual(results_hyphen[0].cid, "wiki/SQLite-vs-DuckDB")

        # Hyphenated phrase queries
        results_btree = self.cache.search("LSM B-tree write heavy")
        self.assertIsInstance(results_btree, list)

        # Empty or punctuation-only query returns empty list cleanly
        self.assertEqual(self.cache.search("---"), [])
        self.assertEqual(self.cache.search(""), [])

    def test_cache_target_resolution(self):
        self.cache.scan()

        # Bare stem
        self.assertEqual(self.cache.resolve_target("SQLite"), "wiki/SQLite")
        self.assertEqual(self.cache.resolve_target("Flash-Attention"), "wiki/Flash-Attention")

        # Exact CID
        self.assertEqual(self.cache.resolve_target("wiki/Flash-Attention"), "wiki/Flash-Attention")

        # Nonexistent
        self.assertIsNone(self.cache.resolve_target("NonExistentStem"))


class TestRawSourceSearch(unittest.TestCase):
    """§2.4/§4.2. Raw sources are searchable, in their own corpus.

    Both halves matter. Storing a raw file's text without indexing it made
    `raw/` a write-only surface: an agent searching for a phrase sitting in a
    collected transcript got nothing back and concluded the vault did not have
    it. Indexing raw into the *curated* table fixes that and breaks something
    worse -- see test_raw_corpus_does_not_distort_curated_ranking.
    """

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name) / "v"
        (self.root / "raw").mkdir(parents=True)
        (self.root / "wiki").mkdir(parents=True)
        (self.root / ".cadabby.json").write_text('{"vault_name": "v"}\n', encoding="utf-8")
        self.vault = Vault(self.root)
        self.cache = VaultCache(self.vault)

    def tearDown(self):
        self.cache.close()
        self.tmp_dir.cleanup()

    def _wiki(self, name: str, body: str):
        (self.root / "wiki" / f"{name}.md").write_text(
            f"---\ntitle: {name}\ntype: concept\nstatus: draft\n---\n\n{body}\n", encoding="utf-8"
        )

    def _raw(self, name: str, body: str):
        (self.root / "raw" / name).write_text(body, encoding="utf-8")

    def test_raw_full_text_is_reachable_only_through_the_raw_domain(self):
        self._raw("transcript.md", "The alpaca protocol was discussed at length.")
        self.cache.scan()

        self.assertEqual(self.cache.search("alpaca"), [], "raw must not leak into unfiltered results")
        hits = self.cache.search("alpaca", domain="raw")
        self.assertEqual([h.cid for h in hits], ["raw/transcript.md"])

    def test_extension_not_in_the_list_is_indexed_by_name_but_not_by_content(self):
        """The config key has to actually decide something (§2.4)."""
        self._raw("notes.org", "The alpaca protocol in org-mode.")
        self.cache.scan()
        self.assertEqual(self.cache.search("alpaca", domain="raw"), [])
        self.assertEqual([h.cid for h in self.cache.search("notes", domain="raw")], ["raw/notes.org"])

    def test_raw_text_extensions_replaces_rather_than_extends_the_defaults(self):
        self._raw("transcript.md", "The alpaca protocol was discussed.")
        self._raw("notes.org", "The alpaca protocol in org-mode.")
        (self.root / ".cadabby.json").write_text('{"vault_name": "v", "raw_text_extensions": [".org"]}\n', "utf-8")
        self.cache.close()
        self.cache = VaultCache(Vault(self.root))
        self.cache.scan(force=True)

        found = {h.cid for h in self.cache.search("alpaca", domain="raw")}
        self.assertEqual(found, {"raw/notes.org"}, "replacement semantics: .md drops out when not listed")

    def test_binary_sources_are_findable_by_filename(self):
        self._raw("alpaca-paper.pdf", "%PDF-1.4 binary junk")
        self.cache.scan()
        self.assertEqual([h.cid for h in self.cache.search("alpaca", domain="raw")], ["raw/alpaca-paper.pdf"])
        self.assertEqual(self.cache.search("binary", domain="raw"), [], "binary content must not be indexed")

    def test_raw_corpus_does_not_distort_curated_ranking(self):
        """The reason raw lives in its own FTS table (§4.2).

        BM25 scores a document against its corpus. Thirty raw transcripts
        repeating a term drive its inverse document frequency to zero, and
        because the trust and status multipliers (§4.4) are applied *to* the
        BM25 score, a flattened score silently disables epistemic ranking.
        A WHERE filter cannot prevent this: filtering happens after scoring.
        """
        for i in range(20):
            self._wiki(f"N{i}", "General prose about systems. " + ("The cache layer." if i < 2 else ""))
        self.cache.scan()
        baseline = self.cache.search("cache")
        self.assertTrue(baseline, "precondition: the term matches curated notes")

        for i in range(30):
            self._raw(f"t{i}.txt", "cache " * 200)
        self.cache.scan()
        after = self.cache.search("cache")

        self.assertEqual([h.cid for h in after], [h.cid for h in baseline], "raw must not reorder curated results")
        for before, now in zip(baseline, after):
            self.assertAlmostEqual(before.score, now.score, places=6, msg="raw must not rescore curated results")
        self.assertGreater(after[0].score, 0.0, "a flattened score would neutralize trust-tier boosting")

    def test_moving_a_file_between_layers_retracts_from_the_right_index(self):
        """Two indexes give retraction a second way to corrupt itself (§4.2)."""
        self._raw("Topic.md", "distinctive alpaca phrasing")
        self.cache.scan()
        self.assertTrue(self.cache.search("alpaca", domain="raw"))

        (self.root / "raw" / "Topic.md").unlink()
        self._wiki("Topic", "distinctive alpaca phrasing")
        self.cache.scan()

        self.assertEqual([h.cid for h in self.cache.search("alpaca")], ["wiki/Topic"])
        self.assertEqual(self.cache.search("alpaca", domain="raw"), [], "stale raw entry survived the move")
        self.assertTrue(self.cache.check_fts_integrity())

    def test_editing_a_raw_file_retracts_its_old_terms(self):
        self._raw("transcript.md", "the original alpaca phrasing")
        self.cache.scan()
        self._raw("transcript.md", "replaced with vicuna phrasing")
        self.cache.scan(force=True)

        self.assertEqual(self.cache.search("alpaca", domain="raw"), [], "old terms were not retracted")
        self.assertTrue(self.cache.search("vicuna", domain="raw"))
        self.assertTrue(self.cache.check_fts_integrity())

    def test_deleting_a_raw_file_removes_it_from_the_raw_index(self):
        self._raw("transcript.md", "the alpaca phrasing")
        self.cache.scan()
        (self.root / "raw" / "transcript.md").unlink()
        self.cache.scan()

        self.assertEqual(self.cache.search("alpaca", domain="raw"), [])
        self.assertTrue(self.cache.check_fts_integrity())


if __name__ == "__main__":
    unittest.main()

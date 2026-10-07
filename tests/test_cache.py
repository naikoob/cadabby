"""Unit tests for SQLite ephemeral cache, FTS5 retraction, deletion reconciliation, and ranking.

Conforms to acceptance criteria from §4 and §10.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from cadabby.cache import VaultCache, sanitize_fts5_query
from cadabby.indexer import generate_index_markdown
from cadabby.vault import Vault
from tests.helpers import copy_demo_vault


class TestVaultCache(unittest.TestCase):
    def setUp(self):
        # Create a temporary working copy of examples/demo-vault
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.vault_root = copy_demo_vault(Path(self.tmp_dir.name) / "demo-vault")
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

    def _drift(self, stem):
        """Edit a verified note's body so its attestation stops binding."""
        path = self.vault.wiki_dir / f"{stem}.md"
        path.write_text(path.read_text("utf-8") + "\n\nDrifted paragraph.\n", "utf-8")

    def test_status_counts_verification_debt(self):
        """§10 C2. The tier is already asserted via SQL; the payload is not.

        `vault_status` is how an agent learns it owes verification work, so
        the count reaching the payload is the part that matters to a caller.
        """
        self.cache.scan()
        self.assertEqual(self.cache.get_status()["verification_debt"], 0)

        self._drift("Epistemic-Trust-Tiers")
        self.cache.scan()
        self.assertEqual(self.cache.get_status()["verification_debt"], 1)

        self._drift("SQLite")
        self.cache.scan()
        self.assertEqual(self.cache.get_status()["verification_debt"], 2)

    def test_status_debt_agrees_with_the_index_report(self):
        """One number, two renderings, two different queries (§2.5, §5.2).

        `get_status` counts `trust_tier = 'stale-verified'` over non-raw rows
        while the gap report adds `parse_error IS NULL`. The extra clause is
        currently redundant -- an unparseable note is stored with a NULL tier
        -- so the two agree today. This holds them together if either query
        moves, because a status count that disagrees with the file the user
        is looking at is worse than either number alone.
        """
        self._drift("Epistemic-Trust-Tiers")
        self._drift("SQLite")
        self.cache.scan()

        reported = generate_index_markdown(self.vault, cache=self.cache)
        section = reported.split("## Verification Debt", 1)[1].split("\n## ", 1)[0]
        listed = [ln for ln in section.splitlines() if ln.startswith("| `")]

        self.assertEqual(len(listed), 2, "fixture stopped producing debt; the test below is vacuous without it")
        self.assertEqual(len(listed), self.cache.get_status()["verification_debt"])

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

    def test_small_vault_saturated_term_preserves_field_weight_hierarchy(self):
        """§4.4, §10 C15. When a term hits >=50% of a small vault, FTS5 clamps IDF
        to ~1e-6; the field-presence floor keeps title/description/tags weights
        alive and keeps scores above 0.05 so CLI search never prints 0.000.
        """
        self.cache.scan()
        # 'SQLite' appears in 5 of the 6 demo-vault notes, triggering FTS5 IDF clamping.
        results = self.cache.search("SQLite")
        by_cid = {r.cid: r for r in results}

        self.assertGreaterEqual(by_cid["wiki/SQLite"].score, 0.35)
        self.assertGreaterEqual(min(r.score for r in results), 0.05)

        # 'Flash' hits 3 of 6 notes (>=50%): wiki/Flash-Attention (title + body,
        # unverified 1.0x -> 0.25) must outrank wiki/Epistemic-Trust-Tiers
        # (body-only mention, human-reviewed 2.0x -> 0.10).
        flash_results = self.cache.search("Flash")
        self.assertEqual(flash_results[0].cid, "wiki/Flash-Attention")
        self.assertGreater(flash_results[0].score, flash_results[1].score)

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
        resolver = self.cache.get_link_resolver()

        # Bare stem
        self.assertEqual(resolver.resolve("SQLite"), "wiki/SQLite")
        self.assertEqual(resolver.resolve("Flash-Attention"), "wiki/Flash-Attention")

        # Exact CID
        self.assertEqual(resolver.resolve("wiki/Flash-Attention"), "wiki/Flash-Attention")

        # Nonexistent
        self.assertIsNone(resolver.resolve("NonExistentStem"))


class TestOnlyMarkdownIsANote(unittest.TestCase):
    """§2.1. A cognitive domain holds notes; the other files in it are not notes.

    `cache.py` had its own copy of the domain walk and that copy omitted the
    `.md` filter the other three copies applied, so any file in a domain became
    a row in `notes`. A PNG was read with `errors="replace"`, its bytes were
    fed to FTS5, it was listed in `index.md` as unfiled work, and gate 5 called
    it an orphan -- telling the reader to go file a diagram. The walk is now
    `Vault.iter_domain_notes()` and there is one filter to get wrong.
    """

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name) / "v"
        (self.root / "wiki").mkdir(parents=True)
        (self.root / "raw").mkdir(parents=True)
        (self.root / ".cadabby.json").write_text('{"vault_name": "v"}\n', encoding="utf-8")
        (self.root / "wiki" / "Real-Note.md").write_text(
            "---\ntype: concept\ntitle: Real Note\ndescription: A note.\nstatus: active\n---\n\nBody.\n",
            encoding="utf-8",
        )
        self.vault = Vault(self.root)
        self.cache = VaultCache(self.vault)

    def tearDown(self):
        self.cache.close()
        self.tmp_dir.cleanup()

    def _indexed(self):
        self.cache.scan()
        rows = self.cache.get_connection().execute("SELECT rel_path FROM notes;").fetchall()
        return {r["rel_path"] for r in rows}

    def test_non_markdown_files_in_a_domain_are_not_notes(self):
        (self.root / "wiki" / "diagram.png").write_bytes(bytes(range(256)) * 4)
        (self.root / "wiki" / "scratch.txt").write_text("not a note", encoding="utf-8")
        self.assertEqual(self._indexed(), {"wiki/Real-Note.md"})

    def test_a_binary_in_a_domain_is_not_reported_as_unfiled_work(self):
        """The visible symptom: index.md told the reader to go file a PNG."""
        (self.root / "wiki" / "diagram.png").write_bytes(bytes(range(256)) * 4)
        self.cache.scan()
        self.assertNotIn("diagram", generate_index_markdown(self.vault, self.cache))

    def test_raw_keeps_accepting_non_markdown(self):
        """The filter is scoped to domains. `raw/` is evidence, not notes (§2.4)."""
        (self.root / "raw" / "transcript.txt").write_text("collected evidence", encoding="utf-8")
        self.assertIn("raw/transcript.txt", self._indexed())

    def test_a_domains_own_manifest_is_not_one_of_its_notes(self):
        (self.root / "wiki" / "AGENTS.md").write_text("# Wiki directives\n", encoding="utf-8")
        self.assertEqual(self._indexed(), {"wiki/Real-Note.md"})

    def test_the_walk_is_ordered(self):
        """Insertion order sets rowids, and rowid is the BM25 tiebreak (§4.4)."""
        for stem in ("Zulu", "Alpha", "Mike"):
            (self.root / "wiki" / f"{stem}.md").write_text(
                f"---\ntype: concept\ntitle: {stem}\ndescription: d\nstatus: active\n---\n\nb\n",
                encoding="utf-8",
            )
        walked = [p.name for p, _, _ in self.vault.iter_domain_notes()]
        self.assertEqual(walked, sorted(walked))


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


class TestVaultCacheRanking(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault_dir = Path(self.tmp.name) / "vault"
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        self.vault = Vault(self.vault_dir)

    def tearDown(self):
        self.tmp.cleanup()

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

    def test_scan_clears_links_when_note_becomes_unparseable(self):
        note_a = self.vault.wiki_dir / "Note-A.md"
        note_b = self.vault.wiki_dir / "Note-B.md"
        note_a.parent.mkdir(parents=True, exist_ok=True)
        note_a.write_text(
            "---\ntype: concept\ntitle: Note A\ndescription: A\nstatus: active\n---\n# A\n[[Note-B]]\n",
            "utf-8",
        )
        note_b.write_text(
            "---\ntype: concept\ntitle: Note B\ndescription: B\nstatus: active\n---\n# B\n[[Note-A]]\n",
            "utf-8",
        )
        with VaultCache(self.vault) as cache:
            cache.scan()
            conn = cache.get_connection()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM links WHERE source_cid = 'wiki/Note-B';").fetchone()[0], 1)

            # Corrupt Note-B frontmatter on update
            note_b.write_text("---\ntags: [bad, flow]\n---\n# B\n", "utf-8")
            cache.scan(force=True)

            # Outbound links from Note-B must be cleared, and Note-A's link to Note-B must become unresolved (NULL)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM links WHERE source_cid = 'wiki/Note-B';").fetchone()[0], 0)
            row = conn.execute("SELECT target_cid FROM links WHERE source_cid = 'wiki/Note-A';").fetchone()
            self.assertIsNone(row["target_cid"])

    def test_scan_reresolves_stem_priority_when_wiki_note_repaired(self):
        (self.vault.root / "projects").mkdir(parents=True, exist_ok=True)
        (self.vault.wiki_dir).mkdir(parents=True, exist_ok=True)

        caller = self.vault.wiki_dir / "Caller.md"
        wiki_shared = self.vault.wiki_dir / "Shared.md"
        proj_shared = self.vault.root / "projects" / "Shared.md"

        caller.write_text(
            "---\ntype: concept\ntitle: Caller\ndescription: C\nstatus: active\n---\n# C\n[[Shared]]\n",
            "utf-8",
        )
        # Initially wiki/Shared has broken frontmatter, so [[Shared]] resolves to projects/Shared
        wiki_shared.write_text("---\ntags: [broken]\n---\n# Shared\n", "utf-8")
        proj_shared.write_text(
            "---\ntype: concept\ntitle: Shared\ndescription: P\nstatus: active\n---\n# P\n",
            "utf-8",
        )

        with VaultCache(self.vault) as cache:
            cache.scan()
            conn = cache.get_connection()
            row1 = conn.execute("SELECT target_cid FROM links WHERE source_cid = 'wiki/Caller';").fetchone()
            self.assertEqual(row1["target_cid"], "projects/Shared")

            # Repair wiki/Shared frontmatter (an update to an existing note)
            wiki_shared.write_text(
                "---\ntype: concept\ntitle: Shared\ndescription: W\nstatus: active\n---\n# W\n",
                "utf-8",
            )
            cache.scan(force=True)
            row2 = conn.execute("SELECT target_cid FROM links WHERE source_cid = 'wiki/Caller';").fetchone()
            self.assertEqual(row2["target_cid"], "wiki/Shared")


if __name__ == "__main__":
    unittest.main()


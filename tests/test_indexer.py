"""index.md as a gap report (§2.5) and its regeneration contract (§4.3).

index.md used to be a catalog: one table row per wiki note, grouped by type.
That duplicated what MOCs curate, what a file explorer shows for a flat
folder, and what search ranks better, and it churned the Git diff on every
note added. These pin the replacement -- and pin that the catalog stays gone.
"""

from __future__ import annotations

import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from cadabby.cache import VaultCache
from cadabby.indexer import generate_index_markdown, rotate_vault_log, sync_vault_index
from cadabby.lint import run_vault_lint
from cadabby.vault import Vault


class IndexerFixture(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.vault_root = Path(self.tmp_dir.name) / "test-vault"
        for sub in ("wiki", "raw"):
            (self.vault_root / sub).mkdir(parents=True, exist_ok=True)
        (self.vault_root / ".cadabby.json").write_text('{"vault_name": "Test Vault"}\n', "utf-8")
        self.vault = Vault(self.vault_root)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _note(self, stem, type_="concept", body="", status="active", domain="wiki", **extra):
        path = self.vault_root / domain / f"{stem}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = [
            "---",
            f"type: {type_}",
            f'title: "{stem}"',
            f'description: "about {stem}"',
            f"status: {status}",
        ]
        for key, value in extra.items():
            fm.append(f"{key}: {value}")
        fm.append("---")
        path.write_text("\n".join(fm) + f"\n# {stem}\n\n{body}\n", "utf-8")
        return path

    def _index(self):
        return generate_index_markdown(self.vault)


class TestIndexIsAGapReport(IndexerFixture):
    """§10 C17. Four sections, each omitted when empty, and no catalog."""

    def test_entry_points_lists_every_moc(self):
        self._note("Alpha-MOC", type_="moc")
        self._note("Beta-MOC", type_="moc")
        md = self._index()
        self.assertIn("## Entry Points", md)
        self.assertIn("[[Alpha-MOC]]", md)
        self.assertIn("[[Beta-MOC]]", md)

    def test_filed_notes_are_not_enumerated(self):
        """The whole point: a note reachable from a MOC earns no row."""
        self._note("Hub-MOC", type_="moc", body="See [[Filed-Note]].")
        self._note("Filed-Note")
        md = self._index()
        self.assertNotIn("[[Filed-Note]]", md)
        self.assertNotIn("## Concepts", md, "the per-type catalog must stay gone")

    def test_unfiled_note_is_reported(self):
        self._note("Hub-MOC", type_="moc")
        self._note("Lonely-Note")
        md = self._index()
        self.assertIn("## Unfiled Notes", md)
        self.assertIn("[[Lonely-Note]]", md)

    def test_reachability_is_transitive(self):
        """'at any depth' -- a note filed two hops from a MOC is filed."""
        self._note("Hub-MOC", type_="moc", body="See [[Middle]].")
        self._note("Middle", body="See [[Deep]].")
        self._note("Deep")
        md = self._index()
        self.assertNotIn("[[Deep]]", md)

    def test_a_link_cycle_terminates(self):
        self._note("Hub-MOC", type_="moc", body="See [[A]].")
        self._note("A", body="See [[B]].")
        self._note("B", body="Back to [[A]].")
        self.assertNotIn("## Unfiled Notes", self._index())

    def test_deprecated_notes_are_not_unfiled_work(self):
        """A retired note is not work someone still has to do (§2.5)."""
        self._note("Hub-MOC", type_="moc")
        self._note("Retired", status="deprecated")
        self.assertNotIn("[[Retired]]", self._index())

    def test_raw_queue_lists_only_unprocessed_sources(self):
        (self.vault_root / "raw" / "pending.md").write_text("unread\n", "utf-8")
        (self.vault_root / "raw" / "used.md").write_text("cited\n", "utf-8")
        self._note("Citing-MOC", type_="moc", **{"sources": "\n  - raw/used.md"})
        md = self._index()
        self.assertIn("`raw/pending.md`", md)
        self.assertNotIn("`raw/used.md`", md)

    def test_verification_debt_section_lists_stale_notes(self):
        self._note("Hub-MOC", type_="moc", body="See [[Drifted]].")
        note = self._note(
            "Drifted",
            **{"verified": "\n  - by: agent:test\n    at: '2026-01-01T00:00:00Z'\n    of: 'sha256:0000'"},
        )
        note.write_text(note.read_text("utf-8") + "\nbody has moved on since that attestation\n", "utf-8")
        md = self._index()
        self.assertIn("## Verification Debt", md)
        self.assertIn("wiki/Drifted", md)

    def test_a_healthy_vault_has_entry_points_and_no_gap_sections(self):
        """§2.5: zero gaps is reachable, but the starting page survives."""
        self._note("Hub-MOC", type_="moc", body="See [[Filed-Note]].")
        self._note("Filed-Note")
        md = self._index()
        self.assertIn("## Entry Points", md)
        for gap in ("## Unfiled Notes", "## Raw Sources", "## Verification Debt"):
            self.assertNotIn(gap, md, f"{gap} rendered with nothing to report")

    def test_an_empty_vault_says_so_rather_than_rendering_nothing(self):
        self.assertIn("Nothing outstanding", self._index())


class TestUnfiledTracksTheGraph(IndexerFixture):
    """§10 C18. The Unfiled section is the one gap lint cannot catch."""

    def test_linking_from_a_moc_clears_the_note(self):
        self._note("Hub-MOC", type_="moc")
        self._note("Stray")
        self.assertIn("[[Stray]]", self._index())

        self._note("Hub-MOC", type_="moc", body="Now filed: [[Stray]].")
        self.assertNotIn("[[Stray]]", self._index())

    def test_a_domain_note_is_never_unfiled(self):
        """Only wiki notes are reported: MOCs are a `wiki/` construct (§2.5).

        Domains carry their own topology in folders and are not required to
        have MOCs at all, so reporting them would put a floor under the
        section that no amount of filing can clear -- the same failure the
        `deprecated` exclusion exists to prevent.
        """
        self._note("Hub-MOC", type_="moc")
        self._note("Acme", domain="customers")
        self.assertNotIn("[[Acme]]", self._index())

    def test_outward_linking_note_is_unfiled_but_not_an_orphan(self):
        """Gate 5 sees edges and stays quiet; index.md is what surfaces it."""
        self._note("Hub-MOC", type_="moc")
        self._note("Talker", body="I link to [[Hub-MOC]] but nothing links to me.")

        self.assertIn("[[Talker]]", self._index())
        codes = {f.code for f in run_vault_lint(self.vault) if "Talker" in f.cid}
        self.assertNotIn("NOTE_ORPHAN", codes)


class TestDomainsGenerateNothing(IndexerFixture):
    """§10 C19. Depositing an engine-owned file in a domain breaks §2.2."""

    def test_no_index_is_written_into_a_cognitive_domain(self):
        self._note("Acme", domain="customers")
        self._note("Hub-MOC", type_="moc")
        with VaultCache(self.vault) as cache:
            cache.scan()
            sync_vault_index(self.vault, cache=cache)

        authored = {p.name for p in (self.vault_root / "customers").iterdir()}
        self.assertEqual(authored, {"Acme.md"})
        self.assertFalse((self.vault_root / "customers" / "index.md").exists())


class TestRegenerationRidesTheScan(IndexerFixture):
    """§10 C20. §4.3 binds regeneration to the scan, not to the write path."""

    def test_a_changed_scan_regenerates_without_an_explicit_sync(self):
        self._note("Hub-MOC", type_="moc")
        with VaultCache(self.vault) as cache:
            cache.scan()
        self.assertTrue(self.vault.index_path.exists())
        self.assertIn("[[Hub-MOC]]", self.vault.index_path.read_text("utf-8"))

    def test_an_unchanged_scan_leaves_bytes_and_mtime_untouched(self):
        self._note("Hub-MOC", type_="moc")
        with VaultCache(self.vault) as cache:
            cache.scan()
            before = self.vault.index_path.read_bytes()
            before_mtime = self.vault.index_path.stat().st_mtime_ns
            time.sleep(0.01)
            inserted, updated, deleted, _ = cache.scan()

        self.assertEqual((inserted, updated, deleted), (0, 0, 0))
        self.assertEqual(self.vault.index_path.read_bytes(), before)
        self.assertEqual(self.vault.index_path.stat().st_mtime_ns, before_mtime)

    def test_a_raw_file_a_human_dropped_in_reaches_the_queue(self):
        """The case no write path sees, and the reason §4.3 binds to the scan."""
        self._note("Hub-MOC", type_="moc")
        with VaultCache(self.vault) as cache:
            cache.scan()
            self.assertNotIn("## Raw Sources", self.vault.index_path.read_text("utf-8"))
            (self.vault_root / "raw" / "dropped.pdf").write_bytes(b"%PDF-1.4\n")
            cache.scan()

        self.assertIn("`raw/dropped.pdf`", self.vault.index_path.read_text("utf-8"))

    def test_an_unchanged_scan_does_not_even_build_the_report(self):
        """§4.3: a scan finding no changes skips regeneration *entirely*.

        The byte guard in sync_vault_index already makes a no-op scan leave
        the file alone, so this is invisible from the filesystem and has to
        be asserted on the call. It is worth asserting because every MCP read
        tool runs a scan: without the change-count check, each read would run
        the report's four queries to discover it had nothing to write.
        """
        self._note("Hub-MOC", type_="moc")
        with VaultCache(self.vault) as cache:
            cache.scan()
            with unittest.mock.patch("cadabby.indexer.sync_vault_index") as regen:
                cache.scan()
            regen.assert_not_called()

    def test_sync_vault_index_stays_byte_idempotent(self):
        self._note("Hub-MOC", type_="moc")
        with VaultCache(self.vault) as cache:
            cache.scan(regenerate_index=False)
            self.assertTrue(sync_vault_index(self.vault, cache=cache))
            self.assertFalse(sync_vault_index(self.vault, cache=cache))


class TestLogRotation(IndexerFixture):
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


if __name__ == "__main__":
    unittest.main()

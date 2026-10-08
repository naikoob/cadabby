"""Unit tests for ops.py: scaffolding, update, verify, and ground."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cadabby.adapters.disk_storage import DiskNoteStorage
from cadabby.errors import HumanAttestationRefusedError
from cadabby.frontmatter import FrontmatterSerializeError, parse_frontmatter
from cadabby.fsutil import compute_file_sha256
from cadabby.ops import ground_notes, scaffold_note, update_note, verify_note
from cadabby.vault import Vault
from tests.helpers import copy_demo_vault


class TestOps(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.vault_root = copy_demo_vault(Path(self.tmp_dir.name) / "demo-vault")
        self.vault = Vault(self.vault_root)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_scaffold_path_outside_note_locations_is_refused(self):
        """§2.1, §2.2, C6: scaffold refuses any path the scan would never index."""
        refused = (
            "raw/Sneaky",
            "log/Sneaky",
            ".hidden/Note",
            "customers/node_modules/Note",
            "wiki/sub/Nested",
            "customers/AGENTS",
        )
        for path in refused:
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "Cannot scaffold"):
                    scaffold_note(self.vault, title="T", type_="concept", description="d", path=path)
                self.assertFalse((self.vault_root / f"{path}.md").exists())
        # A new domain folder needs no registration step (§2.2).
        created = scaffold_note(self.vault, title="T", type_="concept", description="d", path="fresh/Note")
        self.assertTrue(created.is_file())

    def test_update_note_rejects_out_of_subset_patch_without_touching_the_file(self):
        """An over-nested patch must fail the write, not corrupt the note (§3.2).

        Coercing the value would leave frontmatter this engine cannot re-read,
        dropping the note out of search while the call reported success.
        """
        target = self.vault.wiki_dir / "SQLite.md"
        before = target.read_text("utf-8")

        with self.assertRaises(FrontmatterSerializeError):
            update_note(
                vault=self.vault,
                cid_or_path="wiki/SQLite",
                frontmatter_patch={"provenance": {"import": {"tool": "zotero"}}},
                actor="agent:claude-test",
            )

        self.assertEqual(target.read_text("utf-8"), before)
        # Still parseable, so still indexable.
        self.assertTrue(parse_frontmatter(target.read_text("utf-8"))[0])

        # A legal one-level mapping is unaffected by the guard.
        update_note(
            vault=self.vault,
            cid_or_path="wiki/SQLite",
            frontmatter_patch={"provenance": {"tool": "zotero"}},
            actor="agent:claude-test",
        )
        fm, _ = parse_frontmatter(target.read_text("utf-8"))
        self.assertEqual(fm["provenance"], {"tool": "zotero"})

    def test_scaffold_note_success_and_conflict(self):
        note_path = scaffold_note(
            vault=self.vault,
            title="Vector Databases",
            type_="concept",
            description="Overview of dense vector indexing and ANN algorithms",
            tags=["vector", "ann"],
            sources=["raw/karpathy-llm-wiki-gist.md"],
            body="# Vector Databases\n\nContent here.\n",
            actor="agent:claude-test",
        )

        self.assertTrue(note_path.exists())
        self.assertEqual(note_path.parent.name, "wiki")

        fm, _ = parse_frontmatter(note_path.read_text("utf-8"))
        self.assertEqual(fm["title"], "Vector Databases")
        self.assertEqual(fm["type"], "concept")
        self.assertEqual(fm["status"], "draft")
        self.assertEqual(fm["generated"]["by"], "agent:claude-test")

        # Refuses to overwrite
        with self.assertRaises(FileExistsError):
            scaffold_note(
                vault=self.vault,
                title="Vector Databases",
                type_="concept",
                description="Duplicate",
            )

    def test_scaffold_note_path_traversal_rejection(self):
        # Path with .. traversal must raise ValueError
        with self.assertRaises(ValueError) as ctx:
            scaffold_note(
                vault=self.vault,
                title="Escape Note",
                type_="concept",
                description="Attempt to escape vault",
                path="../../outside_vault.md",
            )
        self.assertIn("Path traversal detected", str(ctx.exception))

        # Absolute path must raise ValueError
        with self.assertRaises(ValueError) as ctx:
            scaffold_note(
                vault=self.vault,
                title="Root Note",
                type_="concept",
                description="Attempt absolute path",
                path="/tmp/outside.md",
            )
        self.assertIn("Path traversal detected", str(ctx.exception))

    def test_update_note_sections_and_patch(self):
        cid = "wiki/Flash-Attention"
        note_file = self.vault.wiki_dir / "Flash-Attention.md"
        h_before = compute_file_sha256(note_file)

        # Update note: append a section and patch status
        update_note(
            vault=self.vault,
            cid_or_path=cid,
            frontmatter_patch={"status": "evergreen"},
            append_section=("Benchmarking", "Benchmarking shows 2-4x speedup."),
            expected_hash=h_before,
            actor="agent:benchmarker",
        )

        fm, body = parse_frontmatter(note_file.read_text("utf-8"))
        self.assertEqual(fm["status"], "evergreen")
        self.assertIn("## Benchmarking", body)
        self.assertIn("Benchmarking shows 2-4x speedup.", body)

    def test_verify_note_human_refusal_and_machine_attestation(self):
        cid = "wiki/Flash-Attention"

        # A human:* attestation without interactive authorization is refused with
        # the categorical code, never PERMISSION_DENIED (C12).
        with self.assertRaises(HumanAttestationRefusedError):
            verify_note(
                vault=self.vault,
                cid_or_path=cid,
                actor="human:operator",
                is_human_authorized=False,
            )

        # Machine verification succeeds
        result = verify_note(
            vault=self.vault,
            cid_or_path=cid,
            actor="agent:validator",
            method="automated-check",
        )
        self.assertEqual(result["actor"], "agent:validator")
        self.assertEqual(result["trust_tier"], "machine-confirmed")
        self.assertTrue(result["of"].startswith("sha256:"))

    def test_ground_notes_and_budget_truncation(self):
        cids = ["wiki/Epistemic-Trust-Tiers", "wiki/SQLite"]
        grounded = ground_notes(self.vault, cids, budget_tokens=5000)

        self.assertEqual(len(grounded), 2)
        epistemic = next(g for g in grounded if "Epistemic-Trust-Tiers" in g["cid"])
        self.assertEqual(epistemic["trust_tier"], "human-reviewed")
        self.assertGreater(len(epistemic["links"]), 0)
        self.assertFalse(epistemic["truncated"])

    def test_ground_notes_stem_and_wikilink_resolution(self):
        # 1. Bare note stems
        grounded_stems = ground_notes(self.vault, ["Epistemic-Trust-Tiers", "SQLite"])
        self.assertEqual(len(grounded_stems), 2)
        cids = [g["cid"] for g in grounded_stems]
        self.assertIn("wiki/Epistemic-Trust-Tiers", cids)
        self.assertIn("wiki/SQLite", cids)

        # 2. Wikilink syntax with anchor and alias
        grounded_wiki = ground_notes(
            self.vault,
            ["[[Epistemic-Trust-Tiers#Tiers]]", "[[SQLite|SQLite Database]]"],
        )
        self.assertEqual(len(grounded_wiki), 2)
        cids_wiki = [g["cid"] for g in grounded_wiki]
        self.assertIn("wiki/Epistemic-Trust-Tiers", cids_wiki)
        self.assertIn("wiki/SQLite", cids_wiki)

        # 3. Relative path suffixes and .md extension
        grounded_paths = ground_notes(
            self.vault,
            ["Epistemic-Trust-Tiers.md", "wiki/SQLite.md"],
        )
        self.assertEqual(len(grounded_paths), 2)
        cids_paths = [g["cid"] for g in grounded_paths]
        self.assertIn("wiki/Epistemic-Trust-Tiers", cids_paths)
        self.assertIn("wiki/SQLite", cids_paths)

        # 4. Custom domain notes via bare stem
        cust_dir = self.vault_root / "customers"
        cust_dir.mkdir(parents=True, exist_ok=True)
        (cust_dir / "Acme-Corp.md").write_text(
            "---\ntitle: Acme Corp\ntype: customer\nstatus: active\n---\n# Acme Corp\n",
            "utf-8",
        )
        grounded_cust = ground_notes(self.vault, ["Acme-Corp"])
        self.assertEqual(len(grounded_cust), 1)
        self.assertEqual(grounded_cust[0]["cid"], "customers/Acme-Corp")
        self.assertEqual(grounded_cust[0]["domain"], "customers")

    def test_disk_storage_multi_domain_and_pruning(self):
        # Add a custom domain 'customers'
        cust_dir = self.vault_root / "customers"
        cust_dir.mkdir(parents=True, exist_ok=True)
        (cust_dir / "Acme.md").write_text(
            "---\ntitle: Acme Corp\ntype: customer\nstatus: active\n---\n# Acme\n", "utf-8"
        )
        # Add ignored folder inside domain
        ignored_dir = cust_dir / "node_modules"
        ignored_dir.mkdir(parents=True, exist_ok=True)
        (ignored_dir / "bad.md").write_text("should be ignored", "utf-8")

        storage = DiskNoteStorage(self.vault)
        cids = storage.list_note_cids()

        self.assertIn("customers/Acme", cids)
        self.assertNotIn("customers/node_modules/bad", cids)
        # Should also contain wiki notes
        self.assertTrue(any(c.startswith("wiki/") for c in cids))

    def test_scaffold_moc_note(self):
        moc_path = scaffold_note(
            vault=self.vault,
            title="Distributed Systems MOC",
            type_="moc",
            description="Map of Content covering consensus, replication, and partitioning",
            tags=["distributed-systems", "moc"],
            body="# Distributed Systems MOC\n\nHub for distributed systems.\n",
        )
        self.assertTrue(moc_path.exists())
        self.assertEqual(moc_path.parent.name, "wiki")
        self.assertEqual(moc_path.name, "Distributed-Systems-MOC.md")
        fm, _ = parse_frontmatter(moc_path.read_text("utf-8"))
        self.assertEqual(fm["type"], "moc")
        self.assertEqual(fm["title"], "Distributed Systems MOC")

    def test_scaffold_open_typing_wiki(self):
        adr_path = scaffold_note(
            vault=self.vault,
            title="ADR 001 Event Sourcing",
            type_="adr",
            description="Architectural decision record for event sourcing",
            body="# ADR 001\n\nDecision rationale.\n",
        )
        self.assertTrue(adr_path.exists())
        self.assertEqual(adr_path.parent.name, "wiki")
        self.assertEqual(adr_path.name, "ADR-001-Event-Sourcing.md")
        fm, _ = parse_frontmatter(adr_path.read_text("utf-8"))
        self.assertEqual(fm["type"], "adr")

        grounded = ground_notes(self.vault, ["ADR-001-Event-Sourcing"])
        self.assertEqual(len(grounded), 1)
        self.assertEqual(grounded[0]["type"], "adr")
        self.assertEqual(grounded[0]["cid"], "wiki/ADR-001-Event-Sourcing")

    def test_implicit_occ_detects_concurrent_modification_on_update_and_verify(self):
        from cadabby.adapters.disk_storage import DiskNoteStorage, FileLedger
        from cadabby.fsutil import VaultConflictError
        from cadabby.ops import UpdateNoteUseCase, VerifyNoteUseCase

        base_storage = DiskNoteStorage(self.vault)
        ledger = FileLedger(self.vault)
        note_path = self.vault.wiki_dir / "Flash-Attention.md"

        class RacingStorage(DiskNoteStorage):
            def get_note(self_inner, cid_or_path: str):
                n = super().get_note(cid_or_path)
                # Simulate another writer modifying the file after get_note() but before save_note()
                note_path.write_text(note_path.read_text("utf-8") + "\nConcurrent edit.\n", "utf-8")
                return n

        racing = RacingStorage(self.vault)
        with self.assertRaises(VaultConflictError):
            UpdateNoteUseCase(racing, ledger).execute(
                cid_or_path="wiki/Flash-Attention",
                frontmatter_patch={"description": "Should fail due to concurrent edit"},
            )

        with self.assertRaises(VaultConflictError):
            VerifyNoteUseCase(racing, ledger).execute(
                cid_or_path="wiki/Flash-Attention",
                actor="agent:verifier",
            )
        self.assertIsNotNone(base_storage.get_note("wiki/Flash-Attention"))

    def test_ground_notes_closes_temporary_cache(self):
        from unittest.mock import patch
        from cadabby.cache import VaultCache

        closed_instances: list[VaultCache] = []
        orig_close = VaultCache.close

        def tracking_close(cache_self: VaultCache) -> None:
            closed_instances.append(cache_self)
            orig_close(cache_self)

        with patch.object(VaultCache, "close", autospec=True, side_effect=tracking_close):
            grounded = ground_notes(self.vault, ["wiki/SQLite"], cache=None)
            self.assertEqual(len(grounded), 1)
            self.assertEqual(len(closed_instances), 1)


class TestUpdateNoteEdits(unittest.TestCase):
    """§5.1 `edits`: exact-text replacement and one atomic write per call (C28)."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.vault_root = copy_demo_vault(Path(self.tmp_dir.name) / "demo-vault")
        self.vault = Vault(self.vault_root)
        self.note = self.vault.wiki_dir / "Edits-Fixture.md"
        self.note.write_text(
            "---\ntype: concept\ntitle: Edits Fixture\ndescription: Preamble marker lives here too\n"
            "status: active\n---\n# Edits Fixture\n\nPreamble sentence with a typo: teh vault.\n\n"
            "## Alpha\n\nSame line.\n\n## Beta\n\nSame line.\n",
            "utf-8",
        )
        self.log = self.vault_root / "log.md"

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _update(self, edits):
        return update_note(vault=self.vault, cid_or_path="wiki/Edits-Fixture", edits=edits, actor="agent:t")

    def _log_lines(self) -> int:
        return self.log.read_text("utf-8").count("\n") if self.log.exists() else 0

    def test_replace_text_edits_the_preamble_above_first_section(self):
        self._update([{"op": "replace_text", "old": "teh vault", "new": "the vault"}])
        _, body = parse_frontmatter(self.note.read_text("utf-8"))
        self.assertIn("Preamble sentence with a typo: the vault.", body)
        self.assertIn("## Alpha", body)

    def test_replace_text_zero_or_multiple_matches_writes_nothing(self):
        before = self.note.read_bytes()
        for old in ("absent text", "Same line."):
            with self.subTest(old=old), self.assertRaises(ValueError):
                self._update([{"op": "replace_text", "old": old, "new": "X"}])
            self.assertEqual(self.note.read_bytes(), before)

    def test_replace_text_cannot_reach_frontmatter(self):
        before = self.note.read_bytes()
        with self.assertRaises(ValueError):
            self._update([{"op": "replace_text", "old": "Preamble marker lives here too", "new": "forged"}])
        self.assertEqual(self.note.read_bytes(), before)

    def test_batched_edits_are_atomic_and_logged_once(self):
        before, log_before = self.note.read_bytes(), self._log_lines()
        good = [
            {"op": "replace_text", "old": "teh vault", "new": "the vault"},
            {"op": "replace_section", "heading": "Alpha", "body": "Alpha rewritten."},
        ]
        with self.assertRaises(ValueError):
            self._update([*good, {"op": "replace_text", "old": "nowhere in body", "new": "X"}])
        self.assertEqual(self.note.read_bytes(), before)
        self.assertEqual(self._log_lines(), log_before)

        self._update([*good, {"op": "append_section", "heading": "Gamma", "body": "New."}])
        _, body = parse_frontmatter(self.note.read_text("utf-8"))
        self.assertIn("the vault", body)
        self.assertIn("Alpha rewritten.", body)
        self.assertIn("## Gamma", body)
        self.assertEqual(self._log_lines(), log_before + 1)
        self.assertIn("(3 edits)", self.log.read_text("utf-8"))

    def test_truncate_to_budget_respects_budget_on_single_section_note(self):
        from cadabby.domain import Note
        from cadabby.ops import _truncate_to_budget

        long_note = Note(
            cid="wiki/Long",
            rel_path="wiki/Long.md",
            frontmatter={"type": "concept", "title": "Long", "description": "Desc", "status": "active"},
            body="A" * 600 + "\n",
        )
        out, truncated = _truncate_to_budget(long_note, budget_tokens=100)
        self.assertTrue(truncated)
        self.assertLessEqual(len(out), 400)
        self.assertIn("[truncated: 0 of 1 sections included]", out)

    def test_scaffold_rejects_dot_segment_and_collapses_double_slashes(self):
        from cadabby.adapters.memory_storage import InMemoryLedger, InMemoryNoteStorage
        from cadabby.ops import ScaffoldNoteUseCase

        uc = ScaffoldNoteUseCase(InMemoryNoteStorage(), InMemoryLedger())
        with self.assertRaises(ValueError):
            uc.execute(title="Dot", type_="concept", description="d", path="./Note")
        with self.assertRaises(ValueError):
            uc.execute(title="Dot", type_="concept", description="d", path="customers/./Note")

        clean = uc.execute(title="Clean", type_="concept", description="d", domain="customers", path="customers//acme//Note")
        self.assertEqual(clean.rel_path, "customers/acme/Note.md")

    def test_verify_note_idempotency_sets_already_verified_and_skips_second_write(self):
        from cadabby.ops import verify_note

        first = verify_note(self.vault, "wiki/Edits-Fixture", actor="agent:test")
        self.assertFalse(first["already_verified"])
        log_after_first = self._log_lines()

        second = verify_note(self.vault, "wiki/Edits-Fixture", actor="agent:test")
        self.assertTrue(second["already_verified"])
        self.assertEqual(self._log_lines(), log_after_first)


if __name__ == "__main__":
    unittest.main()



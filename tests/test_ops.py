"""Unit tests for ops.py: scaffolding, update, verify, and ground."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from cadabby.adapters.disk_storage import DiskNoteStorage
from cadabby.frontmatter import FrontmatterSerializeError, parse_frontmatter
from cadabby.fsutil import compute_file_sha256
from cadabby.ops import ground_notes, scaffold_note, update_note, verify_note
from cadabby.vault import Vault


class TestOps(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.vault_root = Path(self.tmp_dir.name) / "demo-vault"
        demo_src = Path(__file__).resolve().parent.parent / "examples" / "demo-vault"
        shutil.copytree(demo_src, self.vault_root, ignore=shutil.ignore_patterns(".cadabby", "*.pyc"))
        self.vault = Vault(self.vault_root)

    def tearDown(self):
        self.tmp_dir.cleanup()

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

        # Attempting human verification over MCP (without is_human_authorized) must raise PermissionError
        with self.assertRaises(PermissionError):
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


if __name__ == "__main__":
    unittest.main()


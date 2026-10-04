"""Unit tests for ops.py: scaffolding, update, verify, and ground."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from cadabby.adapters.disk_storage import DiskNoteStorage
from cadabby.frontmatter import parse_frontmatter
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
        self.assertEqual(note_path.parent.name, "concepts")

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
        cid = "wiki/concepts/Flash-Attention"
        note_file = self.vault.wiki_dir / "concepts" / "Flash-Attention.md"
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
        cid = "wiki/concepts/Flash-Attention"

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
        cids = ["wiki/concepts/Epistemic-Trust-Tiers", "wiki/entities/SQLite"]
        grounded = ground_notes(self.vault, cids, budget_tokens=5000)

        self.assertEqual(len(grounded), 2)
        epistemic = next(g for g in grounded if "Epistemic-Trust-Tiers" in g["cid"])
        self.assertEqual(epistemic["trust_tier"], "human-reviewed")
        self.assertGreater(len(epistemic["links"]), 0)
        self.assertFalse(epistemic["truncated"])

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


if __name__ == "__main__":
    unittest.main()

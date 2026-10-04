"""Unit tests for pure domain aggregates and in-memory hexagonal use cases."""

from __future__ import annotations

import unittest

from cadabby.adapters.memory_storage import InMemoryLedger, InMemoryNoteStorage
from cadabby.domain import Note
from cadabby.fsutil import VaultConflictError
from cadabby.ops import ScaffoldNoteUseCase, UpdateNoteUseCase, VerifyNoteUseCase


class TestDomainModel(unittest.TestCase):
    def test_note_properties_and_trust_tier(self):
        fm = {
            "type": "concept",
            "title": "Quantum Entanglement",
            "description": "Non-local correlation phenomenon",
            "status": "active",
            "tags": ["physics", "quantum"],
            "sources": ["raw/bell-1964.pdf"],
            "verified": [],
        }
        body = "# Quantum Entanglement\n\nSpooky action at a distance.\n"
        note = Note(cid="wiki/concepts/Quantum-Entanglement", rel_path="wiki/concepts/Quantum-Entanglement.md", frontmatter=fm, body=body)

        self.assertEqual(note.title, "Quantum Entanglement")
        self.assertEqual(note.type, "concept")
        self.assertEqual(note.description, "Non-local correlation phenomenon")
        self.assertEqual(note.status, "active")
        self.assertEqual(note.tags, ["physics", "quantum"])
        self.assertEqual(note.sources, ["raw/bell-1964.pdf"])
        self.assertEqual(note.trust_tier, "unverified")

        # Add attestation
        attestation, tier, already = note.add_attestation(actor="agent:claude-3.5", method="automated-check")
        self.assertFalse(already)
        self.assertEqual(tier, "machine-confirmed")
        self.assertEqual(note.trust_tier, "machine-confirmed")
        self.assertEqual(attestation["of"], note.body_hash)

        # Duplicate attestation is a no-op
        _, tier2, already2 = note.add_attestation(actor="agent:claude-3.5", method="automated-check")
        self.assertTrue(already2)
        self.assertEqual(tier2, "machine-confirmed")

        # Invalid actor format raises ValueError
        with self.assertRaises(ValueError):
            note.add_attestation(actor="invalid-actor-without-prefix")

    def test_section_replace_and_append(self):
        body = "# Title\n\nIntro\n\n## Section A\n\nOld A content\n\n## Section B\n\nB content\n"
        note = Note(cid="wiki/concepts/Demo", rel_path="wiki/concepts/Demo.md", frontmatter={"type": "concept"}, body=body)

        note.replace_section("Section A", "New A content")
        self.assertIn("## Section A\n\nNew A content", note.body)
        self.assertNotIn("Old A content", note.body)

        # Append new section
        note.append_section("Section C", "C content")
        self.assertTrue(note.body.endswith("## Section C\n\nC content\n"))

    def test_frontmatter_patching(self):
        note = Note(cid="wiki/concepts/Demo", rel_path="wiki/concepts/Demo.md", frontmatter={"status": "draft", "remove_me": 123}, body="body")
        note.patch_frontmatter({"status": "active", "remove_me": None, "new_field": "hello"})
        self.assertEqual(note.status, "active")
        self.assertNotIn("remove_me", note.frontmatter)
        self.assertEqual(note.frontmatter["new_field"], "hello")


class TestInMemoryHexagonalUseCases(unittest.TestCase):
    """Test use cases running purely in memory with zero disk I/O."""

    def setUp(self):
        self.storage = InMemoryNoteStorage()
        self.ledger = InMemoryLedger()

    def test_scaffold_update_verify_pure_in_memory(self):
        scaffold_uc = ScaffoldNoteUseCase(self.storage, self.ledger)
        update_uc = UpdateNoteUseCase(self.storage, self.ledger)
        verify_uc = VerifyNoteUseCase(self.storage, self.ledger)

        # 1. Scaffold
        note = scaffold_uc.execute(
            title="Fast Attention",
            type_="concept",
            description="IO-aware exact attention algorithm",
            tags=["attention", "gpu"],
            sources=["raw/paper.pdf"],
            body="# Fast Attention\n\nTiling and SRAM optimization.\n",
            actor="agent:scaffold-bot",
        )
        self.assertEqual(note.cid, "wiki/concepts/Fast-Attention")
        self.assertTrue(self.storage.note_exists("wiki/concepts/Fast-Attention"))
        self.assertEqual(len(self.ledger.entries), 1)

        # Scaffold duplicate raises FileExistsError
        with self.assertRaises(FileExistsError):
            scaffold_uc.execute(
                title="Fast Attention",
                type_="concept",
                description="Duplicate",
            )

        # 2. Update with OCC hash check
        initial_serialized = note.serialize()
        import hashlib
        initial_hash = hashlib.sha256(initial_serialized.encode("utf-8")).hexdigest()

        # Wrong hash raises VaultConflictError
        with self.assertRaises(VaultConflictError):
            update_uc.execute(
                cid_or_path="wiki/concepts/Fast-Attention",
                frontmatter_patch={"status": "evergreen"},
                expected_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
            )

        # Correct hash succeeds
        updated_note = update_uc.execute(
            cid_or_path="wiki/concepts/Fast-Attention",
            frontmatter_patch={"status": "evergreen"},
            expected_hash=f"sha256:{initial_hash}",
            actor="agent:update-bot",
        )
        self.assertEqual(updated_note.status, "evergreen")
        self.assertEqual(len(self.ledger.entries), 2)

        # 3. Verify
        # Human over non-human authorized raises PermissionError
        with self.assertRaises(PermissionError):
            verify_uc.execute(
                cid_or_path="wiki/concepts/Fast-Attention",
                actor="human:alice",
                is_human_authorized=False,
            )

        # Agent attestation succeeds
        v_res = verify_uc.execute(
            cid_or_path="wiki/concepts/Fast-Attention",
            actor="agent:audit-bot",
            method="formal-proof",
        )
        self.assertEqual(v_res.trust_tier, "machine-confirmed")
        self.assertFalse(v_res.already_verified)
        self.assertEqual(len(self.ledger.entries), 3)

        # Check persisted note in memory has updated verified frontmatter
        persisted = self.storage.get_note("wiki/concepts/Fast-Attention")
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.trust_tier, "machine-confirmed")


if __name__ == "__main__":
    unittest.main()

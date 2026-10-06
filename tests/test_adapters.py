"""Unit tests for secondary driven storage adapters conforming to ports (§9.1)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cadabby.adapters.disk_storage import DiskNoteStorage, FileLedger
from cadabby.adapters.memory_storage import InMemoryLedger, InMemoryNoteStorage
from cadabby.domain import Note
from cadabby.fsutil import VaultConflictError
from cadabby.ports import LedgerPort, NoteStoragePort
from tests.helpers import create_test_vault


class TestAdaptersProtocolConformance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = create_test_vault(Path(self.tmp.name) / "vault")

    def tearDown(self):
        self.tmp.cleanup()

    def test_adapters_conform_to_runtime_protocols(self):
        disk_storage = DiskNoteStorage(self.vault)
        mem_storage = InMemoryNoteStorage()
        file_ledger = FileLedger(self.vault)
        mem_ledger = InMemoryLedger()

        self.assertIsInstance(disk_storage, NoteStoragePort)
        self.assertIsInstance(mem_storage, NoteStoragePort)
        self.assertIsInstance(file_ledger, LedgerPort)
        self.assertIsInstance(mem_ledger, LedgerPort)


class TestDiskNoteStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault_dir = Path(self.tmp.name) / "vault"
        self.vault = create_test_vault(
            self.vault_dir,
            subdirs=("wiki/concepts", "customers/acme", "projects/apollo", "raw", "log"),
        )
        self.storage = DiskNoteStorage(self.vault)

    def tearDown(self):
        self.tmp.cleanup()

    def test_get_note_success(self):
        note_path = self.vault_dir / "wiki" / "concepts" / "Entropy.md"
        content = (
            "---\n"
            "type: concept\n"
            "title: Information Entropy\n"
            "description: Measure of uncertainty\n"
            "status: active\n"
            "---\n"
            "# Information Entropy\n\n"
            "Shannon entropy formulation.\n"
        )
        note_path.write_text(content, "utf-8")

        # By CID
        note_by_cid = self.storage.get_note("wiki/concepts/Entropy")
        self.assertIsNotNone(note_by_cid)
        assert note_by_cid is not None
        self.assertEqual(note_by_cid.title, "Information Entropy")
        self.assertEqual(note_by_cid.type, "concept")
        self.assertIn("Shannon entropy", note_by_cid.body)

        # By relative path
        note_by_path = self.storage.get_note("wiki/concepts/Entropy.md")
        self.assertIsNotNone(note_by_path)
        assert note_by_path is not None
        self.assertEqual(note_by_path.cid, "wiki/concepts/Entropy")

    def test_get_note_nonexistent_returns_none(self):
        self.assertIsNone(self.storage.get_note("wiki/concepts/NonExistent"))
        self.assertIsNone(self.storage.get_note("wiki/concepts/NonExistent.md"))

    def test_get_note_directory_returns_none(self):
        # A directory path is not a note file
        self.assertIsNone(self.storage.get_note("wiki/concepts"))

    def test_get_note_path_traversal_refused(self):
        # Resolving outside vault root must return None
        self.assertIsNone(self.storage.get_note("../../etc/passwd"))
        self.assertIsNone(self.storage.get_note("/etc/passwd"))

    def test_save_note_success(self):
        note = Note(
            cid="wiki/concepts/Relativity",
            rel_path="wiki/concepts/Relativity.md",
            frontmatter={
                "type": "concept",
                "title": "General Relativity",
                "description": "Geometric gravitation",
                "status": "active",
            },
            body="# General Relativity\n\nCurvature of spacetime.\n",
        )
        self.storage.save_note(note)

        target = self.vault_dir / "wiki" / "concepts" / "Relativity.md"
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text("utf-8"), note.serialize())

    def test_save_note_outside_vault_raises(self):
        escaping_note = Note(
            cid="outside",
            rel_path="../outside.md",
            frontmatter={"type": "concept", "title": "Escape"},
            body="# Outside\n",
        )
        with self.assertRaises(ValueError) as ctx:
            self.storage.save_note(escaping_note)
        self.assertIn("Cannot save note outside vault root", str(ctx.exception))

    def test_save_note_expected_hash_conflict(self):
        note = Note(
            cid="wiki/concepts/Conflict",
            rel_path="wiki/concepts/Conflict.md",
            frontmatter={"type": "concept", "title": "Conflict"},
            body="Original body\n",
        )
        self.storage.save_note(note)

        # Update note with wrong expected_hash raises VaultConflictError
        updated_note = Note(
            cid="wiki/concepts/Conflict",
            rel_path="wiki/concepts/Conflict.md",
            frontmatter={"type": "concept", "title": "Conflict"},
            body="Modified body\n",
        )
        with self.assertRaises(VaultConflictError):
            self.storage.save_note(
                updated_note,
                expected_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
            )

        # Saving with correct hash succeeds
        import hashlib

        current_hash = "sha256:" + hashlib.sha256(note.serialize().encode("utf-8")).hexdigest()
        self.storage.save_note(updated_note, expected_hash=current_hash)

        reloaded = self.storage.get_note("wiki/concepts/Conflict")
        self.assertIsNotNone(reloaded)
        assert reloaded is not None
        self.assertEqual(reloaded.body, "Modified body\n")

    def test_note_exists(self):
        note_path = self.vault_dir / "wiki" / "concepts" / "Exists.md"
        note_path.write_text("---\ntype: concept\ntitle: Exists\n---\n# Exists\n", "utf-8")

        self.assertTrue(self.storage.note_exists("wiki/concepts/Exists"))
        self.assertTrue(self.storage.note_exists("wiki/concepts/Exists.md"))
        self.assertFalse(self.storage.note_exists("wiki/concepts/Missing"))
        self.assertFalse(self.storage.note_exists("wiki/concepts"))
        self.assertFalse(self.storage.note_exists("../../outside"))

    def test_list_note_cids(self):
        # Create notes in three distinct cognitive domains
        (self.vault_dir / "wiki" / "concepts" / "NoteA.md").write_text(
            "---\ntype: concept\ntitle: A\n---\n# A\n", "utf-8"
        )
        (self.vault_dir / "customers" / "acme" / "README.md").write_text(
            "---\ntype: account\ntitle: Acme\n---\n# Acme\n", "utf-8"
        )
        (self.vault_dir / "projects" / "apollo" / "RFC.md").write_text(
            "---\ntype: rfc\ntitle: RFC\n---\n# RFC\n", "utf-8"
        )

        cids = self.storage.list_note_cids()
        self.assertEqual(
            cids,
            [
                "customers/acme/README",
                "projects/apollo/RFC",
                "wiki/concepts/NoteA",
            ],
        )

    def test_list_raw_sources(self):
        raw_dir = self.vault_dir / "raw"
        (raw_dir / "paper.pdf").write_bytes(b"%PDF-1.4\n")
        (raw_dir / "notes.txt").write_text("notes\n", "utf-8")
        nested = raw_dir / "sub"
        nested.mkdir(parents=True, exist_ok=True)
        (nested / "data.csv").write_text("a,b,c\n", "utf-8")

        # Files in ignored dirs must be filtered out
        ignored_git = raw_dir / ".git"
        ignored_git.mkdir(parents=True, exist_ok=True)
        (ignored_git / "HEAD").write_text("ref: refs/heads/main\n", "utf-8")

        sources = self.storage.list_raw_sources()
        self.assertIn("raw/paper.pdf", sources)
        self.assertIn("raw/notes.txt", sources)
        self.assertIn("raw/sub/data.csv", sources)
        self.assertNotIn("raw/.git/HEAD", sources)

    def test_list_raw_sources_nonexistent_raw_dir(self):
        import shutil

        shutil.rmtree(self.vault_dir / "raw")
        self.assertEqual(self.storage.list_raw_sources(), [])


class TestFileLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault_dir = Path(self.tmp.name) / "vault"
        self.vault = create_test_vault(self.vault_dir, subdirs=("log",))
        self.ledger = FileLedger(self.vault)

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_creates_ledger_and_records_entry(self):
        self.ledger.append("Action 1")

        log_path = self.vault.log_path
        self.assertTrue(log_path.exists())
        content = log_path.read_text("utf-8")
        self.assertIn("Action 1", content)
        self.assertTrue(content.startswith("- ["))

        self.ledger.append("Action 2", actor="agent:bot")
        lines = log_path.read_text("utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("Action 1", lines[0])
        self.assertIn("agent:bot: Action 2", lines[1])


if __name__ == "__main__":
    unittest.main()

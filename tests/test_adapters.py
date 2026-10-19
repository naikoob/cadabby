"""Unit tests for secondary driven storage adapters conforming to ports (§9.1)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cadabby.adapters.disk_storage import DiskNoteStorage, FileLedger
from cadabby.adapters.memory_storage import InMemoryLedger, InMemoryNoteStorage, InMemoryVaultLayout
from cadabby.cache import VaultCache
from cadabby.domain import Note
from cadabby.fsutil import VaultConflictError
from cadabby.ports import IndexCachePort, LedgerPort, NoteStoragePort, VaultLayoutPort
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
        mem_layout = InMemoryVaultLayout()

        self.assertIsInstance(disk_storage, NoteStoragePort)
        self.assertIsInstance(mem_storage, NoteStoragePort)
        self.assertIsInstance(file_ledger, LedgerPort)
        self.assertIsInstance(mem_ledger, LedgerPort)
        self.assertIsInstance(self.vault, VaultLayoutPort)
        self.assertIsInstance(mem_layout, VaultLayoutPort)
        with VaultCache(self.vault) as cache:
            self.assertIsInstance(cache, IndexCachePort)

    def test_every_adapter_takes_the_parameters_its_port_declares(self):
        """Parameter lists match between each port and each of its adapters (§9.1).

        `@runtime_checkable` checks only that member *names* exist, so
        `IndexCachePort.scan` lacked `regenerate_index`, which `cadabby sync`
        passes, and nothing noticed. Names, kinds and defaults are compared;
        return annotations are not, since an adapter's `list[X]` legitimately
        narrows a port's `Sequence[X]`.
        """
        import inspect

        from cadabby.vault import Vault

        def params(fn: object) -> list[tuple[str, object, object]]:
            sig = inspect.signature(fn)
            return [(p.name, p.kind, p.default) for p in sig.parameters.values()]

        pairs = {
            NoteStoragePort: (DiskNoteStorage, InMemoryNoteStorage),
            LedgerPort: (FileLedger, InMemoryLedger),
            VaultLayoutPort: (Vault, InMemoryVaultLayout),
            IndexCachePort: (VaultCache,),
        }
        checked = 0
        for port, adapters in pairs.items():
            # The port's own methods (typing.get_protocol_members is 3.13+).
            for member, declared in sorted(vars(port).items()):
                if member.startswith("_") or not inspect.isfunction(declared):
                    continue
                for adapter in adapters:
                    with self.subTest(port=port.__name__, member=member, adapter=adapter.__name__):
                        self.assertEqual(params(getattr(adapter, member)), params(declared))
                        checked += 1
        self.assertGreater(checked, 30)  # the loop really ran over every port


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

    def test_get_note_non_utf8_bytes_is_unparseable_and_named(self):
        """C75: the file is at fault, not the caller: FRONTMATTER_UNPARSEABLE with its path."""
        from cadabby.errors import classify
        from cadabby.frontmatter import FrontmatterParseError

        note_path = self.vault_dir / "wiki" / "concepts" / "Binary.md"
        # Write bytes containing invalid UTF-8 byte 0xff
        note_path.write_bytes(
            b"---\ntype: concept\ntitle: Binary\ndescription: Invalid UTF8\nstatus: active\n---\n# Binary\n\xff\n"
        )
        with self.assertRaises(FrontmatterParseError) as ctx:
            self.storage.get_note("wiki/concepts/Binary")
        self.assertEqual(classify(ctx.exception).code, "FRONTMATTER_UNPARSEABLE")
        self.assertIn("wiki/concepts/Binary.md", str(ctx.exception))

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
        self.assertIn("Cannot save note at '../outside.md'", str(ctx.exception))
        self.assertFalse((self.vault.root.parent / "outside.md").exists())

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

    def test_delete_note_checks_hash_and_path(self):
        """Disk and in-memory adapters refuse a stale token, a non-note path, and a missing note alike."""
        note = Note(cid="wiki/Doomed", rel_path="wiki/Doomed.md", frontmatter={"title": "Doomed"}, body="x\n")
        for storage in (self.storage, InMemoryNoteStorage()):
            with self.subTest(adapter=type(storage).__name__):
                storage.save_note(note)
                with self.assertRaises(VaultConflictError):
                    storage.delete_note("wiki/Doomed", expected_hash="sha256:" + "0" * 64)
                self.assertTrue(storage.note_exists("wiki/Doomed"))
                with self.assertRaises(ValueError):
                    storage.delete_note("raw/source.md")
                token = storage.get_note("wiki/Doomed").source_hash
                storage.delete_note("wiki/Doomed", expected_hash=token)
                self.assertFalse(storage.note_exists("wiki/Doomed"))
                with self.assertRaises(FileNotFoundError):
                    storage.delete_note("wiki/Doomed")

    def test_delete_never_follows_a_symlink_and_listing_never_duplicates_one(self):
        """§5.1, §8. Deleting a symlinked note removes the link; the file it points at survives."""
        real = self.vault_dir / "projects" / "real"
        real.mkdir(parents=True)
        (real / "shared.md").write_text("---\ntitle: Shared\n---\nbody\n", "utf-8")
        link = self.vault_dir / "wiki" / "shared-link.md"
        link.symlink_to("../projects/real/shared.md")
        self.assertEqual(self.storage.list_note_cids().count("projects/real/shared"), 1)
        self.storage.delete_note("wiki/shared-link")
        self.assertFalse(link.is_symlink())
        self.assertTrue((real / "shared.md").is_file())

    def test_prune_empty_dirs_removes_only_folders_left_empty(self):
        note = Note(cid="research/papers/x", rel_path="research/papers/x.md", frontmatter={"title": "X"}, body="x\n")
        self.storage.save_note(note)
        (self.vault_dir / "keep").mkdir()
        (self.vault_dir / "keep" / "other.md").write_text("---\ntitle: O\n---\n", "utf-8")
        self.storage.delete_note("research/papers/x", prune_empty_dirs=True)
        self.assertFalse((self.vault_dir / "research").exists())
        self.assertTrue((self.vault_dir / "keep").exists())
        self.assertTrue(self.vault_dir.exists())

    def test_read_raw_returns_text_even_when_frontmatter_does_not_parse(self):
        (self.vault_dir / "wiki" / "Broken.md").write_text("---\ntags: [a, b]\n---\nbody [[x]]\n", "utf-8")
        self.assertIn("body [[x]]", self.storage.read_raw("wiki/Broken") or "")
        self.assertIsNone(self.storage.read_raw("wiki/Missing"))
        mem = InMemoryNoteStorage([Note(cid="wiki/A", rel_path="wiki/A.md", frontmatter={"title": "A"}, body="hi\n")])
        self.assertIn("hi", mem.read_raw("wiki/A") or "")
        self.assertIsNone(mem.read_raw("wiki/Missing"))

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

    def test_iter_raw_files(self):
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

        sources = [self.vault.rel_path(p) for p in self.vault.iter_raw_files()]
        self.assertIn("raw/paper.pdf", sources)
        self.assertIn("raw/notes.txt", sources)
        self.assertIn("raw/sub/data.csv", sources)
        self.assertNotIn("raw/.git/HEAD", sources)

    def test_iter_raw_files_nonexistent_raw_dir(self):
        import shutil

        shutil.rmtree(self.vault_dir / "raw")
        self.assertEqual(list(self.vault.iter_raw_files()), [])

    def test_crlf_note_verifies_and_updates(self):
        """C31: a note saved with CRLF endings verifies and updates without VAULT_CONFLICT."""
        from cadabby.ops import update_note, verify_note

        note_path = self.vault_dir / "wiki" / "Crlf.md"
        text = (
            "---\ntype: concept\ntitle: Crlf\ndescription: Windows line endings\n"
            "status: active\n---\n\n# Crlf\n\nBody.\n"
        )
        note_path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))

        res = verify_note(self.vault, "wiki/Crlf", actor="agent:test")
        self.assertEqual(res["trust_tier"], "machine-confirmed")
        update_note(self.vault, "wiki/Crlf", frontmatter_patch={"description": "Edited"}, actor="agent:test")
        reread = self.storage.get_note("wiki/Crlf")
        self.assertEqual(reread.frontmatter["description"], "Edited")

    def test_an_update_keeps_crlf_and_the_blank_line_after_the_fence(self):
        """C31: editing frontmatter rewrites no body line and leaves the attestation bound."""
        from cadabby.ops import update_note, verify_note

        note_path = self.vault_dir / "wiki" / "Crlf.md"
        text = (
            "---\ntype: concept\ntitle: Crlf\ndescription: Windows line endings\n"
            "status: active\n---\n\n# Crlf\n\nBody.\n"
        )
        note_path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
        verify_note(self.vault, "wiki/Crlf", actor="agent:test")
        update_note(self.vault, "wiki/Crlf", frontmatter_patch={"description": "Edited"}, actor="agent:test")

        raw = note_path.read_bytes().decode("utf-8")
        self.assertNotIn("\n", raw.replace("\r\n", ""), "every line ending is still CRLF")
        self.assertTrue(raw.endswith("---\r\n\r\n# Crlf\r\n\r\nBody.\r\n"), raw)
        self.assertEqual(self.storage.get_note("wiki/Crlf").trust_tier, "machine-confirmed")


class TestSourceReads(unittest.TestCase):
    """§2.4, §5.1: raw/ evidence is readable through the port, and nothing else is."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault_dir = Path(self.tmp.name) / "vault"
        self.vault = create_test_vault(self.vault_dir, subdirs=("wiki", "raw/deep", "raw/.git", "raw/node_modules"))
        self.storage = DiskNoteStorage(self.vault)
        (self.vault_dir / "raw" / "a.md").write_text("alpha", "utf-8")
        (self.vault_dir / "raw" / "deep" / "b.txt").write_text("beta", "utf-8")
        (self.vault_dir / "raw" / ".dot.md").write_text("dot", "utf-8")
        (self.vault_dir / "raw" / ".git" / "c.md").write_text("git", "utf-8")
        (self.vault_dir / "raw" / "node_modules" / "d.md").write_text("nm", "utf-8")
        (self.vault_dir / "wiki" / "Note.md").write_text("---\ntype: concept\n---\nnote", "utf-8")
        outside = Path(self.tmp.name) / "outside.md"
        outside.write_text("outside", "utf-8")
        (self.vault_dir / "raw" / "escape.md").symlink_to(outside)

    def tearDown(self):
        self.tmp.cleanup()

    def test_disk_lists_and_reads_only_raw_evidence(self):
        self.assertEqual(list(self.storage.list_source_paths()), ["raw/a.md", "raw/deep/b.txt"])
        self.assertEqual(self.storage.read_source("raw/a.md"), "alpha")
        self.assertEqual(self.storage.read_source("raw/deep/b.txt"), "beta")
        for refused in (
            "wiki/Note.md", "raw/../wiki/Note.md", "raw/./a.md", "raw//a.md", "raw/.dot.md",
            "raw/.git/c.md", "raw/node_modules/d.md", "raw/escape.md", "raw", "raw/deep", "/etc/passwd",
        ):
            with self.subTest(path=refused):
                self.assertIsNone(self.storage.read_source(refused))

    def test_disk_listing_matches_the_cache_raw_rows(self):
        with VaultCache(self.vault) as cache:
            cache.scan()
            rows = [r[0] for r in cache.get_connection().execute("SELECT rel_path FROM notes WHERE layer = 'raw' ORDER BY rel_path")]
        self.assertEqual(list(self.storage.list_source_paths()), [r for r in rows if r.startswith("raw/")])

    def test_memory_twin_reads_what_was_placed_and_refuses_the_rest(self):
        mem = InMemoryNoteStorage()
        mem.put_source("raw/a.md", "alpha")
        self.assertEqual(list(mem.list_source_paths()), ["raw/a.md"])
        self.assertEqual(mem.read_source("raw/a.md"), "alpha")
        self.assertIsNone(mem.read_source("raw/b.md"))
        for bad in ("wiki/x.md", "raw/../x.md", "raw"):
            with self.subTest(path=bad), self.assertRaises(ValueError):
                mem.put_source(bad, "x")


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

    def test_in_memory_and_disk_storage_canonicalize_and_guard_paths_identically(self):
        disk = DiskNoteStorage(self.vault)
        mem = InMemoryNoteStorage()
        for bad_rel in ("raw/Spoof.md", "index.md", "wiki/AGENTS.md", "/wiki/Abs.md"):
            bad_note = Note(
                cid="wiki/Bad",
                rel_path=bad_rel,
                frontmatter={"type": "concept", "title": "Bad", "description": "d", "status": "active"},
                body="Body\n",
            )
            with self.assertRaises(ValueError):
                disk.save_note(bad_note)
            with self.assertRaises(ValueError):
                mem.save_note(bad_note)

        note = Note(
            cid="wiki/TagParity",
            rel_path="wiki/TagParity.md",
            frontmatter={
                "type": "concept",
                "title": "Tag Parity",
                "description": "Desc",
                "status": "active",
                "tags": ["Storage", "STORAGE", "B_Tree"],
            },
            body="# Tag Parity\n\nBody.\n",
        )
        (self.vault_dir / "wiki").mkdir(parents=True, exist_ok=True)
        disk.save_note(note)
        mem.save_note(note)
        from_disk = disk.get_note("wiki/TagParity")
        from_mem = mem.get_note("wiki/TagParity")
        assert from_disk is not None and from_mem is not None
        self.assertEqual(from_mem.frontmatter, from_disk.frontmatter)
        self.assertEqual(from_mem.tags, ["storage", "b_tree"])


class TestWriteContainment(unittest.TestCase):
    """C72: no engine write lands outside the vault through a symlink (§8).

    A vault is a folder anyone can hand you (a clone, a template, a share).
    Reading through your own symlinked folders stays a feature; writing
    through a link that leaves the vault is refused, and the target untouched.
    """

    NOTE = "---\ntype: concept\ntitle: T\ndescription: d\nstatus: active\n---\n# T\n"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.outside = base / "outside"
        self.outside.mkdir()
        self.victim = self.outside / "victim.txt"
        self.victim.write_text("precious\n", "utf-8")
        self.root = base / "vault"
        self.vault = create_test_vault(self.root, subdirs=("wiki", "customers", "raw"))
        (self.root / "wiki" / "sqlite.md").write_text(self.NOTE, "utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def assertVictimUntouched(self):
        self.assertEqual(self.victim.read_text("utf-8"), "precious\n")

    def test_ledger_symlinked_outside_is_refused(self):
        from cadabby.errors import VaultEscapeError
        from cadabby.indexer import append_vault_log
        from cadabby.ops import update_note

        (self.root / "log.md").symlink_to(self.victim)
        with self.assertRaises(VaultEscapeError):
            append_vault_log(self.vault, "Updated notes/$(touch PWNED).md", actor="process:cli")
        with self.assertRaises(PermissionError):
            update_note(self.vault, "wiki/sqlite", frontmatter_patch={"status": "evergreen"}, actor="agent:t")
        self.assertVictimUntouched()

    def test_index_and_cache_symlinked_outside_are_refused(self):
        from cadabby.errors import VaultEscapeError
        from cadabby.indexer import sync_vault_index

        (self.root / "index.md").symlink_to(self.victim)
        with VaultCache(self.vault) as cache, self.assertRaises(VaultEscapeError):
            sync_vault_index(self.vault, cache)
        self.assertVictimUntouched()

        (self.root / "index.md").unlink()
        (self.root / ".cadabby").symlink_to(self.outside, target_is_directory=True)
        with self.assertRaises(VaultEscapeError):
            VaultCache(self.vault)
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ["victim.txt"])

    def test_note_write_through_a_symlinked_domain_is_refused(self):
        """T2: the containment check is the only thing stopping this write."""
        from cadabby.errors import VaultEscapeError

        escape_dir = self.outside / "evil"
        escape_dir.mkdir()
        (self.root / "customers" / "evil").symlink_to(escape_dir, target_is_directory=True)
        storage = DiskNoteStorage(self.vault)
        note = Note.from_raw("customers/evil/escape", "customers/evil/escape.md", self.NOTE)
        with self.assertRaises(VaultEscapeError) as ctx:
            storage.save_note(note)
        self.assertEqual(ctx.exception.code, "PERMISSION_DENIED")
        self.assertEqual(list(escape_dir.iterdir()), [])

    def test_dangling_link_into_raw_is_refused(self):
        (self.root / "wiki" / "meeting.md").symlink_to(Path("..") / "raw" / "planted.md")
        storage = DiskNoteStorage(self.vault)
        note = Note.from_raw("wiki/meeting", "wiki/meeting.md", self.NOTE)
        with self.assertRaisesRegex(ValueError, "symlink to 'raw/planted.md'"):
            storage.save_note(note)
        self.assertFalse((self.root / "raw" / "planted.md").exists())

    def test_symlinks_inside_the_vault_still_work(self):
        real = self.root / "customers" / "acme"
        real.mkdir()
        (self.root / "customers" / "alias").symlink_to(real, target_is_directory=True)
        storage = DiskNoteStorage(self.vault)
        storage.save_note(Note.from_raw("customers/alias/n", "customers/alias/n.md", self.NOTE))
        self.assertTrue((real / "n.md").is_file())

    def test_a_folder_symlinked_in_from_outside_is_read_under_its_vault_name(self):
        """D2: reads still follow your own symlinks, and the CID is the in-vault name."""
        notes = self.outside / "notes"
        notes.mkdir()
        (notes / "a.md").write_text(self.NOTE, "utf-8")
        (self.root / "docs").symlink_to(notes, target_is_directory=True)
        with VaultCache(self.vault) as cache:
            cache.scan()
            cids = {r["cid"] for r in cache.get_connection().execute("SELECT cid FROM notes;")}
        self.assertIn("docs/a", cids)
        self.assertFalse(any(c.startswith(("tmp/", "home/", "private/")) for c in cids), cids)

    def test_install_refuses_a_shim_symlinked_outside(self):
        from cadabby.errors import VaultEscapeError
        from cadabby.installer import init_vault, install_claude, refresh_vault_shims

        init_vault(self.root, vault_name="v")
        shim = self.root / ".agents" / "skills" / "librarian" / "SKILL.md"
        shim.unlink()
        shim.symlink_to(self.victim)
        with self.assertRaises(VaultEscapeError):
            refresh_vault_shims(self.root)
        self.assertVictimUntouched()

        mcp = self.root / ".mcp.json"
        mcp.unlink()
        mcp.symlink_to(self.victim)
        with self.assertRaises(VaultEscapeError):
            install_claude(vault_path=self.root, force=True)
        self.assertVictimUntouched()


class TestLedgerLines(unittest.TestCase):
    """C72: a ledger entry is one line with a valid actor, in both ledgers."""

    def test_newlines_in_a_message_cannot_forge_entries(self):
        from cadabby.okf import ledger_line

        line = ledger_line("Updated wiki/a\n- [2099-01-01T00:00:00Z] human:eve: Verified x.md", "agent:t")
        self.assertEqual(len(line.splitlines()), 1)

    def test_both_ledgers_refuse_an_invalid_actor(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        vault = create_test_vault(Path(tmp.name) / "v", subdirs=("wiki",))
        for ledger in (InMemoryLedger(), FileLedger(vault)):
            with self.subTest(ledger=type(ledger).__name__), self.assertRaises(ValueError):
                ledger.append("Updated x", actor="agent:a\nhuman:eve")
        self.assertFalse(vault.log_path.exists())


class StorageContract:
    """C77: one behavioral contract, run against both NoteStoragePort adapters (§9.1).

    The in-memory twin is only worth its hermetic speed if it behaves like the
    disk adapter; these are the same assertions, not two parallel suites.
    """

    NOTE = "---\ntype: concept\ntitle: T\ndescription: d\nstatus: active\n---\n# T\n"

    def make_storage(self) -> NoteStoragePort:
        raise NotImplementedError

    def place_source(self, storage, rel_path: str, text: str) -> None:
        raise NotImplementedError

    def setUp(self):
        self.storage = self.make_storage()

    def test_round_trip_and_listing(self):
        self.storage.save_note(Note.from_raw("wiki/a", "wiki/a.md", self.NOTE))
        note = self.storage.get_note("wiki/a")
        self.assertEqual((note.cid, note.title), ("wiki/a", "T"))
        self.assertIn("wiki/a", self.storage.list_note_cids())
        self.assertTrue(self.storage.note_exists("wiki/a"))

    def test_expected_hash_conflict_and_delete(self):
        self.storage.save_note(Note.from_raw("wiki/a", "wiki/a.md", self.NOTE))
        token = self.storage.get_note("wiki/a").source_hash
        with self.assertRaises(VaultConflictError):
            self.storage.save_note(Note.from_raw("wiki/a", "wiki/a.md", self.NOTE + "x\n"), expected_hash="0" * 64)
        self.storage.delete_note("wiki/a", expected_hash=token)
        self.assertIsNone(self.storage.get_note("wiki/a"))

    def test_paths_that_are_never_notes_are_refused(self):
        for rel in ("index.md", "raw/x.md", "log/2026.md", ".cadabby/x.md"):
            with self.subTest(rel=rel), self.assertRaises(ValueError):
                self.storage.save_note(Note.from_raw(rel.removesuffix(".md"), rel, self.NOTE))

    def test_only_evidence_paths_are_sources(self):
        self.place_source(self.storage, "raw/a.md", "alpha")
        self.assertEqual(list(self.storage.list_source_paths()), ["raw/a.md"])
        for rel in ("raw/.hidden.md", "raw/.git/c.md", "raw/node_modules/d.md", "raw\\a.md", "raw/../x.md", "wiki/a.md"):
            with self.subTest(rel=rel):
                self.assertIsNone(self.storage.read_source(rel))
        self.assertEqual(self.storage.read_source("raw/a.md"), "alpha")
        self.assertEqual(self.storage.read_source("raw/a.md", max_bytes=2), "al")


class TestDiskStorageContract(StorageContract, unittest.TestCase):
    def make_storage(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = create_test_vault(Path(self.tmp.name) / "v", subdirs=("wiki", "raw", "log"))
        return DiskNoteStorage(self.vault)

    def place_source(self, storage, rel_path, text):
        path = self.vault.root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8")


class TestMemoryStorageContract(StorageContract, unittest.TestCase):
    def make_storage(self):
        return InMemoryNoteStorage()

    def place_source(self, storage, rel_path, text):
        storage.put_source(rel_path, text)

    def test_the_twin_cannot_hold_sources_disk_would_never_list(self):
        storage = InMemoryNoteStorage()
        for rel in ("raw/.hidden.md", "raw/.git/c.md", "raw/node_modules/d.md", "raw\\a.md"):
            with self.subTest(rel=rel), self.assertRaises(ValueError):
                storage.put_source(rel, "x")


class LayoutContract:
    """C77: one behavioral contract for `VaultLayoutPort` across `Vault` and `InMemoryVaultLayout` (§9.1).

    Exercises `ScaffoldNoteUseCase`, `MoveNoteUseCase`, and `GroundNotesUseCase`
    through both layout implementations so domain contracts, templates, folder
    collisions, leftover files, and `raw_text_extensions` cannot drift between
    disk and hermetic in-memory tests.
    """

    def make_env(self) -> tuple[NoteStoragePort, LedgerPort, VaultLayoutPort]:
        raise NotImplementedError

    def place_domain(self, name: str, manifest: str) -> None:
        raise NotImplementedError

    def place_template(self, name: str, raw_text: str) -> None:
        raise NotImplementedError

    def place_source(self, rel_path: str, text: str) -> None:
        raise NotImplementedError

    def place_folder(self, rel_path: str) -> None:
        raise NotImplementedError

    def place_file(self, rel_path: str, text: str = "x") -> None:
        raise NotImplementedError

    def setUp(self):
        self.storage, self.ledger, self.layout = self.make_env()

    def test_layout_enforces_domain_contracts_and_directives(self):
        from cadabby.ops import GroundNotesUseCase, MoveNoteUseCase, ScaffoldNoteUseCase

        self.place_domain("customers", "---\nallowed_types:\n  - account\n---\nAlways cite a call.\n")
        self.place_domain("broken", "---\nallowed_types: [a, b]\n---\n")

        scaffold = ScaffoldNoteUseCase(self.storage, self.ledger, vault=self.layout)
        move = MoveNoteUseCase(self.storage, self.ledger, vault=self.layout)
        ground = GroundNotesUseCase(self.storage, vault=self.layout)

        with self.assertRaisesRegex(ValueError, "allowed types"):
            scaffold.execute("Bad", "concept", "d", domain="customers")
        with self.assertRaisesRegex(ValueError, "AGENTS.md is invalid"):
            scaffold.execute("Bad", "concept", "d", domain="broken")

        acc = scaffold.execute("Acme", "account", "d", domain="customers")
        wiki_note = scaffold.execute("SQLite", "concept", "d", domain="wiki")
        self.assertEqual(acc.cid, "customers/acme")

        with self.assertRaisesRegex(ValueError, "allowed types"):
            move.plan("wiki/sqlite", "customers/sqlite")
        with self.assertRaisesRegex(ValueError, "AGENTS.md is invalid"):
            move.plan("wiki/sqlite", "broken/sqlite")

        grounded = {g["cid"]: g for g in ground.execute(["customers/acme", "wiki/sqlite"])}
        self.assertEqual(grounded["customers/acme"]["directives"], "Always cite a call.")
        self.assertEqual(grounded["wiki/sqlite"]["directives"], "")

    def test_layout_raw_text_extensions_and_templates(self):
        from cadabby.ops import GroundNotesUseCase, ScaffoldNoteUseCase

        self.place_source("raw/img.png", "binary-bytes")
        self.place_source("raw/notes.md", "# Raw Heading\n\nEvidence.\n")
        ground = GroundNotesUseCase(self.storage, vault=self.layout)
        items = {g["path"]: g for g in ground.execute(["raw/img.png", "raw/notes.md"])}
        self.assertTrue(items["raw/img.png"]["filename_only"])
        self.assertIsNone(items["raw/img.png"]["content"])
        self.assertFalse(items["raw/notes.md"]["filename_only"])
        self.assertIn("Evidence.", items["raw/notes.md"]["content"] or "")

        self.place_template("concept", "---\ntitle: {{title}}\nverified: [bad]\n---\n# {{title}}\n\n## Summary\n")
        scaffold = ScaffoldNoteUseCase(self.storage, self.ledger, vault=self.layout)
        seeded = scaffold.execute("B-Tree", "concept", "d", template="concept")
        self.assertIn("# B-Tree\n\n## Summary", seeded.body)
        self.assertEqual(seeded.verified, [])
        with self.assertRaisesRegex(ValueError, "template folder"):
            scaffold.execute("Inside Templates", "concept", "d", domain="templates")

    def test_layout_folders_and_leftover_files_on_move(self):
        from cadabby.ops import MoveNoteUseCase, ScaffoldNoteUseCase

        self.place_folder("customers/empty")
        self.place_file("customers/acme/logo.png", "png")
        scaffold = ScaffoldNoteUseCase(self.storage, self.ledger, vault=self.layout)
        scaffold.execute("Wiki Note", "concept", "d", domain="wiki")
        scaffold.execute("Acme Deal", "concept", "d", domain="customers", path="customers/acme/deal")

        move = MoveNoteUseCase(self.storage, self.ledger, vault=self.layout)
        with self.assertRaisesRegex(ValueError, "existing folder"):
            move.plan("wiki/wiki-note", "customers/empty")

        plan = move.plan("customers/acme/", "customers/beta/")
        self.assertTrue(any("customers/acme/logo.png" in w for w in plan.warnings), plan.warnings)


class TestDiskLayoutContract(LayoutContract, unittest.TestCase):
    def make_env(self):
        import json

        self._json = json
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = create_test_vault(Path(self.tmp.name) / "v", subdirs=("wiki", "raw", "log"))
        return DiskNoteStorage(self.vault), FileLedger(self.vault), self.vault

    def place_domain(self, name: str, manifest: str) -> None:
        d = self.vault.root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "AGENTS.md").write_text(manifest, "utf-8")

    def place_template(self, name: str, raw_text: str) -> None:
        obs = self.vault.root / ".obsidian"
        obs.mkdir(parents=True, exist_ok=True)
        (obs / "templates.json").write_text(self._json.dumps({"folder": "templates"}), "utf-8")
        tpl = self.vault.root / "templates"
        tpl.mkdir(parents=True, exist_ok=True)
        fname = name if name.endswith(".md") else f"{name}.md"
        (tpl / fname).write_text(raw_text, "utf-8")

    def place_source(self, rel_path: str, text: str) -> None:
        p = self.vault.root / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, "utf-8")

    def place_folder(self, rel_path: str) -> None:
        (self.vault.root / rel_path).mkdir(parents=True, exist_ok=True)

    def place_file(self, rel_path: str, text: str = "x") -> None:
        p = self.vault.root / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, "utf-8")


class TestMemoryLayoutContract(LayoutContract, unittest.TestCase):
    def make_env(self):
        storage = InMemoryNoteStorage()
        return storage, InMemoryLedger(), storage.layout

    def place_domain(self, name: str, manifest: str) -> None:
        self.layout.put_domain(name, manifest)

    def place_template(self, name: str, raw_text: str) -> None:
        self.layout.put_template(name, raw_text)

    def place_source(self, rel_path: str, text: str) -> None:
        self.storage.put_source(rel_path, text)

    def place_folder(self, rel_path: str) -> None:
        self.layout.put_folder(rel_path)

    def place_file(self, rel_path: str, text: str = "x") -> None:
        self.layout.put_file(rel_path, text)


if __name__ == "__main__":
    unittest.main()

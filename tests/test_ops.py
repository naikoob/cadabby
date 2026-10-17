"""Unit tests for ops.py: scaffolding, update, verify, and ground."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cadabby.adapters.disk_storage import DiskNoteStorage, FileLedger
from cadabby.adapters.memory_storage import InMemoryLedger, InMemoryNoteStorage
from cadabby.cache import VaultCache
from cadabby.domain import Note
from cadabby.errors import AmbiguousNoteError, HumanAttestationRefusedError
from cadabby.frontmatter import FrontmatterSerializeError, parse_frontmatter
from cadabby.fsutil import VaultConflictError, compute_file_sha256
from cadabby.lint import run_vault_lint
from cadabby.mcp import McpServer
from cadabby.ops import (
    ScaffoldNoteUseCase,
    UpdateNoteUseCase,
    VerifyNoteUseCase,
    _truncate_to_budget,
    ground_notes,
    resolve_note_cid,
    sanitize_filename,
    scaffold_note,
    update_note,
    verify_note,
)
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
        target = self.vault.wiki_dir / "sqlite.md"
        before = target.read_text("utf-8")

        with self.assertRaises(FrontmatterSerializeError):
            update_note(
                vault=self.vault,
                cid_or_path="wiki/sqlite",
                frontmatter_patch={"provenance": {"import": {"tool": "zotero"}}},
                actor="agent:claude-test",
            )

        self.assertEqual(target.read_text("utf-8"), before)
        # Still parseable, so still indexable.
        self.assertTrue(parse_frontmatter(target.read_text("utf-8"))[0])

        # A legal one-level mapping is unaffected by the guard.
        update_note(
            vault=self.vault,
            cid_or_path="wiki/sqlite",
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
        cid = "wiki/flash-attention"
        note_file = self.vault.wiki_dir / "flash-attention.md"
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
        cid = "wiki/flash-attention"

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
        cids = ["wiki/epistemic-trust-tiers", "wiki/sqlite"]
        grounded = ground_notes(self.vault, cids, budget_tokens=5000)

        self.assertEqual(len(grounded), 2)
        epistemic = next(g for g in grounded if "epistemic-trust-tiers" in g["cid"])
        self.assertEqual(epistemic["trust_tier"], "human-reviewed")
        self.assertGreater(len(epistemic["links"]), 0)
        self.assertFalse(epistemic["truncated"])

    def test_ground_notes_stem_and_wikilink_resolution(self):
        # 1. Bare note stems
        grounded_stems = ground_notes(self.vault, ["epistemic-trust-tiers", "sqlite"])
        self.assertEqual(len(grounded_stems), 2)
        cids = [g["cid"] for g in grounded_stems]
        self.assertIn("wiki/epistemic-trust-tiers", cids)
        self.assertIn("wiki/sqlite", cids)

        # 2. Wikilink syntax with anchor and alias
        grounded_wiki = ground_notes(
            self.vault,
            ["[[epistemic-trust-tiers#Epistemic Trust Tiers]]", "[[sqlite|SQLite Database]]"],
        )
        self.assertEqual(len(grounded_wiki), 2)
        cids_wiki = [g["cid"] for g in grounded_wiki]
        self.assertIn("wiki/epistemic-trust-tiers", cids_wiki)
        self.assertIn("wiki/sqlite", cids_wiki)
        self.assertEqual(grounded_wiki[0]["section"], "Epistemic Trust Tiers")
        self.assertIsNone(grounded_wiki[1]["section"])

        # 3. Relative path suffixes and .md extension
        grounded_paths = ground_notes(
            self.vault,
            ["epistemic-trust-tiers.md", "wiki/sqlite.md"],
        )
        self.assertEqual(len(grounded_paths), 2)
        cids_paths = [g["cid"] for g in grounded_paths]
        self.assertIn("wiki/epistemic-trust-tiers", cids_paths)
        self.assertIn("wiki/sqlite", cids_paths)

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

    def test_ground_notes_section_anchor_extracts_target_section_and_subsections(self):
        """§5.1, §10 C56: #section grounding extracts matching ATX heading and nested subsections, ignores fenced headings, and rejects missing anchors."""
        note_path = self.vault_root / "wiki" / "storage-engine.md"
        note_path.write_text(
            "---\n"
            "type: concept\n"
            "title: Storage Engine\n"
            "description: B-Tree and WAL internals\n"
            "status: active\n"
            "---\n"
            "# Storage Engine\n\n"
            "Overview paragraph.\n\n"
            "## B-Tree Indexes (v2)\n\n"
            "B-Tree body text.\n\n"
            "```markdown\n"
            "## Fake Heading Inside Fence\n"
            "```\n\n"
            "### Page Splitting\n\n"
            "Nested subsection under B-Tree.\n\n"
            "## Write-Ahead Log\n\n"
            "WAL section text.\n",
            "utf-8",
        )

        # 1. Exact heading match and GitHub-slug match both extract the ## section + ### subsection, stopping before next ##
        for target in (
            "wiki/storage-engine#B-Tree Indexes (v2)",
            "[[storage-engine#b-tree-indexes-v2|B-Tree]]",
        ):
            grounded = ground_notes(self.vault, [target])
            self.assertEqual(len(grounded), 1)
            item = grounded[0]
            self.assertEqual(item["cid"], "wiki/storage-engine")
            self.assertEqual(item["section"], "B-Tree Indexes (v2)")
            self.assertIn("## B-Tree Indexes (v2)", item["content"])
            self.assertIn("## Fake Heading Inside Fence", item["content"])
            self.assertIn("### Page Splitting", item["content"])
            self.assertNotIn("Overview paragraph.", item["content"])
            self.assertNotIn("## Write-Ahead Log", item["content"])

        # 2. Deeper subsection match extracts only the ### subsection
        sub_grounded = ground_notes(self.vault, ["storage-engine#Page Splitting"])
        self.assertEqual(sub_grounded[0]["section"], "Page Splitting")
        self.assertIn("### Page Splitting", sub_grounded[0]["content"])
        self.assertNotIn("B-Tree body text.", sub_grounded[0]["content"])
        self.assertNotIn("## Write-Ahead Log", sub_grounded[0]["content"])

        # 3. Missing anchor raises ValueError (INVALID_ARGUMENT) naming available headings
        with self.assertRaises(ValueError) as ctx:
            ground_notes(self.vault, ["wiki/storage-engine#Nonexistent"])
        self.assertIn("B-Tree Indexes (v2)", str(ctx.exception))
        self.assertIn("Write-Ahead Log", str(ctx.exception))

        # 4. Legacy filename containing literal '#' still resolves as full note before anchor splitting
        csharp_path = self.vault_root / "wiki" / "C#-Basics.md"
        csharp_path.write_text(
            "---\ntype: concept\ntitle: C# Basics\ndescription: CSharp\nstatus: active\n---\n# C# Basics\n",
            "utf-8",
        )
        cs_grounded = ground_notes(self.vault, ["wiki/C#-Basics"])
        self.assertEqual(cs_grounded[0]["cid"], "wiki/C#-Basics")
        self.assertIsNone(cs_grounded[0]["section"])

    def test_ground_notes_enriches_one_hop_links_and_backlinks_with_neighbor_metadata(self):
        """§5.1, §10 C56: Grounded 1-hop links and backlinks include neighbor title, type, status, trust_tier, and description."""
        # Add a dead link to sqlite.md so we test both resolved and unresolved forward links
        sqlite_path = self.vault_root / "wiki" / "sqlite.md"
        sqlite_text = sqlite_path.read_text("utf-8")
        sqlite_path.write_text(sqlite_text + "\nSee also [[nonexistent-target]].\n", "utf-8")

        grounded = ground_notes(self.vault, ["wiki/sqlite"])
        self.assertEqual(len(grounded), 1)
        item = grounded[0]

        # Resolved forward link to wiki/epistemic-trust-tiers carries full neighbor metadata
        resolved_link = next(l for l in item["links"] if l["resolved_cid"] == "wiki/epistemic-trust-tiers")
        self.assertEqual(resolved_link["title"], "Epistemic Trust Tiers")
        self.assertEqual(resolved_link["type"], "concept")
        self.assertEqual(resolved_link["status"], "active")
        self.assertEqual(resolved_link["trust_tier"], "human-reviewed")
        self.assertTrue(resolved_link["description"])

        # Unresolved forward link has None for neighbor metadata fields
        dead_link = next(l for l in item["links"] if l["target"] == "nonexistent-target")
        self.assertIsNone(dead_link["resolved_cid"])
        self.assertIsNone(dead_link["title"])
        self.assertIsNone(dead_link["trust_tier"])
        self.assertIsNone(dead_link["description"])

        # Backlink from wiki/sqlite-vs-duckdb carries full neighbor metadata
        backlink = next(b for b in item["backlinks"] if b["source_cid"] == "wiki/sqlite-vs-duckdb")
        self.assertEqual(backlink["title"], "SQLite vs DuckDB")
        self.assertEqual(backlink["type"], "comparison")
        self.assertEqual(backlink["status"], "deprecated")
        self.assertEqual(backlink["trust_tier"], "unverified")
        self.assertTrue(backlink["description"])

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
        self.assertEqual(moc_path.name, "distributed-systems-moc.md")
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
        self.assertEqual(adr_path.name, "adr-001-event-sourcing.md")
        fm, _ = parse_frontmatter(adr_path.read_text("utf-8"))
        self.assertEqual(fm["type"], "adr")

        grounded = ground_notes(self.vault, ["adr-001-event-sourcing"])
        self.assertEqual(len(grounded), 1)
        self.assertEqual(grounded[0]["type"], "adr")
        self.assertEqual(grounded[0]["cid"], "wiki/adr-001-event-sourcing")

    def test_default_stem_is_lowercase_kebab_case(self):
        """§5.1, §10 C42. Without `path`, the stem is the title in lowercase kebab-case (STYLE.md)."""
        self.assertEqual(sanitize_filename("Cloud Spanner Multi-Region Quorum Latency (nam11)"), "cloud-spanner-multi-region-quorum-latency-nam11")
        self.assertEqual(sanitize_filename("snake_case  Title -- v2"), "snake-case-title-v2")
        self.assertEqual(sanitize_filename("!!!"), "untitled")

        note = ScaffoldNoteUseCase(InMemoryNoteStorage(), InMemoryLedger()).execute(
            title="Flash Attention", type_="concept", description="d"
        )
        self.assertEqual(note.rel_path, "wiki/flash-attention.md")

    def test_scaffold_refuses_case_only_collision(self):
        """§5.1, §10 C42. A stem differing from an existing note only by case is refused on disk and in memory."""
        (self.vault.wiki_dir / "flash-attention.md").rename(self.vault.wiki_dir / "Flash-Attention.md")
        with self.assertRaisesRegex(FileExistsError, "only by letter case"):
            scaffold_note(self.vault, title="Flash Attention", type_="concept", description="d")
        self.assertFalse((self.vault.wiki_dir / "flash-attention.md").exists())

        uc = ScaffoldNoteUseCase(InMemoryNoteStorage(), InMemoryLedger())
        uc.execute(title="Twin", type_="concept", description="d", path="wiki/Twin")
        with self.assertRaisesRegex(FileExistsError, "wiki/Twin.md"):
            uc.execute(title="Twin", type_="concept", description="d")

    def test_scaffold_never_overwrites_a_note_reached_through_a_symlinked_folder(self):
        """§5.1. The listing skips symlinked folders; the existence check must still see their notes."""
        real = self.vault_root / "projects" / "real"
        real.mkdir(parents=True)
        original = "---\ntype: account\ntitle: Real\ndescription: d\nstatus: active\n---\nIrreplaceable notes.\n"
        (real / "account.md").write_text(original, "utf-8")
        (self.vault_root / "customers").mkdir()
        os.symlink("../projects/real", self.vault_root / "customers" / "acme", target_is_directory=True)

        with self.assertRaisesRegex(FileExistsError, "already exists"):
            scaffold_note(self.vault, "Account", "account", "new", path="customers/acme/account", body="# Overwritten\n")
        self.assertEqual((real / "account.md").read_text("utf-8"), original)

    def test_names_resolve_to_one_canonical_note_or_are_refused(self):
        """§5.1, §10 C49. Exact path first, then a unique case-insensitive path, then wikilink semantics."""
        storage = DiskNoteStorage(self.vault)
        self.assertEqual(resolve_note_cid(storage, "wiki/Flash-Attention"), "wiki/flash-attention")
        self.assertEqual(resolve_note_cid(storage, "[[flash-attention#Tiling|FA]]"), "wiki/flash-attention")
        self.assertEqual(resolve_note_cid(storage, "./wiki/sqlite.md"), "wiki/sqlite")

        for rel in ("customers/acme/README.md", "projects/apollo/README.md"):
            (self.vault_root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.vault_root / rel).write_text("---\ntype: moc\ntitle: Hub\ndescription: d\nstatus: active\n---\n# Hub\n", "utf-8")
        before = {p: p.read_bytes() for p in self.vault_root.rglob("README.md")}
        for call in (
            lambda: update_note(self.vault, "README", frontmatter_patch={"description": "x"}),
            lambda: verify_note(self.vault, "README", actor="agent:t"),
        ):
            with self.assertRaises(AmbiguousNoteError) as ctx:
                call()
            self.assertEqual(ctx.exception.candidates, ["customers/acme/README", "projects/apollo/README"])
        self.assertEqual({p: p.read_bytes() for p in self.vault_root.rglob("README.md")}, before)
        # An exact path always works, and a wiki-relative name is canonical (§4.4), never ambiguous.
        update_note(self.vault, "projects/apollo/README", frontmatter_patch={"description": "x"})
        (self.vault_root / "customers" / "acme" / "sqlite.md").write_text(
            "---\ntype: concept\ntitle: Twin\ndescription: d\nstatus: active\n---\n# Twin\n", "utf-8"
        )
        self.assertEqual(resolve_note_cid(storage, "sqlite"), "wiki/sqlite")
        with self.assertRaises(FileNotFoundError):
            resolve_note_cid(storage, "../outside")

    def test_filename_containing_hash_is_addressable(self):
        """§5.1. The exact path is tried before the name is read as wikilink syntax."""
        (self.vault.wiki_dir / "C#-Basics.md").write_text(
            "---\ntype: concept\ntitle: C#\ndescription: d\nstatus: active\n---\n# C#\n", "utf-8"
        )
        path = update_note(self.vault, "wiki/C#-Basics", frontmatter_patch={"description": "edited"})
        self.assertEqual(path.name, "C#-Basics.md")

    def test_patch_frontmatter_never_writes_engine_owned_fields(self):
        """§3.4, §10 C47. `verified` and `generated` are written by the engine alone."""
        note = self.vault.wiki_dir / "flash-attention.md"
        before = note.read_bytes()
        forged = [{"by": "human:mallory", "at": "2026-01-01T00:00:00Z", "of": "sha256:" + "0" * 64}]
        with self.assertRaises(HumanAttestationRefusedError):
            update_note(self.vault, "wiki/flash-attention", frontmatter_patch={"verified": forged})
        for patch_ in ({"verified": None}, {"verified": [{"by": "agent:x", "of": "sha256:1"}]}, {"generated": {"by": "agent:x"}}):
            with self.subTest(patch=patch_), self.assertRaisesRegex(ValueError, "engine-owned"):
                update_note(self.vault, "wiki/flash-attention", frontmatter_patch=patch_)
        self.assertEqual(note.read_bytes(), before)

        with McpServer(self.vault) as server:
            res = server.handle_tools_call("vault_update_note", {"cid": "wiki/flash-attention", "patch_frontmatter": {"verified": forged}})
        self.assertEqual(json.loads(res["content"][0]["text"])["error"]["code"], "HUMAN_ATTESTATION_REFUSED")
        self.assertEqual(note.read_bytes(), before)

    def test_list_valued_core_fields_never_break_the_scan(self):
        """§3.1, §10 C48. A list title is indexed as absent and reported by gate 1; writes refuse to create one."""
        (self.vault.wiki_dir / "Listy.md").write_text(
            "---\ntype: concept\ntitle:\n  - one\n  - two\ndescription: d\nstatus: active\n---\n# Listy [[sqlite]]\n", "utf-8"
        )
        with VaultCache(self.vault) as cache:
            cache.scan()
            self.assertGreater(cache.get_status()["total_notes"], 0)
        codes = {f.code for f in run_vault_lint(self.vault) if f.rel_path == "wiki/Listy.md"}
        self.assertIn("ENUM_INVALID", codes)

        with self.assertRaisesRegex(ValueError, "'type' must be a string"):
            update_note(self.vault, "wiki/sqlite", frontmatter_patch={"type": ["a", "b"]})
        with self.assertRaisesRegex(ValueError, "description must be a string"):
            scaffold_note(self.vault, "Listy Two", "concept", ["one", "two"])  # type: ignore[arg-type]
        self.assertFalse((self.vault.wiki_dir / "listy-two.md").exists())

    def test_implicit_occ_detects_concurrent_modification_on_update_and_verify(self):
        base_storage = DiskNoteStorage(self.vault)
        ledger = FileLedger(self.vault)
        note_path = self.vault.wiki_dir / "flash-attention.md"

        class RacingStorage(DiskNoteStorage):
            def get_note(self_inner, cid_or_path: str):
                n = super().get_note(cid_or_path)
                # Simulate another writer modifying the file after get_note() but before save_note()
                note_path.write_text(note_path.read_text("utf-8") + "\nConcurrent edit.\n", "utf-8")
                return n

        racing = RacingStorage(self.vault)
        with self.assertRaises(VaultConflictError):
            UpdateNoteUseCase(racing, ledger).execute(
                cid_or_path="wiki/flash-attention",
                frontmatter_patch={"description": "Should fail due to concurrent edit"},
            )

        with self.assertRaises(VaultConflictError):
            VerifyNoteUseCase(racing, ledger).execute(
                cid_or_path="wiki/flash-attention",
                actor="agent:verifier",
            )
        self.assertIsNotNone(base_storage.get_note("wiki/flash-attention"))

    def test_ground_notes_closes_temporary_cache(self):
        closed_instances: list[VaultCache] = []
        orig_close = VaultCache.close

        def tracking_close(cache_self: VaultCache) -> None:
            closed_instances.append(cache_self)
            orig_close(cache_self)

        with patch.object(VaultCache, "close", autospec=True, side_effect=tracking_close):
            grounded = ground_notes(self.vault, ["wiki/sqlite"], cache=None)
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
        uc = ScaffoldNoteUseCase(InMemoryNoteStorage(), InMemoryLedger())
        for bad_path in ("./Note", "customers/./Note", "  /abs/Note"):
            with self.subTest(bad_path=bad_path), self.assertRaisesRegex(ValueError, "Path traversal"):
                uc.execute(title="Dot", type_="concept", description="d", path=bad_path)
        with self.assertRaisesRegex(ValueError, "path must be a string"):
            uc.execute(title="Typed", type_="concept", description="d", path=5)  # type: ignore[arg-type]

        clean = uc.execute(title="Clean", type_="concept", description="d", domain="customers", path="customers//acme//Note")
        self.assertEqual(clean.rel_path, "customers/acme/Note.md")

    def test_empty_path_means_omitted_and_trailing_slash_names_a_folder(self):
        """§5.1. Clients that send "" for unset strings get a title-derived note, as without `path`."""
        uc = ScaffoldNoteUseCase(InMemoryNoteStorage(), InMemoryLedger())
        self.assertEqual(uc.execute(title="Empty Path", type_="concept", description="d", path="").rel_path, "wiki/empty-path.md")
        self.assertEqual(uc.execute(title="Blank Path", type_="concept", description="d", path="  ").rel_path, "wiki/blank-path.md")
        folder = uc.execute(title="Acme Sizing", type_="concept", description="d", path="customers/acme/")
        self.assertEqual(folder.rel_path, "customers/acme/acme-sizing.md")

    def test_verify_note_idempotency_sets_already_verified_and_skips_second_write(self):
        first = verify_note(self.vault, "wiki/Edits-Fixture", actor="agent:test")
        self.assertFalse(first["already_verified"])
        log_after_first = self._log_lines()

        second = verify_note(self.vault, "wiki/Edits-Fixture", actor="agent:test")
        self.assertTrue(second["already_verified"])
        self.assertEqual(self._log_lines(), log_after_first)

    def test_update_and_verify_resolve_bare_stems_and_wikilinks(self):
        """§5.1. update_note and verify_note accept bare stems and [[wikilinks#Anchor|Alias]] like ground_notes."""
        updated = update_note(
            self.vault,
            cid_or_path="[[Edits-Fixture#Alpha|Fixture]]",
            frontmatter_patch={"description": "Resolved via wikilink"},
        )
        self.assertEqual(updated, self.note)
        res = verify_note(self.vault, cid_or_path="Edits-Fixture", actor="agent:test")
        self.assertEqual(res["cid"], "wiki/Edits-Fixture")


if __name__ == "__main__":
    unittest.main()



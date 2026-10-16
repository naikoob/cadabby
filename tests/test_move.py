"""vault_move_note: link rewriting, trust impact, blockers, rollback (§5.1)."""

from __future__ import annotations

import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from cadabby.adapters.disk_storage import DiskNoteStorage, FileLedger
from cadabby.adapters.memory_storage import InMemoryLedger, InMemoryNoteStorage
from cadabby.cache import VaultCache
from cadabby.domain import Note
from cadabby.errors import AmbiguousNoteError
from cadabby.fsutil import VaultConflictError
from cadabby.lint import run_vault_lint
from cadabby.mcp import McpServer
from cadabby.ops import MoveNoteUseCase, move_note, verify_note
from cadabby.vault import Vault
from tests.helpers import copy_demo_vault


def _fm(title: str, type_: str = "concept") -> str:
    return f"---\ntype: {type_}\ntitle: {title}\ndescription: d\nstatus: active\n---\n"


LINKER_BODY = """# Linker

- plain [[target-note]]
- alias [[target-note|Target]]
- anchor [[target-note#Heading]]
- embed ![[target-note#Heading]]
- md [md](target-note.md#Heading) and [angle](<target-note.md>)

| col | link |
| --- | --- |
| a | [[target-note\\|Aliased]] |

Code `[[target-note]]` is not a link.

```
[[target-note]]
```
"""

LINKER_AFTER = """# Linker

- plain [[renamed-note]]
- alias [[renamed-note|Target]]
- anchor [[renamed-note#Heading]]
- embed ![[renamed-note#Heading]]
- md [md](../customers/acme/renamed-note.md#Heading) and [angle](<../customers/acme/renamed-note.md>)

| col | link |
| --- | --- |
| a | [[renamed-note\\|Aliased]] |

Code `[[target-note]]` is not a link.

```
[[target-note]]
```
"""


class TestMoveNote(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = copy_demo_vault(Path(self.tmp.name) / "demo-vault")
        self.vault = Vault(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8")
        return path

    def _snapshot(self) -> dict[str, str]:
        return {
            p.relative_to(self.root).as_posix(): p.read_text("utf-8")
            for p in sorted(self.root.rglob("*.md"))
            if ".cadabby" not in p.parts
        }

    def _target_and_linker(self) -> None:
        self._write("wiki/target-note.md", _fm("Target") + "# Target\n\n## Heading\n\nText.\n")
        self._write("wiki/linker.md", _fm("Linker") + LINKER_BODY)

    def test_dry_run_writes_nothing(self):
        """§10 C44. The plan lists every change; nothing on disk moves until apply."""
        self._target_and_linker()
        before = self._snapshot()
        plan = move_note(self.vault, "wiki/target-note", "customers/acme/renamed-note")
        self.assertFalse(plan["applied"])
        self.assertEqual([n["cid"] for n in plan["notes"]], ["wiki/linker"])
        self.assertEqual(self._snapshot(), before)

    def test_rewrites_every_link_form_and_nothing_else(self):
        """§10 C44. Wikilinks, aliases, anchors, embeds, escaped table pipes and Markdown links; code is untouched."""
        self._target_and_linker()
        plan = move_note(self.vault, "wiki/target-note", "customers/acme/renamed-note", apply=True)
        self.assertEqual(plan["notes"][0]["links_rewritten"], 7)
        self.assertFalse((self.root / "wiki/target-note.md").exists())
        self.assertTrue((self.root / "customers/acme/renamed-note.md").exists())
        linker = (self.root / "wiki/linker.md").read_text("utf-8")
        self.assertTrue(linker.endswith(LINKER_AFTER), linker)
        errors = [f for f in run_vault_lint(self.vault) if f.severity == "error"]
        self.assertEqual(errors, [])

    def test_links_that_still_resolve_are_left_alone(self):
        """§10 C44. Same stem in a new folder: bare wikilinks still open it, so no referrer goes stale."""
        self._write("wiki/target-note.md", _fm("Target") + "# Target\n")
        self._write("wiki/linker.md", _fm("Linker") + "See [[target-note]].\n")
        before = (self.root / "wiki/linker.md").read_text("utf-8")
        plan = move_note(self.vault, "wiki/target-note", "customers/acme/target-note", apply=True)
        self.assertEqual(plan["notes"], [])
        self.assertEqual((self.root / "wiki/linker.md").read_text("utf-8"), before)

    def test_moved_note_keeps_its_tier_when_its_body_is_unchanged(self):
        """§10 C44. Attestations bind to content, not to the path (§3.4)."""
        self._write("wiki/target-note.md", _fm("Target") + "# Target\n\nNo relative links.\n")
        verify_note(self.vault, "wiki/target-note", actor="human:tester", is_human_authorized=True)
        plan = move_note(self.vault, "wiki/target-note", "customers/acme/target-note", apply=True)
        self.assertEqual(plan["moved_note"]["tier_before"], "human-reviewed")
        self.assertEqual(plan["moved_note"]["tier_after"], "human-reviewed")
        with VaultCache(self.vault) as cache:
            cache.scan()
            row = cache.get_connection().execute(
                "SELECT trust_tier FROM notes WHERE cid = 'customers/acme/target-note'"
            ).fetchone()
        self.assertEqual(row["trust_tier"], "human-reviewed")

    def test_moved_note_own_relative_links_follow_its_new_depth(self):
        """The note's own `raw/` citations and note links still name the same files from the new folder."""
        self._write("wiki/sibling.md", _fm("Sibling") + "# Sibling\n\n[[target-note]]\n")
        self._write(
            "wiki/target-note.md",
            _fm("Target") + "[src](../raw/karpathy-llm-wiki-gist.md) and [sib](sibling.md)\n",
        )
        plan = move_note(self.vault, "wiki/target-note", "customers/acme/target-note", apply=True)
        self.assertEqual(plan["moved_note"]["links_rewritten"], 2)
        body = (self.root / "customers/acme/target-note.md").read_text("utf-8")
        self.assertIn("[src](../../raw/karpathy-llm-wiki-gist.md)", body)
        self.assertIn("[sib](../../wiki/sibling.md)", body)
        codes = {f.code for f in run_vault_lint(self.vault)}
        self.assertNotIn("RAW_LINK_BROKEN", codes)
        self.assertNotIn("LINK_DEAD", codes)

    def test_retargeting_someone_elses_link_is_refused(self):
        """§10 C45. A move into wiki/ would steal a bare link that opens a domain note today."""
        self._write("projects/apollo/architecture.md", _fm("Apollo Architecture") + "# Apollo\n")
        self._write("wiki/reader.md", _fm("Reader") + "See [[architecture]].\n")
        self._write("wiki/design-notes.md", _fm("Design") + "# Design\n")
        before = self._snapshot()
        plan = move_note(self.vault, "wiki/design-notes", "wiki/architecture")
        self.assertEqual(len(plan["blocked"]), 1)
        self.assertIn("projects/apollo/architecture", plan["blocked"][0])
        with self.assertRaisesRegex(ValueError, "Move refused"):
            move_note(self.vault, "wiki/design-notes", "wiki/architecture", apply=True)
        self.assertEqual(self._snapshot(), before)

    def test_human_reviewed_referrer_needs_explicit_approval(self):
        """§10 C45. The demo's human-reviewed note links to flash-attention; renaming it needs the user's OK."""
        plan = move_note(self.vault, "wiki/flash-attention", "fa-tiling")
        self.assertTrue(any("epistemic-trust-tiers" in b for b in plan["blocked"]))
        with self.assertRaises(ValueError):
            move_note(self.vault, "wiki/flash-attention", "fa-tiling", apply=True)

        applied = move_note(
            self.vault,
            "wiki/flash-attention",
            "fa-tiling",
            apply=True,
            approve_stale=["wiki/epistemic-trust-tiers"],
        )
        tiers = {n["cid"]: (n["tier_before"], n["tier_after"]) for n in applied["notes"]}
        self.assertEqual(tiers["wiki/epistemic-trust-tiers"], ("human-reviewed", "stale-verified"))
        names = {p.name for p in (self.root / "wiki").glob("*.md")}
        self.assertIn("fa-tiling.md", names)
        self.assertNotIn("flash-attention.md", names)
        self.assertEqual([f for f in run_vault_lint(self.vault) if f.severity == "error"], [])

    def test_conflict_mid_apply_restores_every_file(self):
        """§10 C45. A referrer edited between plan and apply aborts the move and undoes what was written."""
        self._write("wiki/target-note.md", _fm("Target") + "# Target\n")
        self._write("wiki/a-ref.md", _fm("A") + "[[target-note]]\n")
        self._write("wiki/z-ref.md", _fm("Z") + "[[target-note]]\n")
        storage, ledger = DiskNoteStorage(self.vault), FileLedger(self.vault)
        uc = MoveNoteUseCase(storage, ledger, vault=self.vault)
        plan = uc.plan("wiki/target-note", "wiki/renamed-note")
        before = self._snapshot()
        edited = before["wiki/z-ref.md"] + "\nEdited in Obsidian meanwhile.\n"
        (self.root / "wiki/z-ref.md").write_text(edited, "utf-8")

        with self.assertRaises(VaultConflictError):
            uc.apply(plan, actor="agent:test")

        after = self._snapshot()
        self.assertEqual(after["wiki/target-note.md"], before["wiki/target-note.md"])
        self.assertNotIn("wiki/renamed-note.md", after)
        self.assertIn("[[target-note]]", after["wiki/a-ref.md"])
        self.assertEqual(after["wiki/z-ref.md"], edited, "the concurrent edit survives")

    def test_destination_rules_match_scaffold(self):
        self._write("wiki/target-note.md", _fm("Target") + "# Target\n")
        self._write("customers/AGENTS.md", "---\nallowed_types:\n  - account\n---\n# Customers\n")
        cases = [
            ("wiki/sub/target-note", ValueError, "flat"),
            ("raw/target-note", ValueError, "reserved"),
            ("customers/acme/target-note", ValueError, "allowed types"),
            ("wiki/sqlite", FileExistsError, "already exists"),
            ("wiki/SQLite", FileExistsError, "only by letter case"),
            ("wiki/target-note", ValueError, "already at"),
        ]
        for to, exc, needle in cases:
            with self.subTest(to=to), self.assertRaisesRegex(exc, needle):
                move_note(self.vault, "wiki/target-note", to)

    def test_one_ledger_line_per_move(self):
        self._target_and_linker()
        move_note(self.vault, "wiki/target-note", "customers/acme/renamed-note", apply=True, actor="agent:test")
        log = next(p for p in [self.root / "log.md", *self.root.glob("log/*.md")] if p.exists()).read_text("utf-8")
        moved = [line for line in log.splitlines() if "Moved " in line]
        self.assertEqual(len(moved), 1)
        self.assertIn("wiki/target-note.md -> customers/acme/renamed-note.md (rewrote 7 links in 1 notes)", moved[0])

    def test_mcp_dry_run_then_apply_and_refusal(self):
        self._target_and_linker()
        with McpServer(self.vault) as server:
            dry = server.handle_tools_call("vault_move_note", {"cid": "wiki/target-note", "to": "renamed-note"})
            self.assertFalse(dry["isError"])
            self.assertFalse(json.loads(dry["content"][0]["text"])["applied"])
            done = server.handle_tools_call(
                "vault_move_note", {"cid": "wiki/target-note", "to": "renamed-note", "apply": True}
            )
            self.assertTrue(json.loads(done["content"][0]["text"])["applied"])
            refused = server.handle_tools_call(
                "vault_move_note", {"cid": "wiki/flash-attention", "to": "fa-tiling", "apply": True}
            )
            self.assertTrue(refused["isError"])
            self.assertEqual(json.loads(refused["content"][0]["text"])["error"]["code"], "INVALID_ARGUMENT")

    def test_moved_note_preserves_valid_relative_links_and_trust_tier(self):
        """§5.1. Relative links that still resolve from the new path are left alone, so attestations stay valid."""
        self._write(
            "wiki/target-note.md",
            _fm("Target") + "[sib](./sqlite.md) and [up](../wiki/sqlite.md) and [here](./#top)\n",
        )
        verify_note(self.vault, "wiki/target-note", actor="human:owner", is_human_authorized=True)
        plan = move_note(self.vault, "wiki/target-note", "renamed-note", apply=True)
        self.assertEqual(plan["moved_note"]["links_rewritten"], 0)
        self.assertEqual(plan["moved_note"]["tier_before"], "human-reviewed")
        self.assertEqual(plan["moved_note"]["tier_after"], "human-reviewed")
        body = (self.root / "wiki/renamed-note.md").read_text("utf-8")
        self.assertIn("[sib](./sqlite.md)", body)
        self.assertIn("[up](../wiki/sqlite.md)", body)
        self.assertIn("[here](./#top)", body)

    def test_moved_note_self_link_by_filename_follows_the_rename(self):
        """§5.1. `[self](target-note.md#top)` names the moved note itself, so it names the new file afterwards."""
        self._write("wiki/target-note.md", _fm("Target") + "# Top\n\n[self](target-note.md#top)\n")
        plan = move_note(self.vault, "wiki/target-note", "customers/acme/renamed-note", apply=True)
        self.assertEqual(plan["moved_note"]["links_rewritten"], 1)
        self.assertIn("[self](renamed-note.md#top)", (self.root / "customers/acme/renamed-note.md").read_text("utf-8"))
        self.assertNotIn("LINK_DEAD", {f.code for f in run_vault_lint(self.vault)})

    def test_bare_markdown_link_encodes_parentheses_on_move(self):
        """§5.1. Bare Markdown links percent-encode '(' and ')' so the parser reads the rewritten path back."""
        self._write("wiki/target-note.md", _fm("Target") + "# Target\n")
        self._write("wiki/linker.md", _fm("Linker") + "See [bare](target-note.md#Top) and [angle](<target-note.md#Top>).\n")
        move_note(self.vault, "wiki/target-note", "customers/acme/note(v2)", apply=True)
        linker_body = (self.root / "wiki/linker.md").read_text("utf-8")
        self.assertIn("[bare](../customers/acme/note%28v2%29.md#Top)", linker_body)
        self.assertIn("[angle](<../customers/acme/note(v2).md#Top>)", linker_body)
        self.assertEqual([f for f in run_vault_lint(self.vault) if f.code == "LINK_DEAD"], [])

    def test_destination_rejects_traversal_and_supports_trailing_slash_folder_move(self):
        """§5.1. '.'/'..' segments are refused; a trailing slash moves the note into that folder."""
        self._write("wiki/target-note.md", _fm("Target") + "# Target\n")
        for bad in ("wiki/../customers/x", "./x"):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "Path traversal"):
                move_note(self.vault, "wiki/target-note", bad)
        for bad in ("   ", "///"):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "vault-relative"):
                move_note(self.vault, "wiki/target-note", bad)

        res = move_note(self.vault, "wiki/target-note", "customers/acme/", apply=True)
        self.assertEqual(res["to"], "customers/acme/target-note")
        self.assertTrue((self.root / "customers/acme/target-note.md").is_file())

    def test_destination_naming_an_existing_folder_needs_a_slash_or_md(self):
        """§5.1. `to: customers/acme` with a customers/acme/ folder is refused rather than creating customers/acme.md."""
        self._write("wiki/target-note.md", _fm("Target") + "# Target\n")
        self._write("customers/acme/README.md", _fm("Acme", "account") + "# Acme\n")
        with self.assertRaisesRegex(ValueError, "existing folder"):
            move_note(self.vault, "wiki/target-note", "customers/acme")
        self.assertEqual(move_note(self.vault, "wiki/target-note", "customers/acme.md")["to"], "customers/acme")

    def test_move_accepts_stem_and_wikilink_inputs_and_approvals(self):
        """§5.1. The note and each approve_stale entry may be a CID, stem or wikilink."""
        plan = move_note(
            self.vault,
            "flash-attention",
            "fa-tiling",
            approve_stale=["[[epistemic-trust-tiers]]"],
        )
        self.assertEqual(plan["blocked"], [])
        plan = move_note(
            self.vault,
            "[[flash-attention]]",
            "fa-tiling",
            apply=True,
            approve_stale=["epistemic-trust-tiers"],
        )
        self.assertEqual(plan["from"], "wiki/flash-attention")
        self.assertEqual(plan["to"], "wiki/fa-tiling")

    def test_ambiguous_names_and_unknown_approvals_are_refused(self):
        """§5.1, §10 C49. Two README hubs make `README` ambiguous; an approval naming no note is an error."""
        self._write("customers/acme/README.md", _fm("Acme", "moc") + "# Acme\n")
        self._write("projects/apollo/README.md", _fm("Apollo", "moc") + "# Apollo\n")
        before = self._snapshot()
        with self.assertRaises(AmbiguousNoteError) as ctx:
            move_note(self.vault, "README", "archive/", apply=True)
        self.assertEqual(ctx.exception.candidates, ["customers/acme/README", "projects/apollo/README"])
        with self.assertRaises(FileNotFoundError):
            move_note(self.vault, "wiki/sqlite", "sqlite-x", approve_stale=["No-Such-Note"])
        self.assertEqual(self._snapshot(), before)

    def test_wrong_case_name_resolves_to_the_real_note(self):
        """§10 C49. The source is always a listed CID, never the caller's spelling (APFS/NTFS would open either)."""
        plan = move_note(self.vault, "wiki/Flash-Attention", "fa-renamed")
        self.assertEqual(plan["from"], "wiki/flash-attention")
        # The real note's referrers are planned, so the human-reviewed guard still fires.
        self.assertTrue(any("epistemic-trust-tiers" in b for b in plan["blocked"]))

    def test_overlapping_nested_link_spans_do_not_duplicate_body_text(self):
        """§5.1. Nested link syntax [x]([[target-note]].md) in a referrer rewrites the wikilink once, cleanly."""
        self._write("wiki/target-note.md", _fm("Target") + "# Target\n")
        self._write("wiki/linker.md", _fm("Linker") + "Nested [x]([[target-note]].md) here.\n")
        plan = move_note(self.vault, "wiki/target-note", "renamed-note", apply=True)
        self.assertEqual(plan["notes"][0]["links_rewritten"], 1)
        body = (self.root / "wiki/linker.md").read_text("utf-8")
        self.assertIn("Nested [x]([[renamed-note]].md) here.", body)

    def test_link_nested_in_a_rewritten_link_goes_with_it(self):
        """§5.1. When both the outer path and the inner wikilink need rewriting, only the outer one is edited."""
        self._write("wiki/target-note.md", _fm("Target") + "Nested [x]([[target-note]].md) end.\n")
        plan = move_note(self.vault, "wiki/target-note", "customers/acme/renamed-note", apply=True)
        self.assertEqual(plan["moved_note"]["links_rewritten"], 1)
        body = (self.root / "customers/acme/renamed-note.md").read_text("utf-8")
        self.assertIn("Nested [x](../../wiki/[[target-note]].md) end.", body)

    def test_human_reviewed_moved_note_can_be_approved_by_its_destination(self):
        """§5.1. A moved note whose own links change is named in approve_stale by old or new path."""
        self._write("wiki/hr-target.md", _fm("HR") + "[sib](sqlite.md)\n")
        verify_note(self.vault, "wiki/hr-target", actor="human:tester", is_human_authorized=True)
        blocked = move_note(self.vault, "wiki/hr-target", "customers/acme/hr-target")["blocked"]
        self.assertTrue(any("hr-target.md is human-reviewed" in b for b in blocked))
        applied = move_note(
            self.vault, "wiki/hr-target", "customers/acme/hr-target", apply=True, approve_stale=["customers/acme/hr-target"]
        )
        self.assertEqual(applied["moved_note"]["tier_after"], "stale-verified")

    def test_destination_stem_collision_uses_the_shortest_qualified_wikilink(self):
        """§5.1. A bare stem that would open another note is lengthened until it opens the moved one."""
        self._write("wiki/architecture.md", _fm("Wiki Architecture") + "# Arch\n")
        self._write("wiki/design-notes.md", _fm("Design") + "# Design\n")
        self._write("wiki/reader.md", _fm("Reader") + "See [[design-notes]].\n")
        move_note(self.vault, "wiki/design-notes", "customers/acme/architecture", apply=True)
        self.assertIn("See [[acme/architecture]].", (self.root / "wiki/reader.md").read_text("utf-8"))
        self.assertNotIn("LINK_DEAD", {f.code for f in run_vault_lint(self.vault)})

    def test_dead_links_the_move_brings_to_life_are_reported_not_blocked(self):
        self._write("wiki/target-note.md", _fm("Target") + "# Target\n")
        self._write("wiki/hopeful.md", _fm("Hopeful") + "[[renamed-note]] and [md](renamed-note.md)\n")
        plan = move_note(self.vault, "wiki/target-note", "renamed-note")
        self.assertEqual(plan["blocked"], [])
        self.assertEqual(len(plan["newly_resolved"]), 2)

    def test_unparseable_notes_block_only_when_they_link_to_the_moved_note(self):
        """§5.1. A broken note elsewhere is a warning; one that links to the moved note is a blocker."""
        self._write("wiki/target-note.md", _fm("Target") + "# Target\n")
        self._write("wiki/broken.md", "---\ntype: concept\ntags: [a, b]\n---\nNo links at all.\n")
        (self.root / "wiki/garbled.md").write_bytes(b"---\ntype: concept\n---\n\xff\xfe not utf-8\n")
        plan = move_note(self.vault, "wiki/target-note", "renamed-note")
        self.assertEqual(plan["blocked"], [])
        self.assertEqual(sorted(w.split(" ")[0] for w in plan["warnings"]), ["wiki/broken.md", "wiki/garbled.md"])

        self._write("wiki/broken.md", "---\ntype: concept\ntags: [a, b]\n---\nSee [[target-note]].\n")
        plan = move_note(self.vault, "wiki/target-note", "renamed-note")
        self.assertTrue(any(b.startswith("wiki/broken.md cannot be parsed") for b in plan["blocked"]))

    def test_moved_note_link_that_climbs_out_of_the_vault_is_left_alone(self):
        self._write("wiki/target-note.md", _fm("Target") + "[out](../../outside.md)\n")
        plan = move_note(self.vault, "wiki/target-note", "customers/acme/target-note", apply=True)
        self.assertEqual(plan["moved_note"]["links_rewritten"], 0)
        self.assertIn("[out](../../outside.md)", (self.root / "customers/acme/target-note.md").read_text("utf-8"))

    def test_same_note_dot_anchor_survives_a_cross_folder_move(self):
        """§5.1. `[here](./#top)` jumps within the note; it must not be turned into a path to the old folder."""
        self._write("wiki/target-note.md", _fm("Target") + "# Top\n\n[here](./#top)\n")
        plan = move_note(self.vault, "wiki/target-note", "customers/acme/target-note", apply=True)
        self.assertEqual(plan["moved_note"]["links_rewritten"], 0)
        self.assertIn("[here](./#top)", (self.root / "customers/acme/target-note.md").read_text("utf-8"))

    def test_case_insensitive_storage_plans_against_the_listed_spelling(self):
        """§10 C49. On APFS/NTFS the storage opens any spelling; the plan must still use the real CID."""

        class CaseInsensitiveDisk(DiskNoteStorage):
            def _real(self, cid_or_path: str) -> str:
                wanted = cid_or_path.casefold().removesuffix(".md")
                return next((c for c in self.list_note_cids() if c.casefold() == wanted), cid_or_path)

            def note_exists(self, cid_or_path: str) -> bool:
                return super().note_exists(self._real(cid_or_path))

            def get_note(self, cid_or_path: str) -> Note | None:
                note = super().get_note(self._real(cid_or_path))
                if note is not None:  # named after the spelling asked for, as DiskNoteStorage does on APFS
                    note = Note(cid=cid_or_path, rel_path=cid_or_path + ".md", frontmatter=note.frontmatter, body=note.body, source_hash=note.source_hash)
                return note

        uc = MoveNoteUseCase(CaseInsensitiveDisk(self.vault), FileLedger(self.vault), vault=self.vault)
        plan = uc.plan("wiki/Flash-Attention", "fa-renamed")
        self.assertEqual(plan.source.cid, "wiki/flash-attention")
        self.assertTrue(any("epistemic-trust-tiers" in b for b in plan.blocked))

    def test_a_rewrite_that_would_not_read_back_blocks_the_plan(self):
        """§5.1. Every rewritten link is re-parsed; one that no longer names the moved note is a blocker."""
        self._write("wiki/target-note.md", _fm("Target") + "# Target\n")
        self._write("wiki/linker.md", _fm("Linker") + "See [bare](target-note.md).\n")
        # Simulate an encoder bug: the space is written unencoded, so the bare link would end early.
        with unittest.mock.patch("cadabby.ops._markdown_path_text", lambda path, angle: path):
            plan = move_note(self.vault, "wiki/target-note", "customers/acme/a b")
        self.assertTrue(any("would no longer open the same file" in b for b in plan["blocked"]), plan["blocked"])

    def test_moved_note_images_and_reference_definitions_follow_the_new_folder(self):
        """§5.1. Paths that are not graph edges still go stale when a note changes folder."""
        self._write("raw/attachments/fig.png", "png")
        self._write(
            "wiki/target-note.md",
            _fm("Target") + "![fig](../raw/attachments/fig.png) ![wide](<../raw/attachments/fig.png>)\n\n"
            "[paper]: ../raw/karpathy-llm-wiki-gist.md\n",
        )
        self._write("wiki/cites.md", _fm("Cites") + "See [the note][t].\n\n[t]: target-note.md\n")
        plan = move_note(self.vault, "wiki/target-note", "customers/acme/target-note", apply=True)
        self.assertEqual(plan["moved_note"]["links_rewritten"], 3)
        body = (self.root / "customers/acme/target-note.md").read_text("utf-8")
        self.assertIn("![fig](../../raw/attachments/fig.png)", body)
        self.assertIn("![wide](<../../raw/attachments/fig.png>)", body)
        self.assertIn("[paper]: ../../raw/karpathy-llm-wiki-gist.md", body)
        self.assertIn("[t]: ../customers/acme/target-note.md", (self.root / "wiki/cites.md").read_text("utf-8"))

    def test_rewritten_links_stay_edges_or_the_destination_is_refused(self):
        """§5.1. Embeds of a dotted stem gain `.md`; names links cannot carry are refused up front."""
        self._write("wiki/kv-cache.md", _fm("KV") + "## Summary\n\nText.\n")
        self._write("wiki/kv-moc.md", _fm("KV MOC", "moc") + "![[kv-cache#Summary]]\n")
        move_note(self.vault, "wiki/kv-cache", "kv-cache-v1.5", apply=True)
        self.assertIn("![[kv-cache-v1.5.md#Summary]]", (self.root / "wiki/kv-moc.md").read_text("utf-8"))
        self.assertNotIn("NOTE_ORPHAN", {f.code for f in run_vault_lint(self.vault) if "kv-" in f.rel_path})

        self._write("wiki/target-note.md", _fm("Target") + "# Target\n")
        for bad in ("wiki/c#-notes", "wiki/a|b", "wiki/x[1]", "projects/apollo/raw/target-note", "customers/ acme/x"):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "Cannot move"):
                move_note(self.vault, "wiki/target-note", bad)

    def test_percent_and_scheme_like_names_round_trip(self):
        self._write("wiki/target-note.md", _fm("Target") + "# Target\n")
        self._write("wiki/linker.md", _fm("Linker") + "[a](target-note.md) [b](<target-note.md>)\n")
        move_note(self.vault, "wiki/target-note", "%41literal", apply=True)
        move_note(self.vault, "wiki/%41literal", "re:invent", apply=True)
        body = (self.root / "wiki/linker.md").read_text("utf-8")
        self.assertIn("[a](./re:invent.md)", body)
        self.assertIn("[b](<./re:invent.md>)", body)
        self.assertNotIn("LINK_DEAD", {f.code for f in run_vault_lint(self.vault)})

    def test_source_edited_between_plan_and_apply_is_a_conflict(self):
        """§8, §10 C45. The final delete checks the source's token, so a late edit is never lost."""
        self._write("wiki/target-note.md", _fm("Target") + "# Target\n")
        self._write("wiki/a-ref.md", _fm("A") + "[[target-note]]\n")
        uc = MoveNoteUseCase(DiskNoteStorage(self.vault), FileLedger(self.vault), vault=self.vault)
        plan = uc.plan("wiki/target-note", "customers/acme/moved-note")
        before = self._snapshot()
        edited = before["wiki/target-note.md"] + "\nEdited after plan.\n"
        (self.root / "wiki/target-note.md").write_text(edited, "utf-8")
        with self.assertRaises(VaultConflictError):
            uc.apply(plan, actor="agent:test")
        self.assertEqual(self._snapshot(), {**before, "wiki/target-note.md": edited})
        self.assertFalse((self.root / "customers").exists())

    def test_case_only_rename_refuses_a_twin_written_after_the_plan(self):
        """§5.1. After retiring the old spelling, apply re-checks the destination before writing."""
        self._write("wiki/Case-Target.md", _fm("Case") + "# Case\n")
        uc = MoveNoteUseCase(DiskNoteStorage(self.vault), FileLedger(self.vault), vault=self.vault)
        plan = uc.plan("wiki/Case-Target", "wiki/case-target")
        before = self._snapshot()
        twin = _fm("Intruder") + "# Written meanwhile\n"
        self._write("wiki/case-target.md", twin)
        with self.assertRaisesRegex(FileExistsError, "already exists"):
            uc.apply(plan, actor="agent:test")
        self.assertEqual(self._snapshot(), {**before, "wiki/case-target.md": twin})

    def test_rollback_restores_exact_bytes_and_removes_new_folders(self):
        """§8, §10 C50. Comments and CRLF survive a rollback, and no folder the move created is left behind."""
        self._write("wiki/target-note.md", _fm("Target") + "# Target\n")
        crlf = "---\r\n# reviewed by legal\r\ntype: concept\r\ntitle: 'A ref'\r\ndescription: d\r\nstatus: active\r\n---\r\nSee [[target-note]].\r\n"
        (self.root / "wiki/a-ref.md").write_bytes(crlf.encode("utf-8"))
        self._write("wiki/z-ref.md", _fm("Z") + "[[target-note]]\n")
        uc = MoveNoteUseCase(DiskNoteStorage(self.vault), FileLedger(self.vault), vault=self.vault)
        plan = uc.plan("wiki/target-note", "research/papers/renamed-note")
        (self.root / "wiki/z-ref.md").write_text(_fm("Z") + "[[target-note]] edited\n", "utf-8")
        with self.assertRaises(VaultConflictError):
            uc.apply(plan, actor="agent:test")
        self.assertEqual((self.root / "wiki/a-ref.md").read_bytes(), crlf.encode("utf-8"))
        self.assertFalse((self.root / "research").exists())

    def test_pure_rename_copies_the_note_byte_for_byte(self):
        raw = "---\n# owner: legal, do not edit\ntype: concept\ntitle: 'Quoted'\ndescription: d\nstatus: active\n---\n# Body\n"
        self._write("wiki/target-note.md", raw)
        move_note(self.vault, "wiki/target-note", "customers/acme/", apply=True)
        self.assertEqual((self.root / "customers/acme/target-note.md").read_text("utf-8"), raw)

    def test_stale_plan_token_refuses_apply_and_writes_nothing(self):
        """§5.1, §10 C50. The dry run's token binds the apply to the plan the user saw."""
        self._target_and_linker()
        token = move_note(self.vault, "wiki/target-note", "renamed-note")["plan_token"]
        self.assertEqual(move_note(self.vault, "wiki/target-note", "renamed-note")["plan_token"], token)
        self._write("wiki/late.md", _fm("Late") + "[[target-note]]\n")
        before = self._snapshot()
        with self.assertRaises(VaultConflictError):
            move_note(self.vault, "wiki/target-note", "renamed-note", apply=True, plan_token=token)
        self.assertEqual(self._snapshot(), before)
        fresh = move_note(self.vault, "wiki/target-note", "renamed-note")["plan_token"]
        self.assertNotEqual(fresh, token)
        self.assertTrue(move_note(self.vault, "wiki/target-note", "renamed-note", apply=True, plan_token=fresh)["applied"])

    def test_folder_move_preserves_comoving_links_and_prunes_empty_dirs(self):
        """§2.6, §5.1, §10 C54: Moving a folder prefix preserves co-moving relative links, updates external links, and prunes empty dirs."""
        self._write(
            "customers/northwind/README.md",
            _fm("Northwind Hub", type_="moc") + "# Northwind\nSee [deal](deal.md) and [[deal]].\n",
        )
        self._write(
            "customers/northwind/deal.md",
            _fm("Northwind Deal")
            + "# Deal\nBack to [README](README.md) and source [gist](../../raw/karpathy-llm-wiki-gist.md).\n",
        )
        self._write(
            "wiki/overview.md",
            _fm("Overview") + "# Overview\nTrack [deal](../customers/northwind/deal.md) and [[customers/northwind/README]].\n",
        )
        # Human-verify README.md, which only links relatively to co-moving deal.md (no ../raw/ link that changes depth)
        verify_note(self.vault, "customers/northwind/README", actor="human:alice", is_human_authorized=True)

        # Moving from depth 2 (customers/northwind/*) to depth 3 (customers/enterprise/northwind/*)
        # README.md's body has [deal](deal.md) which is co-moving and unchanged! So README.md stays human-reviewed without approve_stale.
        res = move_note(
            self.vault,
            "customers/northwind/",
            "customers/enterprise/northwind/",
            apply=True,
            actor="agent:test",
        )
        self.assertTrue(res["applied"])
        self.assertEqual(len(res["moved_notes"]), 2)
        self.assertFalse((self.root / "customers/northwind").exists())
        self.assertTrue((self.root / "customers/enterprise/northwind/README.md").exists())
        self.assertTrue((self.root / "customers/enterprise/northwind/deal.md").exists())

        # Co-moving relative link [deal](deal.md) is byte-for-byte untouched, preserving human-reviewed trust
        readme_after = (self.root / "customers/enterprise/northwind/README.md").read_text("utf-8")
        self.assertIn("[deal](deal.md)", readme_after)
        with VaultCache(self.vault) as cache:
            cache.scan()
            row = cache.get_connection().execute(
                "SELECT trust_tier FROM notes WHERE cid = 'customers/enterprise/northwind/README';"
            ).fetchone()
            self.assertEqual(row["trust_tier"], "human-reviewed")

        # Outbound raw/ link in deal.md was rewritten for the deeper folder depth
        deal_after = (self.root / "customers/enterprise/northwind/deal.md").read_text("utf-8")
        self.assertIn("[README](README.md)", deal_after)
        self.assertIn("[gist](../../../raw/karpathy-llm-wiki-gist.md)", deal_after)

        # External referrer in wiki/overview.md was updated for both markdown and qualified wikilink
        overview_after = (self.root / "wiki/overview.md").read_text("utf-8")
        self.assertIn("[deal](../customers/enterprise/northwind/deal.md)", overview_after)
        self.assertIn("[[README]]", overview_after)
        self.assertEqual([f for f in run_vault_lint(self.vault) if f.severity == "error"], [])

    def test_folder_move_into_archive_retires_subtree_atomically(self):
        """§2.2, §2.6, §5.1, §10 C53, C54: Moving an active domain folder into archive/ retires the subtree and preserves co-moving human-reviewed notes."""
        self._write(
            "cs244b/README.md",
            _fm("CS244B Hub", type_="moc") + "# CS244B\n- [raft](lectures/raft.md)\n",
        )
        self._write(
            "cs244b/lectures/raft.md",
            _fm("Raft Lecture") + "# Raft\nPart of [CS244B](../README.md).\n",
        )
        verify_note(self.vault, "cs244b/README", actor="human:alice", is_human_authorized=True)
        verify_note(self.vault, "cs244b/lectures/raft", actor="human:alice", is_human_authorized=True)

        # Dry-run plan shows 2 subtree moves and no blocked notes because co-moving relative links do not change
        plan = move_note(self.vault, "cs244b", "archive/cs244b", apply=False)
        self.assertEqual(len(plan["moved_notes"]), 2)
        self.assertEqual(plan["blocked"], [])

        applied = move_note(
            self.vault,
            "cs244b",
            "archive/cs244b",
            apply=True,
            plan_token=plan["plan_token"],
            actor="agent:test",
        )
        self.assertTrue(applied["applied"])
        self.assertFalse((self.root / "cs244b").exists())
        self.assertTrue((self.root / "archive/cs244b/README.md").exists())
        self.assertTrue((self.root / "archive/cs244b/lectures/raft.md").exists())

        with VaultCache(self.vault) as cache:
            cache.scan()
            rows = cache.get_connection().execute(
                "SELECT cid, layer, trust_tier FROM notes WHERE layer = 'archive' ORDER BY cid;"
            ).fetchall()
            self.assertEqual(
                [(r["cid"], r["layer"], r["trust_tier"]) for r in rows],
                [
                    ("archive/cs244b/README", "archive", "human-reviewed"),
                    ("archive/cs244b/lectures/raft", "archive", "human-reviewed"),
                ],
            )
        self.assertEqual([f for f in run_vault_lint(self.vault) if f.severity == "error"], [])

    def test_folder_move_edge_cases_overlapping_shift_casefold_and_leftovers(self):
        """§5.1, §10 C54: Overlapping parent/child subtree shift, case-folded folder prefix, folder approve_stale, and leftover non-note warnings."""
        # 1. Overlapping parent/child subtree shift: moving customers/acme/sub/ -> customers/acme/
        # where both sub/deal.md and sub/sub/deal.md exist (item 1's destination equals item 0's source).
        self._write("customers/acme/sub/deal.md", _fm("Outer Deal") + "# Outer\n")
        self._write("customers/acme/sub/sub/deal.md", _fm("Inner Deal") + "# Inner\n")
        plan = move_note(self.vault, "customers/acme/sub/", "customers/acme/", apply=False)
        move_note(
            self.vault,
            "customers/acme/sub/",
            "customers/acme/",
            apply=True,
            plan_token=plan["plan_token"],
            actor="agent:test",
        )
        self.assertIn("# Outer", (self.root / "customers/acme/deal.md").read_text("utf-8"))
        self.assertIn("# Inner", (self.root / "customers/acme/sub/deal.md").read_text("utf-8"))
        self.assertFalse((self.root / "customers/acme/sub/sub").exists())

        # 2. Case-insensitive source folder prefix + folder prefix in approve_stale + leftover non-note warning
        self._write(
            "cs244b/AGENTS.md",
            "---\ndomain: cs244b\ndescription: Distributed systems.\n---\n# CS244B\n",
        )
        self._write(
            "cs244b/notes.md",
            _fm("Notes") + "# Notes\nSee [gist](../raw/karpathy-llm-wiki-gist.md).\n",
        )
        verify_note(self.vault, "cs244b/notes", actor="human:alice", is_human_authorized=True)

        # Case-folded source folder prefix ("CS244B/") resolves to "cs244b/" and warns about cs244b/AGENTS.md
        blocked_plan = move_note(self.vault, "CS244B/", "archive/cs244b/", apply=False)
        self.assertEqual(blocked_plan["from"], "cs244b/")
        self.assertEqual(len(blocked_plan["blocked"]), 1)
        self.assertTrue(any("cs244b/AGENTS.md" in w for w in blocked_plan["warnings"]))

        # Approving via the folder prefix unblocks all moved notes under that prefix
        ok_plan = move_note(
            self.vault,
            "CS244B/",
            "archive/cs244b/",
            approve_stale=["cs244b/"],
            apply=False,
        )
        self.assertEqual(ok_plan["blocked"], [])
        move_note(
            self.vault,
            "CS244B/",
            "archive/cs244b/",
            approve_stale=["cs244b/"],
            apply=True,
            plan_token=ok_plan["plan_token"],
            actor="agent:test",
        )
        self.assertTrue((self.root / "archive/cs244b/notes.md").exists())
        self.assertTrue((self.root / "cs244b/AGENTS.md").exists())


class TestMoveNoteInMemory(unittest.TestCase):
    """Hexagonal parity: the use case runs with no vault on disk."""

    def test_in_memory_move_matches_disk_semantics(self):
        storage = InMemoryNoteStorage(
            [
                Note.from_raw("wiki/target-note", "wiki/target-note.md", _fm("Target") + "# Target\n"),
                Note.from_raw("wiki/linker", "wiki/linker.md", _fm("Linker") + LINKER_BODY),
            ]
        )
        ledger = InMemoryLedger()
        uc = MoveNoteUseCase(storage, ledger)
        uc.apply(uc.plan("wiki/target-note", "customers/acme/renamed-note"), actor="agent:test")
        self.assertFalse(storage.note_exists("wiki/target-note"))
        self.assertTrue(storage.note_exists("customers/acme/renamed-note"))
        linker = storage.get_note("wiki/linker")
        assert linker is not None
        self.assertEqual(linker.body.strip(), LINKER_AFTER.strip())
        self.assertEqual(len(ledger.entries), 1)

    @staticmethod
    def _store() -> InMemoryNoteStorage:
        return InMemoryNoteStorage(
            [
                Note.from_raw("wiki/Target", "wiki/Target.md", _fm("Target") + "# Target\n"),
                Note.from_raw("wiki/a-ref", "wiki/a-ref.md", _fm("A") + "[[Target]]\n"),
                Note.from_raw("wiki/z-ref", "wiki/z-ref.md", _fm("Z") + "[[Target]]\n"),
            ]
        )

    @staticmethod
    def _contents(storage: InMemoryNoteStorage) -> dict[str, str]:
        return {cid: storage.read_raw(cid) or "" for cid in storage.list_note_cids()}

    def test_in_memory_case_only_rename_and_late_twin(self):
        """§5.1. A case-only rename succeeds; a twin written between plan and apply is refused and the source restored."""
        storage, ledger = self._store(), InMemoryLedger()
        uc = MoveNoteUseCase(storage, ledger)
        uc.apply(uc.plan("wiki/Target", "wiki/target"), actor="agent:test")
        self.assertEqual(storage.list_note_cids(), ["wiki/a-ref", "wiki/target", "wiki/z-ref"])

        storage = self._store()
        uc = MoveNoteUseCase(storage, InMemoryLedger())
        plan = uc.plan("wiki/Target", "wiki/target")
        storage.save_note(Note.from_raw("wiki/target", "wiki/target.md", _fm("Twin") + "# Twin\n"))
        before = self._contents(storage)
        with self.assertRaisesRegex(FileExistsError, "already exists"):
            uc.apply(plan, actor="agent:test")
        self.assertEqual(self._contents(storage), before)

    def test_in_memory_conflict_restores_every_note_and_logs_nothing(self):
        """§10 C45. Earlier rewrites and the destination are undone; the concurrent edit survives; no ledger line."""
        storage, ledger = self._store(), InMemoryLedger()
        uc = MoveNoteUseCase(storage, ledger)
        plan = uc.plan("wiki/Target", "wiki/moved")
        concurrent = _fm("Z") + "[[Target]] concurrent\n"
        storage.save_note(Note.from_raw("wiki/z-ref", "wiki/z-ref.md", concurrent))
        before = self._contents(storage)
        with self.assertRaises(VaultConflictError):
            uc.apply(plan, actor="agent:test")
        self.assertEqual(self._contents(storage), before)
        self.assertEqual(ledger.entries, [])

    def test_failed_rollback_is_reported_with_every_file_left_behind(self):
        """§5.1. If an undo step itself fails, the error says so instead of claiming a clean rollback."""

        class UndoFails(InMemoryNoteStorage):
            def delete_note(self, cid_or_path: str, expected_hash: str | None = None, *, prune_empty_dirs: bool = False) -> None:
                if prune_empty_dirs:
                    raise OSError(f"cannot remove {cid_or_path}")
                super().delete_note(cid_or_path, expected_hash, prune_empty_dirs=prune_empty_dirs)

        storage = UndoFails(list(self._store()._notes.values()))
        uc = MoveNoteUseCase(storage, InMemoryLedger())
        plan = uc.plan("wiki/Target", "wiki/moved")
        storage.save_note(Note.from_raw("wiki/z-ref", "wiki/z-ref.md", _fm("Z") + "edited\n"))
        with self.assertRaisesRegex(RuntimeError, "rollback incomplete.*cannot remove wiki/moved"):
            uc.apply(plan, actor="agent:test")

    def test_in_memory_destination_rules_match_disk(self):
        """§5.1. Traversal and paths outside the vault fail with the same messages as on disk."""
        uc = MoveNoteUseCase(self._store(), InMemoryLedger())
        with self.assertRaisesRegex(ValueError, "Path traversal"):
            uc.plan("wiki/Target", "wiki/../x")
        with self.assertRaisesRegex(ValueError, "outside vault root"):
            InMemoryNoteStorage().save_note(Note(cid="../outside", rel_path="../outside.md"))


if __name__ == "__main__":
    unittest.main()

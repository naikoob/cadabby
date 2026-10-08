"""Unit tests for six-gate epistemic linting."""

from __future__ import annotations

import re
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from cadabby.lint import run_vault_lint
from cadabby.vault import Vault
from tests.helpers import copy_demo_vault


class TestLint(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.vault_root = copy_demo_vault(Path(self.tmp_dir.name) / "demo-vault")
        self.vault = Vault(self.vault_root)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_clean_demo_vault_has_zero_errors(self):
        findings = run_vault_lint(self.vault)
        errors = [f for f in findings if f.severity == "error"]
        self.assertEqual(len(errors), 0, f"Clean demo vault had unexpected errors: {errors}")

    def test_gate1_schema_integrity_errors(self):
        # 1. Unparseable frontmatter
        bad_note = self.vault.wiki_dir / "Bad-Yaml.md"
        bad_note.write_text("---\ntags: [flow, style]\n---\n# Bad\n", "utf-8")

        # 2. Missing required field (e.g. description)
        missing_field = self.vault.wiki_dir / "Missing-Field.md"
        missing_field.write_text(
            "---\ntype: concept\ntitle: Missing Field\nstatus: active\n---\n# Content\n",
            "utf-8",
        )

        findings = run_vault_lint(self.vault)
        codes = {f.code for f in findings}
        self.assertIn("FRONTMATTER_UNPARSEABLE", codes)
        self.assertIn("FIELD_MISSING", codes)

    def test_gate1_tag_grammar(self):
        """Whitespace in a tag is an error; non-canonical case is only a warning.

        Whitespace corrupts the space-joined `--tag` facet (§4.2) and the writer
        cannot repair it; case is repaired by the next agent write (§3.2).
        """
        (self.vault.wiki_dir / "Spaced-Tag.md").write_text(
            "---\ntype: concept\ntitle: Spaced Tag\ndescription: Has a bad tag\n"
            "status: active\ntags:\n  - machine learning\n---\n# Spaced\n\n[[SQLite]]\n",
            "utf-8",
        )
        (self.vault.wiki_dir / "Cased-Tag.md").write_text(
            "---\ntype: concept\ntitle: Cased Tag\ndescription: Has a shouty tag\n"
            "status: active\ntags:\n  - Machine-Learning\n---\n# Cased\n\n[[SQLite]]\n",
            "utf-8",
        )

        findings = run_vault_lint(self.vault)
        tag_findings = {f.rel_path: f for f in findings if f.code == "TAG_MALFORMED"}

        self.assertIn("wiki/Spaced-Tag.md", tag_findings)
        self.assertEqual(tag_findings["wiki/Spaced-Tag.md"].severity, "error")

        self.assertIn("wiki/Cased-Tag.md", tag_findings)
        self.assertEqual(tag_findings["wiki/Cased-Tag.md"].severity, "warning")

    def test_gate2_layout_consistency_error(self):
        # 1. Nesting inside wiki/ triggers WIKI_NESTING_DISALLOWED
        nested_dir = self.vault.wiki_dir / "nested"
        nested_dir.mkdir(parents=True, exist_ok=True)
        nested_note = nested_dir / "Nested.md"
        nested_note.write_text(
            "---\ntype: concept\ntitle: Nested Note\ndescription: Test\nstatus: active\n---\n# Note\n",
            "utf-8",
        )

        # 2. Nesting inside a custom domain is allowed
        cust_dir = self.vault_root / "customers" / "subdivision"
        cust_dir.mkdir(parents=True, exist_ok=True)
        cust_note = cust_dir / "Sub-Customer.md"
        cust_note.write_text(
            "---\ntitle: Sub Customer\ntype: customer\ndescription: Nested custom domain note\nstatus: active\n---\n# Note\n",
            "utf-8",
        )

        findings = run_vault_lint(self.vault)
        wiki_nesting_findings = [f for f in findings if f.code == "WIKI_NESTING_DISALLOWED"]
        self.assertEqual(len(wiki_nesting_findings), 1)
        self.assertEqual(wiki_nesting_findings[0].rel_path, "wiki/nested/Nested.md")

        # Ensure custom domain nesting didn't trigger any layout error
        cust_errors = [f for f in findings if "customers" in f.rel_path and f.severity == "error"]
        self.assertEqual(len(cust_errors), 0)

    def test_gate2_nested_folder_named_wiki_is_still_nested(self):
        """Gate 2 compares the whole parent path; the folder's name alone let this through."""
        nested = self.vault.wiki_dir / "x" / "wiki"
        nested.mkdir(parents=True)
        (nested / "N.md").write_text(
            "---\ntype: concept\ntitle: N\ndescription: Test\nstatus: active\n---\n# N\n",
            "utf-8",
        )
        findings = [f for f in run_vault_lint(self.vault) if f.code == "WIKI_NESTING_DISALLOWED"]
        self.assertEqual([f.rel_path for f in findings], ["wiki/x/wiki/N.md"])
        self.assertIn("'wiki/x/wiki/'", findings[0].message)

    def test_lint_parses_only_unparseable_notes(self):
        """Lint reads the scan's rows; it re-parses only notes whose frontmatter failed.

        Gates 1, 2 and 6 used to re-read and re-parse every note straight after
        the scan had done exactly that. The broken note is re-parsed once to
        recover the parser's line number, and nothing else is.
        """
        import cadabby.lint as lint_module

        (self.vault.wiki_dir / "Bad-Yaml.md").write_text("---\ntags: [flow, style]\n---\n# Bad\n", "utf-8")
        real_parse = lint_module.parse_frontmatter
        calls: list[str] = []

        def counting_parse(content: str):
            calls.append(content)
            return real_parse(content)

        from cadabby.cache import VaultCache

        with VaultCache(self.vault) as cache:
            cache.scan()
            with unittest.mock.patch.object(lint_module, "parse_frontmatter", counting_parse):
                findings = run_vault_lint(self.vault, cache)

        self.assertEqual(len(calls), 1)
        unparseable = [f for f in findings if f.code == "FRONTMATTER_UNPARSEABLE"]
        self.assertEqual([f.rel_path for f in unparseable], ["wiki/Bad-Yaml.md"])
        self.assertEqual(unparseable[0].line, 2)

    def test_gate3_dead_link_error(self):
        # Add a dead link to a note
        note_file = self.vault.wiki_dir / "Epistemic-Trust-Tiers.md"
        content = note_file.read_text("utf-8") + "\n\nLink to [[Non-Existent-Target]]."
        note_file.write_text(content, "utf-8")

        findings = run_vault_lint(self.vault)
        codes = {f.code for f in findings}
        self.assertIn("LINK_DEAD", codes)

    def test_gate4_missing_source_error(self):
        # Add a note citing a non-existent raw file
        missing_src = self.vault.wiki_dir / "Missing-Source.md"
        missing_src.write_text(
            "---\ntype: concept\ntitle: Missing Source\ndescription: Test\nstatus: active\nsources:\n  - raw/ghost.pdf\n---\n# Note\n",
            "utf-8",
        )

        findings = run_vault_lint(self.vault)
        codes = {f.code for f in findings}
        self.assertIn("SOURCE_MISSING", codes)

    def test_gate6_verification_integrity(self):
        # Add a note with malformed actor and unbound verification
        bad_ver = self.vault.wiki_dir / "Bad-Verification.md"
        bad_ver.write_text(
            """---
type: concept
title: Bad Verification
description: Test
status: active
verified:
  - by: invalid-actor-without-prefix
    at: '2026-10-03T12:00:00Z'
---
# Content
""",
            "utf-8",
        )

        findings = run_vault_lint(self.vault)
        codes = {f.code for f in findings}
        self.assertIn("ACTOR_MALFORMED", codes)
        self.assertIn("VERIFICATION_UNBOUND", codes)

    def test_gate1_invalid_timestamps_in_both_blocks(self):
        """`generated.at` and `verified[].at` are separate branches (§6.3 gate 1).

        Asserting only one leaves the other free to be unreachable, which is
        the failure mode an unasserted emission site actually has.
        """
        (self.vault.wiki_dir / "Bad-Generated-At.md").write_text(
            "---\ntype: concept\ntitle: Bad Generated\ndescription: Test\nstatus: active\n"
            "generated:\n  at: 'last Tuesday'\n  by: agent:test\n---\n# Note\n\n[[SQLite]]\n",
            "utf-8",
        )
        (self.vault.wiki_dir / "Bad-Verified-At.md").write_text(
            "---\ntype: concept\ntitle: Bad Verified\ndescription: Test\nstatus: active\n"
            "verified:\n  - by: agent:test\n    at: '03/10/2026'\n    of: 'sha256:0000'\n"
            "---\n# Note\n\n[[SQLite]]\n",
            "utf-8",
        )

        findings = run_vault_lint(self.vault)
        offenders = {f.rel_path for f in findings if f.code == "TIMESTAMP_INVALID"}
        self.assertEqual(offenders, {"wiki/Bad-Generated-At.md", "wiki/Bad-Verified-At.md"})
        for f in findings:
            if f.code == "TIMESTAMP_INVALID":
                self.assertEqual(f.severity, "error")

    def test_gate3_anchor_missing_only_when_the_target_resolves(self):
        """A dead target is LINK_DEAD; ANCHOR_MISSING is for a live one (§6.3).

        The gates share a branch, so a fixture that gets the target wrong
        exercises the wrong arm and still passes on the code alone.
        """
        note = self.vault.wiki_dir / "Epistemic-Trust-Tiers.md"
        note.write_text(
            note.read_text("utf-8") + "\n\nSee [[SQLite#No-Such-Heading]].\n",
            "utf-8",
        )

        findings = run_vault_lint(self.vault)
        anchors = [f for f in findings if f.code == "ANCHOR_MISSING"]
        self.assertEqual(len(anchors), 1)
        self.assertEqual(anchors[0].rel_path, "wiki/Epistemic-Trust-Tiers.md")
        self.assertEqual(anchors[0].severity, "warning")
        self.assertNotIn("LINK_DEAD", {f.code for f in findings})

    def test_gate3_anchor_that_exists_is_silent(self):
        """Pins the negative arm: otherwise 'always flag' passes the test above."""
        target = self.vault.wiki_dir / "SQLite.md"
        target.write_text(target.read_text("utf-8") + "\n\n## Storage Engine\n\nDetail.\n", "utf-8")
        note = self.vault.wiki_dir / "Epistemic-Trust-Tiers.md"
        note.write_text(note.read_text("utf-8") + "\n\nSee [[SQLite#Storage Engine]].\n", "utf-8")

        findings = run_vault_lint(self.vault)
        self.assertEqual([f for f in findings if f.code == "ANCHOR_MISSING"], [])

    def test_gate6_stale_verification_is_debt_not_an_error(self):
        """A drifted attestation is warning-severity: it is work, not corruption.

        Erroring would make `lint` fail on every honest edit-before-reverify,
        which is the normal state of a vault being written to.
        """
        (self.vault.wiki_dir / "Stale-Verified.md").write_text(
            "---\ntype: concept\ntitle: Stale Verified\ndescription: Test\nstatus: active\n"
            "verified:\n  - by: human:owner\n    at: '2026-01-01T00:00:00Z'\n"
            "    of: 'sha256:deadbeef'\n---\n# Note\n\nBody has moved on. [[SQLite]]\n",
            "utf-8",
        )

        findings = run_vault_lint(self.vault)
        stale = [f for f in findings if f.code == "VERIFICATION_STALE"]
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0].rel_path, "wiki/Stale-Verified.md")
        self.assertEqual(stale[0].severity, "warning")
        self.assertNotIn("VERIFICATION_UNBOUND", {f.code for f in findings})

    def test_gate6_superseded_history_is_silent_when_reverified(self):
        """§6.3 gate 6. Append-only `verified:` keeps older entries for `audit`.

        Once a note is re-verified at its current body_hash with equal or higher
        authority, the superseded entry is history rather than debt and must not
        warn; a prior `human:*` entry followed only by an `agent:*` entry still
        warns so an agent cannot silence an unpaid human re-review.
        """
        from cadabby.okf import compute_body_hash

        body = "# Note\n\nUpdated prose. [[SQLite]]\n"
        live_hash = compute_body_hash(body)

        # 1. Superseded agent entry + current human entry -> silent.
        (self.vault.wiki_dir / "Reverified-Human.md").write_text(
            "---\ntype: concept\ntitle: Reverified Human\ndescription: Test\nstatus: active\n"
            "verified:\n"
            "  - by: agent:earlier\n    at: '2026-01-01T00:00:00Z'\n    of: 'sha256:deadbeef'\n"
            f"  - by: human:owner\n    at: '2026-01-02T00:00:00Z'\n    of: '{live_hash}'\n"
            f"---\n{body}",
            "utf-8",
        )
        # 2. Superseded agent entry + current agent entry -> silent.
        (self.vault.wiki_dir / "Reverified-Agent.md").write_text(
            "---\ntype: concept\ntitle: Reverified Agent\ndescription: Test\nstatus: active\n"
            "verified:\n"
            "  - by: agent:earlier\n    at: '2026-01-01T00:00:00Z'\n    of: 'sha256:deadbeef'\n"
            f"  - by: agent:later\n    at: '2026-01-02T00:00:00Z'\n    of: '{live_hash}'\n"
            f"---\n{body}",
            "utf-8",
        )
        # 3. Stale human entry + current agent entry -> still warns on the human entry.
        (self.vault.wiki_dir / "Human-Superseded-By-Agent.md").write_text(
            "---\ntype: concept\ntitle: Human Superseded By Agent\ndescription: Test\nstatus: active\n"
            "verified:\n"
            "  - by: human:owner\n    at: '2026-01-01T00:00:00Z'\n    of: 'sha256:deadbeef'\n"
            f"  - by: agent:later\n    at: '2026-01-02T00:00:00Z'\n    of: '{live_hash}'\n"
            f"---\n{body}",
            "utf-8",
        )

        stale = [f for f in run_vault_lint(self.vault) if f.code == "VERIFICATION_STALE"]
        self.assertEqual([f.rel_path for f in stale], ["wiki/Human-Superseded-By-Agent.md"])
        self.assertIn("human:owner", stale[0].message)

    def test_gate5_orphan_detection_single_query(self):
        # Scaffold an orphan note with no inbound and no outbound links
        orphan = self.vault.wiki_dir / "Lonely-Orphan.md"
        orphan.write_text(
            """---
type: concept
title: Lonely Orphan
description: An orphan note with no connections
status: active
---
# Lonely Orphan

There are no links here.
""",
            "utf-8",
        )

        findings = run_vault_lint(self.vault)
        orphan_findings = [f for f in findings if f.code == "NOTE_ORPHAN"]
        self.assertEqual(len(orphan_findings), 1)
        self.assertEqual(orphan_findings[0].rel_path, "wiki/Lonely-Orphan.md")
        self.assertEqual(orphan_findings[0].severity, "warning")

    def test_gate3_anchor_check_strips_code_spans_and_normalizes_hyphens(self):
        target = self.vault.wiki_dir / "Target-Note.md"
        target.write_text(
            "---\ntype: concept\ntitle: Target Note\ndescription: T\nstatus: active\n---\n"
            "# Target Note\n\n"
            "## B-Tree Indexes\n\n"
            "```python\n"
            "# Fake Heading Inside Code\n"
            "```\n"
            "[[SQLite]]\n",
            "utf-8",
        )
        caller = self.vault.wiki_dir / "Caller-Note.md"
        caller.write_text(
            "---\ntype: concept\ntitle: Caller Note\ndescription: C\nstatus: active\n---\n"
            "# Caller Note\n\n"
            "Valid hyphenated anchor: [[Target-Note#B-Tree-Indexes]].\n"
            "Invalid code comment anchor: [[Target-Note#Fake-Heading-Inside-Code]].\n",
            "utf-8",
        )
        findings = run_vault_lint(self.vault)
        anchor_warnings = [f for f in findings if f.code == "ANCHOR_MISSING" and f.rel_path == "wiki/Caller-Note.md"]
        self.assertEqual(len(anchor_warnings), 1)
        self.assertIn("Fake-Heading-Inside-Code", anchor_warnings[0].message)

    def test_gate4_rejects_directories_and_non_raw_paths(self):
        bad_src = self.vault.wiki_dir / "Bad-Source.md"
        bad_src.write_text(
            "---\ntype: concept\ntitle: Bad Source\ndescription: S\nstatus: active\n"
            "sources:\n"
            "  - raw\n"
            "  - AGENTS.md\n"
            "  - raw/../AGENTS.md\n"
            "---\n# Bad Source\n\n[[SQLite]]\n",
            "utf-8",
        )
        findings = run_vault_lint(self.vault)
        missing = [f for f in findings if f.code == "SOURCE_MISSING" and f.rel_path == "wiki/Bad-Source.md"]
        self.assertEqual(len(missing), 3)

    def _write_domain_note(self, rel: str, body: str) -> None:
        path = self.vault_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\ntype: concept\ntitle: Raw Linker\ndescription: S\nstatus: active\n---\n# Raw Linker\n\n" + body + "\n",
            "utf-8",
        )

    def test_raw_link_wrong_depth_is_reported_with_suggestion(self):
        (self.vault_root / "raw").mkdir(exist_ok=True)
        (self.vault_root / "raw" / "a.md").write_text("evidence\n", "utf-8")
        self._write_domain_note("customers/acme/N.md", "Per [the call](../../../raw/a.md).")
        findings = [f for f in run_vault_lint(self.vault) if f.code == "RAW_LINK_BROKEN"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "error")
        self.assertEqual(findings[0].rel_path, "customers/acme/N.md")
        self.assertIn("did you mean '../../raw/a.md'", findings[0].message)

    def test_correct_raw_links_code_spans_and_urls_are_ignored(self):
        (self.vault_root / "raw").mkdir(exist_ok=True)
        (self.vault_root / "raw" / "a.md").write_text("evidence\n", "utf-8")
        self._write_domain_note(
            "customers/acme/N.md",
            "Per [the call](../../raw/a.md). Example: `[x](../raw/ghost.md)`. "
            "See [upstream](https://example.com/raw/ghost.md).",
        )
        findings = [f for f in run_vault_lint(self.vault) if f.code == "RAW_LINK_BROKEN"]
        self.assertEqual(findings, [])

    def test_gate5_excludes_unparseable_notes_from_orphans(self):
        broken = self.vault.wiki_dir / "Broken-Unparseable.md"
        broken.write_text("---\ntags: [bad, flow]\n---\n# Broken\n", "utf-8")
        findings = run_vault_lint(self.vault)
        for_broken = [f.code for f in findings if f.rel_path == "wiki/Broken-Unparseable.md"]
        self.assertEqual(for_broken, ["FRONTMATTER_UNPARSEABLE"])


class TestLintTaxonomyIsClosed(unittest.TestCase):
    """§10 C4. One fixture seeding every code, and three lists held together.

    The per-gate tests above each assert the codes they care about, which
    leaves the taxonomy itself unguarded: a code can be added to lint.py and
    never documented, documented and never emitted, or emitted and never
    exercised, and no single test notices. These pin the set rather than its
    members, so adding a gate forces all three lists to move together.
    """

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.vault_root = copy_demo_vault(Path(self.tmp_dir.name) / "demo-vault")
        self.vault = Vault(self.vault_root)
        self._seed_one_of_everything()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _write(self, rel, text):
        path = self.vault_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8")

    def _fm(self, title, *extra, type_="concept", status="active", body="Body."):
        lines = [
            "---",
            f"type: {type_}",
            f"title: {title}",
            "description: Fixture note",
            f"status: {status}",
            *extra,
            "---",
            f"# {title}",
            "",
            body,
        ]
        return "\n".join(lines) + "\n"

    def _seed_one_of_everything(self):
        # Gate 1 -- schema integrity.
        self._write("wiki/F-Unparseable.md", "---\ntags: [inline, list]\n---\n# Bad\n")
        self._write(
            "wiki/F-Field-Missing.md",
            "---\ntype: concept\ntitle: No Description\nstatus: active\n---\n# Note\n\n[[SQLite]]\n",
        )
        self._write("wiki/F-Enum-Invalid.md", self._fm("Enum Invalid", status="nonexistent", body="[[SQLite]]"))
        self._write(
            "wiki/F-Timestamp-Invalid.md",
            self._fm("Timestamp Invalid", "generated:", "  at: 'last Tuesday'", "  by: agent:test", body="[[SQLite]]"),
        )
        # TAG_MALFORMED is the one code with two severities, so it needs two
        # notes: whitespace is unrepairable and errors, case is repaired on
        # the next agent write and only warns (§6.3 gate 1).
        self._write(
            "wiki/F-Tag-Malformed.md",
            self._fm("Tag Malformed", "tags:", "  - machine learning", body="[[SQLite]]"),
        )
        self._write(
            "wiki/F-Tag-Cased.md",
            self._fm("Tag Cased", "tags:", "  - Machine-Learning", body="[[SQLite]]"),
        )
        # A domain manifest whose contract cannot be honored (flow style).
        self._write("broken/AGENTS.md", "---\nallowed_types: [a, b]\n---\nDirectives.\n")
        # Gate 2 -- flat wiki.
        self._write("wiki/nested/F-Nested.md", self._fm("Nested", body="[[SQLite]]"))
        # Gate 3 -- links. Dead target vs live target with a bad anchor.
        self._write("wiki/F-Link-Dead.md", self._fm("Link Dead", body="[[Does-Not-Exist]]"))
        self._write("wiki/F-Anchor-Missing.md", self._fm("Anchor Missing", body="[[SQLite#No-Such-Heading]]"))
        # Gate 4 -- provenance.
        self._write(
            "wiki/F-Source-Missing.md",
            self._fm("Source Missing", "sources:", "  - raw/ghost.pdf", body="[[SQLite]]"),
        )
        self._write(
            "wiki/F-Raw-Link-Broken.md",
            self._fm("Raw Link Broken", body="[[SQLite]] [src](../../raw/ghost.md)"),
        )
        # Gate 5 -- connectivity. No links in either direction.
        self._write("wiki/F-Orphan.md", self._fm("Orphan", body="Nothing links here and it links nowhere."))
        # Gate 6 -- verification.
        self._write(
            "wiki/F-Actor-Malformed.md",
            self._fm(
                "Actor Malformed",
                "verified:",
                "  - by: no-prefix-actor",
                "    at: '2026-01-01T00:00:00Z'",
                "    of: 'sha256:0000'",
                body="[[SQLite]]",
            ),
        )
        self._write(
            "wiki/F-Verification-Unbound.md",
            self._fm(
                "Verification Unbound",
                "verified:",
                "  - by: agent:test",
                "    at: '2026-01-01T00:00:00Z'",
                body="[[SQLite]]",
            ),
        )
        self._write(
            "wiki/F-Verification-Stale.md",
            self._fm(
                "Verification Stale",
                "verified:",
                "  - by: human:owner",
                "    at: '2026-01-01T00:00:00Z'",
                "    of: 'sha256:deadbeef'",
                body="[[SQLite]]",
            ),
        )

    @staticmethod
    def _codes_emitted_by_lint():
        src = Path(__file__).resolve().parent.parent / "src" / "cadabby" / "lint.py"
        return set(re.findall(r'code="([A-Z_]+)"', src.read_text("utf-8")))

    @staticmethod
    def _codes_named_by_spec():
        """Codes backticked inside §6.3, bounded at the next top-level heading.

        Bounding matters: §8 names `VAULT_CONFLICT` and `O_APPEND`, which are
        error conditions rather than lint codes, and a greedy read of the
        section swallows them.
        """
        spec = (Path(__file__).resolve().parent.parent / "SPECIFICATION.md").read_text("utf-8")
        body = spec.split("### 6.3.", 1)[1].split("\n## ", 1)[0]
        return set(re.findall(r"`([A-Z][A-Z_]{3,})`", body))

    def test_the_fixture_seeds_one_of_every_code(self):
        produced = {f.code for f in run_vault_lint(self.vault)}
        self.assertEqual(
            produced,
            self._codes_emitted_by_lint(),
            "fixture and lint.py disagree; a code is unreachable or unseeded",
        )

    def test_the_spec_names_exactly_the_codes_lint_emits(self):
        self.assertEqual(self._codes_named_by_spec(), self._codes_emitted_by_lint())

    def test_every_code_carries_a_severity_the_spec_allows(self):
        for finding in run_vault_lint(self.vault):
            with self.subTest(code=finding.code):
                self.assertIn(finding.severity, ("error", "warning"))

    def test_only_connectivity_and_debt_are_warnings(self):
        """Severity is the difference between 'broken' and 'owed' (§6.3).

        Promoting a warning to an error makes `lint` fail on vaults that are
        merely incomplete, which is every vault mid-ingestion; demoting an
        error hides corruption. The split is a contract, so it is pinned.
        """
        by_severity = {}
        for finding in run_vault_lint(self.vault):
            by_severity.setdefault(finding.severity, set()).add(finding.code)

        self.assertEqual(
            by_severity["warning"],
            {"NOTE_ORPHAN", "VERIFICATION_STALE", "ANCHOR_MISSING", "TAG_MALFORMED"},
        )
        self.assertEqual(
            by_severity["error"] & by_severity["warning"],
            {"TAG_MALFORMED"},
            "TAG_MALFORMED is the only code that is both, and only for the case arm",
        )


class TestMarkdownNoteLinks(unittest.TestCase):
    """§4.4, §6.3, §10 C29. Relative Markdown links to notes are graph edges like wikilinks."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.vault_root = Path(self.tmp_dir.name) / "vault"
        for sub in ("wiki", "raw"):
            (self.vault_root / sub).mkdir(parents=True)
        (self.vault_root / "STYLE.md").write_text("# Style\n", "utf-8")
        self.vault = Vault(self.vault_root)
        self._note("wiki/Architecture.md", "Architecture", "## B-Tree Indexes (v2)\n\nDetail.")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _note(self, rel, title, body):
        path = self.vault_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\ntype: concept\ntitle: {title}\ndescription: Fixture\nstatus: active\n---\n# {title}\n\n{body}\n",
            "utf-8",
        )

    def _codes_for(self, rel):
        return [(f.code, f.message) for f in run_vault_lint(self.vault) if f.rel_path == rel]

    def test_qualified_link_never_falls_back_to_stem(self):
        """§4.4, C9: a target with a path segment resolves by exact CID or suffix only."""
        self._note("projects/apollo/Architecture.md", "Apollo Architecture", "Design.")
        self._note(
            "projects/apollo/Plan.md",
            "Plan",
            "[[apollo/Architecture]] [[apollo/Architcture]] [[customers/acmee/Architecture]] [[Architecture]]",
        )
        dead = sorted(m for c, m in self._codes_for("projects/apollo/Plan.md") if c == "LINK_DEAD")
        self.assertEqual(len(dead), 2, dead)
        self.assertTrue(any("apollo/Architcture" in m for m in dead))
        self.assertTrue(any("customers/acmee/Architecture" in m for m in dead))

    def test_gate2_reported_alongside_unparseable_frontmatter(self):
        """§6.3, C11: the layout gate reads only the path, so a broken header cannot hide it."""
        path = self.vault_root / "wiki" / "sub" / "Broken.md"
        path.parent.mkdir(parents=True)
        path.write_text("---\ntags: [flow, style]\n---\n# Broken\n", "utf-8")
        codes = {c for c, _ in self._codes_for("wiki/sub/Broken.md")}
        self.assertEqual(codes, {"FRONTMATTER_UNPARSEABLE", "WIKI_NESTING_DISALLOWED"})

    def test_status_broken_links_matches_lint(self):
        """§4.5, C34: vault_status.broken_links counts exactly what gate 3 calls LINK_DEAD."""
        from cadabby.cache import VaultCache

        self._note("wiki/Linker.md", "Linker", "[[Architecture]] [[Nope]] [[x/Missing]] [md](./Gone.md)")
        dead = [f for f in run_vault_lint(self.vault) if f.code == "LINK_DEAD"]
        with VaultCache(self.vault) as cache:
            cache.scan()
            status = cache.get_status()
        self.assertEqual(status["broken_links"], len(dead))
        self.assertEqual(len(dead), 3)
        self.assertNotIn("unprocessed_raw_sources", status)

    def test_markdown_link_is_a_graph_edge_with_backlink(self):
        from cadabby.cache import VaultCache
        from cadabby.graph import get_note_graph

        self._note("customers/acme/Deal.md", "Deal", "Built on [Arch](../../wiki/Architecture.md).")
        with VaultCache(self.vault) as cache:
            cache.scan()
            graph = get_note_graph(cache.get_connection(), "wiki/Architecture")
        self.assertEqual(
            [(b["source_cid"], b["kind"]) for b in graph["backlinks"]],
            [("customers/acme/Deal", "markdown")],
        )

    def test_markdown_link_prevents_orphan(self):
        self._note("customers/acme/Deal.md", "Deal", "Built on [Arch](../../wiki/Architecture.md).")
        self.assertNotIn("NOTE_ORPHAN", [c for c, _ in self._codes_for("wiki/Architecture.md")])

    def test_dead_markdown_link_suggests_relative_path(self):
        self._note("customers/acme/Deal.md", "Deal", "Built on [Arch](../wiki/Architecture.md#B-Tree).")
        dead = [m for c, m in self._codes_for("customers/acme/Deal.md") if c == "LINK_DEAD"]
        self.assertEqual(len(dead), 1)
        self.assertIn("Dead Markdown link '../wiki/Architecture.md#B-Tree'", dead[0])
        self.assertIn("did you mean '../../wiki/Architecture.md#B-Tree'?", dead[0])

    def test_markdown_link_to_non_note_file_is_not_an_edge(self):
        (self.vault_root / "customers").mkdir()
        (self.vault_root / "customers" / "AGENTS.md").write_text("---\ndescription: c\n---\n", "utf-8")
        self._note(
            "wiki/Guide.md",
            "Guide",
            "See [style](../STYLE.md), [manifest](../customers/AGENTS.md), [cite](../raw/x.md) "
            "and [[Architecture]].",
        )
        codes = [c for c, _ in self._codes_for("wiki/Guide.md")]
        self.assertNotIn("LINK_DEAD", codes)

    def test_anchor_matches_github_slug(self):
        self._note(
            "wiki/Reader.md",
            "Reader",
            "[ok](Architecture.md#b-tree-indexes-v2) [[Architecture#B-Tree Indexes (v2)]] "
            "[bad](Architecture.md#no-such-heading)",
        )
        missing = [m for c, m in self._codes_for("wiki/Reader.md") if c == "ANCHOR_MISSING"]
        self.assertEqual(len(missing), 1)
        self.assertIn("#no-such-heading", missing[0])

    def test_ground_payload_reports_link_kind(self):
        from cadabby.ops import ground_notes

        self._note("wiki/Reader.md", "Reader", "[Arch](Architecture.md) and [[Architecture]].")
        payload = ground_notes(self.vault, ["wiki/Reader"])
        kinds = sorted(link["kind"] for link in payload[0]["links"])
        self.assertEqual(kinds, ["markdown", "wiki"])

    def test_gate6_non_string_of_hash_emits_verification_unbound(self):
        (self.vault_root / "wiki" / "BoolOf.md").write_text(
            "---\ntype: concept\ntitle: BoolOf\ndescription: Desc\nstatus: active\n"
            "verified:\n  - by: agent:claude\n    at: '2026-01-01T00:00:00Z'\n    of: true\n---\n\n[[Architecture]]\n",
            "utf-8",
        )
        codes = [c for c, _ in self._codes_for("wiki/BoolOf.md")]
        self.assertIn("VERIFICATION_UNBOUND", codes)

    def test_gate1_empty_or_non_string_required_fields_are_rejected(self):
        (self.vault_root / "wiki" / "EmptyFields.md").write_text(
            '---\ntype: ""\ntitle: ""\ndescription: "   "\nstatus: false\n---\n\n[[Architecture]]\n',
            "utf-8",
        )
        findings = self._codes_for("wiki/EmptyFields.md")
        missing = [m for c, m in findings if c == "FIELD_MISSING"]
        invalid = [m for c, m in findings if c == "ENUM_INVALID"]
        self.assertEqual(len(missing), 3)
        self.assertEqual(len(invalid), 1)
        self.assertIn("Invalid status 'False'", invalid[0])

    def test_anchor_matches_heading_containing_inline_code(self):
        self._note(
            "wiki/CodeHeading.md",
            "CodeHeading",
            "## The `vault_search` Tool\n\nSee [[CodeHeading#the-vault_search-tool]] and [[CodeHeading#The vault_search Tool]].",
        )
        codes = [c for c, _ in self._codes_for("wiki/CodeHeading.md")]
        self.assertNotIn("ANCHOR_MISSING", codes)


if __name__ == "__main__":
    unittest.main()

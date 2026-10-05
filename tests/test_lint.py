"""Unit tests for six-gate epistemic linting."""

from __future__ import annotations

import re
import shutil
import tempfile
import unittest
from pathlib import Path

from cadabby.lint import run_vault_lint
from cadabby.vault import Vault


class TestLint(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.vault_root = Path(self.tmp_dir.name) / "demo-vault"
        demo_src = Path(__file__).resolve().parent.parent / "examples" / "demo-vault"
        shutil.copytree(demo_src, self.vault_root, ignore=shutil.ignore_patterns(".cadabby", "*.pyc"))
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
        self.vault_root = Path(self.tmp_dir.name) / "demo-vault"
        demo_src = Path(__file__).resolve().parent.parent / "examples" / "demo-vault"
        shutil.copytree(demo_src, self.vault_root, ignore=shutil.ignore_patterns(".cadabby", "*.pyc"))
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


if __name__ == "__main__":
    unittest.main()

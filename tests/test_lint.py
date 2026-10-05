"""Unit tests for six-gate epistemic linting."""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()

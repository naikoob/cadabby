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
        shutil.copytree(demo_src, self.vault_root)
        self.vault = Vault(self.vault_root)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_clean_demo_vault_has_zero_errors(self):
        findings = run_vault_lint(self.vault)
        errors = [f for f in findings if f.severity == "error"]
        self.assertEqual(len(errors), 0, f"Clean demo vault had unexpected errors: {errors}")

    def test_gate1_schema_integrity_errors(self):
        # 1. Unparseable frontmatter
        bad_note = self.vault.wiki_dir / "concepts" / "Bad-Yaml.md"
        bad_note.write_text("---\ntags: [flow, style]\n---\n# Bad\n", "utf-8")

        # 2. Missing required field (e.g. description)
        missing_field = self.vault.wiki_dir / "concepts" / "Missing-Field.md"
        missing_field.write_text(
            "---\ntype: concept\ntitle: Missing Field\nstatus: active\n---\n# Content\n",
            "utf-8",
        )

        findings = run_vault_lint(self.vault)
        codes = {f.code for f in findings}
        self.assertIn("FRONTMATTER_UNPARSEABLE", codes)
        self.assertIn("FIELD_MISSING", codes)

    def test_gate2_layout_consistency_error(self):
        # Place a note of type: entity into wiki/concepts/
        mismatched = self.vault.wiki_dir / "concepts" / "Mismatched.md"
        mismatched.write_text(
            "---\ntype: entity\ntitle: Mismatched\ndescription: Test\nstatus: active\n---\n# Note\n",
            "utf-8",
        )

        findings = run_vault_lint(self.vault)
        codes = {f.code for f in findings}
        self.assertIn("TYPE_DIR_MISMATCH", codes)

    def test_gate3_dead_link_error(self):
        # Add a dead link to a note
        note_file = self.vault.wiki_dir / "concepts" / "Epistemic-Trust-Tiers.md"
        content = note_file.read_text("utf-8") + "\n\nLink to [[Non-Existent-Target]]."
        note_file.write_text(content, "utf-8")

        findings = run_vault_lint(self.vault)
        codes = {f.code for f in findings}
        self.assertIn("LINK_DEAD", codes)

    def test_gate4_missing_source_error(self):
        # Add a note citing a non-existent raw file
        missing_src = self.vault.wiki_dir / "concepts" / "Missing-Source.md"
        missing_src.write_text(
            "---\ntype: concept\ntitle: Missing Source\ndescription: Test\nstatus: active\nsources:\n  - raw/ghost.pdf\n---\n# Note\n",
            "utf-8",
        )

        findings = run_vault_lint(self.vault)
        codes = {f.code for f in findings}
        self.assertIn("SOURCE_MISSING", codes)

    def test_gate6_verification_integrity(self):
        # Add a note with malformed actor and unbound verification
        bad_ver = self.vault.wiki_dir / "concepts" / "Bad-Verification.md"
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


if __name__ == "__main__":
    unittest.main()

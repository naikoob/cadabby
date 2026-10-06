"""Unit tests for Git provenance audit of human:* verifications (§3.5)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from cadabby.audit import is_git_repository, run_vault_audit
from cadabby.vault import Vault


class TestAudit(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.vault_root = Path(self.tmp_dir.name) / "vault"
        self.vault_root.mkdir(parents=True, exist_ok=True)
        (self.vault_root / "wiki").mkdir(parents=True, exist_ok=True)
        (self.vault_root / ".cadabby.json").write_text(
            '{\n  "vault_name": "Audit Vault",\n  "identities": {\n    "human:alice": ["alice@example.com", "alice.work@corp.com"]\n  }\n}\n',
            "utf-8",
        )
        self.vault = Vault(self.vault_root)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _write_note(self, stem: str, frontmatter_extra: str = "", body: str = "# Note\n"):
        p = self.vault_root / "wiki" / f"{stem}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        fm = [
            "---",
            "type: concept",
            f'title: "{stem}"',
            'description: "test note"',
            "status: active",
        ]
        if frontmatter_extra:
            fm.append(frontmatter_extra)
        fm.append("---")
        p.write_text("\n".join(fm) + "\n" + body, "utf-8")
        return p

    def test_is_git_repository(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="true\n")
            self.assertTrue(is_git_repository(self.vault))

            mock_run.return_value = MagicMock(returncode=128, stdout="")
            self.assertFalse(is_git_repository(self.vault))

            mock_run.side_effect = FileNotFoundError()
            self.assertFalse(is_git_repository(self.vault))

    def test_non_git_repository_returns_notice(self):
        with patch("cadabby.audit.is_git_repository", return_value=False):
            findings, notice = run_vault_audit(self.vault)
            self.assertEqual(findings, [])
            self.assertIsNotNone(notice)
            self.assertIn("not a Git repository", notice or "")

    def test_vault_without_human_attestations_is_clean(self):
        self._write_note("Note-A", frontmatter_extra="verified:\n  - by: agent:verifier\n    at: '2026-10-01T00:00:00Z'\n    of: sha256:abc")
        with patch("cadabby.audit.is_git_repository", return_value=True):
            findings, notice = run_vault_audit(self.vault)
            self.assertEqual(findings, [])
            self.assertIsNone(notice)

    def test_provenance_mismatch_detected(self):
        self._write_note(
            "Note-Alice",
            frontmatter_extra="verified:\n  - by: human:alice\n    at: '2026-10-01T00:00:00Z'\n    of: sha256:abc",
        )

        mock_blame = (
            "commit123 1 1 1\n"
            "author Mallory\n"
            "author-mail <mallory@evil.com>\n"
            "\t  - by: human:alice\n"
        )

        with (
            patch("cadabby.audit.is_git_repository", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=mock_blame)
            findings, notice = run_vault_audit(self.vault)
            self.assertIsNone(notice)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].code, "PROVENANCE_MISMATCH")
            self.assertEqual(findings[0].actor, "human:alice")
            self.assertIn("mallory@evil.com", findings[0].message)

    def test_provenance_match_clean(self):
        self._write_note(
            "Note-Alice",
            frontmatter_extra="verified:\n  - by: human:alice\n    at: '2026-10-01T00:00:00Z'\n    of: sha256:abc",
        )

        mock_blame = (
            "commit123 1 1 1\n"
            "author Alice\n"
            "author-mail <alice@example.com>\n"
            "\t  - by: human:alice\n"
        )

        with (
            patch("cadabby.audit.is_git_repository", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=mock_blame)
            findings, notice = run_vault_audit(self.vault)
            self.assertIsNone(notice)
            self.assertEqual(findings, [])

    def test_unconfigured_actor_identity_fails_provenance(self):
        self._write_note(
            "Note-Bob",
            frontmatter_extra="verified:\n  - by: human:bob\n    at: '2026-10-01T00:00:00Z'\n    of: sha256:abc",
        )

        mock_blame = (
            "commit123 1 1 1\n"
            "author Bob\n"
            "author-mail <bob@example.com>\n"
            "\t  - by: human:bob\n"
        )

        with (
            patch("cadabby.audit.is_git_repository", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=mock_blame)
            findings, notice = run_vault_audit(self.vault)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].code, "PROVENANCE_MISMATCH")
            self.assertIn("is not mapped to 'human:bob'", findings[0].message)

    def test_unsigned_endorsement_when_require_signed(self):
        self._write_note(
            "Note-Alice",
            frontmatter_extra="verified:\n  - by: human:alice\n    at: '2026-10-01T00:00:00Z'\n    of: sha256:abc",
        )

        mock_blame = (
            "commit456 1 1 1\n"
            "author Alice\n"
            "author-mail <alice@example.com>\n"
            "\t  - by: human:alice\n"
        )

        with (
            patch("cadabby.audit.is_git_repository", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            def fake_run(cmd, **kwargs):
                ret = MagicMock()
                if "blame" in cmd:
                    ret.returncode = 0
                    ret.stdout = mock_blame
                elif "log" in cmd:
                    # 'N' indicates no signature
                    ret.returncode = 0
                    ret.stdout = "N\n"
                return ret

            mock_run.side_effect = fake_run
            findings, notice = run_vault_audit(self.vault, require_signed=True)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].code, "UNSIGNED_ENDORSEMENT")
            self.assertIn("is unsigned (status=N)", findings[0].message)

    def test_signed_endorsement_passes_when_require_signed(self):
        self._write_note(
            "Note-Alice",
            frontmatter_extra="verified:\n  - by: human:alice\n    at: '2026-10-01T00:00:00Z'\n    of: sha256:abc",
        )

        mock_blame = (
            "commit456 1 1 1\n"
            "author Alice\n"
            "author-mail <alice@example.com>\n"
            "\t  - by: human:alice\n"
        )

        with (
            patch("cadabby.audit.is_git_repository", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            def fake_run(cmd, **kwargs):
                ret = MagicMock()
                if "blame" in cmd:
                    ret.returncode = 0
                    ret.stdout = mock_blame
                elif "log" in cmd:
                    # 'G' indicates good valid signature
                    ret.returncode = 0
                    ret.stdout = "G\n"
                return ret

            mock_run.side_effect = fake_run
            findings, notice = run_vault_audit(self.vault, require_signed=True)
            self.assertEqual(findings, [])

    def test_audit_ignores_body_mentions_of_human_attestations(self):
        # Body text or code block mentioning - by: human:alice
        body_with_mentions = (
            "# Main Note\n\n"
            "Here is an example snippet:\n"
            "```yaml\n"
            "- by: human:alice\n"
            "```\n\n"
            "- by: human:alice in body list item\n"
        )
        self._write_note("Note-Mentions", body=body_with_mentions)

        with (
            patch("cadabby.audit.is_git_repository", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            findings, notice = run_vault_audit(self.vault)
            self.assertEqual(findings, [])
            # subprocess.run should never even be called for git blame
            mock_run.assert_not_called()

    def test_unparseable_or_missing_frontmatter_ignored(self):
        broken = self.vault_root / "wiki" / "Broken.md"
        broken.write_text("No frontmatter at all here\n", "utf-8")

        with (
            patch("cadabby.audit.is_git_repository", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            findings, notice = run_vault_audit(self.vault)
            self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()

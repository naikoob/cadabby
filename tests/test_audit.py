"""Unit tests for Git provenance audit of human:* verifications (§3.5)."""

from __future__ import annotations

import os
import shutil
import subprocess
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
            "commit123 2 2\n"
            "\t    at: '2026-10-01T00:00:00Z'\n"
            "commit123 3 3\n"
            "\t    of: sha256:abc\n"
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
            "commit123 2 2\n"
            "\t    at: '2026-10-01T00:00:00Z'\n"
            "commit123 3 3\n"
            "\t    of: sha256:abc\n"
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
            "commit123 2 2\n"
            "\t    at: '2026-10-01T00:00:00Z'\n"
            "commit123 3 3\n"
            "\t    of: sha256:abc\n"
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

    def test_uncommitted_endorsement_is_a_provenance_failure(self):
        """C39: an endorsement Git has no commit for is reported as such, not as "unsigned".

        `git blame` attributes an uncommitted line to the all-zero commit with
        a placeholder author. It used to surface under --require-signed as
        "unsigned (status=)" from a `git log` of a commit that does not exist.
        """
        self._write_note(
            "Note-Alice",
            frontmatter_extra="verified:\n  - by: human:alice\n    at: '2026-10-01T00:00:00Z'\n    of: sha256:abc",
        )
        blame = f"{'0' * 40} 1 1 1\nauthor Not Committed Yet\nauthor-mail <not.committed.yet>\n\t  - by: human:alice\n"
        with (
            patch("cadabby.audit.is_git_repository", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=blame)
            findings, _ = run_vault_audit(self.vault, require_signed=True)
        self.assertEqual([f.code for f in findings], ["PROVENANCE_MISMATCH"])
        self.assertIn("not yet committed", findings[0].message)
        self.assertEqual(mock_run.call_count, 1, "no `git log` for a commit that does not exist")

    def test_untracked_endorsement_is_a_provenance_failure(self):
        """§3.5, §10 C39: a file Git does not track is reported, not skipped as if clean."""
        self._write_note(
            "Forged",
            frontmatter_extra="verified:\n  - by: human:alice\n    at: '2026-10-01T00:00:00Z'\n    of: sha256:abc",
        )
        with (
            patch("cadabby.audit.is_git_repository", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(
                returncode=128, stdout="", stderr="fatal: no such path 'wiki/Forged.md' in HEAD\n"
            )
            findings, _ = run_vault_audit(self.vault, require_signed=True)
        self.assertEqual([f.code for f in findings], ["PROVENANCE_MISMATCH"])
        self.assertEqual(findings[0].rel_path, "wiki/Forged.md")
        self.assertIn("no Git history", findings[0].message)
        self.assertEqual(mock_run.call_count, 1, "no signature check without a commit")

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_untracked_endorsement_real_git(self):
        """The reviewer's repro against real Git: a new, never-added forged note."""
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
        subprocess.run(["git", "init", "-q"], cwd=self.vault_root, check=True, env=env)
        self._write_note(
            "Forged",
            frontmatter_extra="verified:\n  - by: human:alice\n    at: '2026-10-01T00:00:00Z'\n    of: sha256:abc",
        )
        with patch.dict(os.environ, env):
            findings, notice = run_vault_audit(self.vault)
        self.assertIsNone(notice)
        self.assertEqual([(f.code, f.rel_path) for f in findings], [("PROVENANCE_MISMATCH", "wiki/Forged.md")])

    def test_empty_author_never_matches(self):
        (self.vault_root / ".cadabby.json").write_text('{"identities": {"human:alice": [""]}}', "utf-8")
        self._write_note(
            "Note-Alice",
            frontmatter_extra="verified:\n  - by: human:alice\n    at: '2026-10-01T00:00:00Z'\n    of: sha256:abc",
        )
        with (
            patch("cadabby.audit.is_git_repository", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="commit123 1 1 1\nauthor-mail <>\n")
            findings, _ = run_vault_audit(Vault(self.vault_root))
        self.assertEqual([f.code for f in findings], ["PROVENANCE_MISMATCH"])

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
            "commit456 2 2\n"
            "\t    at: '2026-10-01T00:00:00Z'\n"
            "commit456 3 3\n"
            "\t    of: sha256:abc\n"
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
            "commit456 2 2\n"
            "\t    at: '2026-10-01T00:00:00Z'\n"
            "commit456 3 3\n"
            "\t    of: sha256:abc\n"
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

    def test_provenance_mismatch_when_by_is_not_first_key(self):
        self._write_note(
            "Note-Reordered",
            frontmatter_extra=(
                "verified:\n"
                "  - at: '2026-10-01T00:00:00Z'\n"
                "    by: human:alice # inline comment\n"
                "    of: sha256:abc"
            ),
        )

        mock_blame = (
            "commit789 1 1 1\n"
            "author Mallory\n"
            "author-mail <mallory@evil.com>\n"
            "\t    by: human:alice # inline comment\n"
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
            self.assertEqual(findings[0].line, 8)

    def test_audit_handles_non_utf8_file_without_crashing(self):
        corrupt_file = self.vault_root / "wiki" / "Corrupt.md"
        corrupt_file.write_bytes(b"---\ntype: concept\ntitle: Corrupt\nstatus: active\n---\n# Corrupt\n\xff\n")
        with (
            patch("cadabby.audit.is_git_repository", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            findings, notice = run_vault_audit(self.vault)
            self.assertEqual(findings, [])
            self.assertIsNone(notice)

    def test_git_log_uses_positional_separator(self):
        self._write_note(
            "Note-Alice",
            frontmatter_extra="verified:\n  - by: human:alice\n    at: '2026-10-01T00:00:00Z'\n    of: sha256:abc",
        )
        mock_blame = (
            "commit456 1 1 1\n"
            "author Alice\n"
            "author-mail <alice@example.com>\n"
            "\t  - by: human:alice\n"
            "commit456 2 2\n"
            "\t    at: '2026-10-01T00:00:00Z'\n"
            "commit456 3 3\n"
            "\t    of: sha256:abc\n"
        )
        commands_run: list[list[str]] = []
        with (
            patch("cadabby.audit.is_git_repository", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            def fake_run(cmd, **kwargs):
                commands_run.append(cmd)
                ret = MagicMock()
                if "blame" in cmd:
                    ret.returncode = 0
                    ret.stdout = mock_blame
                elif "log" in cmd:
                    ret.returncode = 0
                    ret.stdout = "G\n"
                return ret

            mock_run.side_effect = fake_run
            findings, notice = run_vault_audit(self.vault, require_signed=True)
            self.assertEqual(findings, [])
            log_cmds = [c for c in commands_run if "log" in c]
            self.assertEqual(len(log_cmds), 1)
            self.assertEqual(log_cmds[0][-1], "--")

    # A hung git never hangs the audit, and never lets an endorsement pass unchecked.
    # (Kept in TestAudit: the runner's pattern-filter test expects one class here.)

    def test_a_hung_rev_parse_reads_as_no_repository(self):
        vault = MagicMock()
        vault.root = Path(tempfile.gettempdir())
        with patch("cadabby.audit.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 30)) as run:
            self.assertFalse(is_git_repository(vault))
        self.assertEqual(run.call_args.kwargs["timeout"], 30)

    def test_a_hung_blame_fails_the_audit(self):
        from cadabby.audit import GitTimeoutError

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / ".cadabby.json").write_text('{"vault_name": "v"}', "utf-8")
        (root / "wiki").mkdir()
        (root / "wiki" / "n.md").write_text(
            "---\ntype: concept\ntitle: N\ndescription: d\nstatus: active\nverified:\n"
            "  - by: human:alice\n    at: '2026-01-01T00:00:00Z'\n    method: manual-review\n"
            "    of: 'sha256:" + "0" * 64 + "'\n---\n# N\n",
            "utf-8",
        )
        vault = Vault(root)

        def fake_run(cmd, **kwargs):
            if "rev-parse" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="true\n", stderr="")
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

        with patch("cadabby.audit.subprocess.run", side_effect=fake_run):
            with self.assertRaises(GitTimeoutError):
                run_vault_audit(vault)

    # --- C73: real-Git bypasses from code review 2 --------------------------

    def _git_env(self, name: str, email: str) -> dict[str, str]:
        return {
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
        }

    def _commit_as(self, name: str, email: str, message: str) -> None:
        env = self._git_env(name, email)
        subprocess.run(["git", "add", "-A"], cwd=self.vault_root, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", message], cwd=self.vault_root, check=True, env=env)

    def _alice_endorsed_repo(self) -> Path:
        subprocess.run(["git", "init", "-q"], cwd=self.vault_root, check=True, env=self._git_env("a", "a@x"))
        note = self._write_note(
            "Endorsed",
            frontmatter_extra="verified:\n  - by: human:alice\n    at: '2026-10-01T00:00:00Z'\n"
            "    method: manual-review\n    of: 'sha256:" + "a" * 64 + "'",
        )
        self._commit_as("Alice", "alice@example.com", "alice reviews")
        return note

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_rewriting_only_the_of_line_is_caught(self):
        """S2a: the agent edits the body and re-points alice's `of:`; her `by:` line keeps her commit."""
        note = self._alice_endorsed_repo()
        text = note.read_text("utf-8").replace("a" * 64, "b" * 64).replace("# Note", "# Note\n\nAgent text.")
        note.write_text(text, "utf-8")
        self._commit_as("Agent", "agent@bot.invalid", "agent edits")
        with patch.dict(os.environ, self._git_env("x", "x@x")):
            findings, notice = run_vault_audit(Vault(self.vault_root))
        self.assertIsNone(notice)
        self.assertEqual([f.code for f in findings], ["PROVENANCE_MISMATCH"])
        of_line = next(n for n, l in enumerate(text.split("\n"), start=1) if l.strip().startswith("of:"))
        self.assertEqual(findings[0].line, of_line)
        self.assertIn("agent@bot.invalid", findings[0].message)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_an_untouched_endorsement_stays_clean(self):
        self._alice_endorsed_repo()
        with patch.dict(os.environ, self._git_env("x", "x@x")):
            findings, _ = run_vault_audit(Vault(self.vault_root))
        self.assertEqual(findings, [])

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_a_carriage_return_forgery_is_unparseable_and_reported(self):
        """S2b: a `\\r`-joined entry on one Git line no longer borrows another line's blame."""
        from cadabby.frontmatter import FrontmatterParseError, parse_frontmatter

        note = self._alice_endorsed_repo()
        forged = self._write_note("Forged-CR")
        self._commit_as("Agent", "agent@bot.invalid", "plain note")
        text = forged.read_text("utf-8").replace(
            'status: active\n',
            "status: active\rverified:\r  - by: human:alice\r    at: '2026-10-01T00:00:00Z'\r"
            "    of: 'sha256:" + "c" * 64 + "'\n",
        )
        forged.write_bytes(text.encode("utf-8"))
        self._commit_as("Agent", "agent@bot.invalid", "forge")
        with self.assertRaises(FrontmatterParseError):
            parse_frontmatter(text)
        with patch.dict(os.environ, self._git_env("x", "x@x")):
            findings, _ = run_vault_audit(Vault(self.vault_root))
        self.assertEqual([(f.code, f.rel_path) for f in findings], [("PROVENANCE_MISMATCH", "wiki/Forged-CR.md")])
        self.assertIn("cannot be audited", findings[0].message)
        self.assertTrue(note.exists())

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_repository_config_cannot_run_code_during_an_audit(self):
        """S11: a shipped .git/config naming a core.fsmonitor program is not executed."""
        self._alice_endorsed_repo()
        sentinel = Path(self.tmp_dir.name) / "PWNED"
        hook = Path(self.tmp_dir.name) / "fsmonitor.sh"
        hook.write_text(f"#!/bin/sh\ntouch '{sentinel}'\n", "utf-8")
        hook.chmod(0o755)
        subprocess.run(["git", "config", "core.fsmonitor", str(hook)], cwd=self.vault_root, check=True,
                       env=self._git_env("x", "x@x"))
        with patch.dict(os.environ, self._git_env("x", "x@x")):
            run_vault_audit(Vault(self.vault_root))
        self.assertFalse(sentinel.exists())


if __name__ == "__main__":
    unittest.main()


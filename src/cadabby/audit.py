"""Git provenance audit of human:* verification endorsements.

Conforms strictly to Cadabby Technical Specification §3.5.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cadabby.constants import ACTOR_PATTERN
from cadabby.frontmatter import FrontmatterParseError, split_comment, split_frontmatter
from cadabby.okf import is_actor_human, split_lines
from cadabby.vault import Vault

# What `git blame --porcelain` reports as the commit of a line not yet committed.
UNCOMMITTED_SHA = "0" * 40


@dataclass
class AuditFinding:
    code: str  # "PROVENANCE_MISMATCH" | "UNSIGNED_ENDORSEMENT"
    rel_path: str
    line: int
    actor: str
    commit: str
    message: str


# Per git call. A hung git (a credential prompt, a stale index.lock on a
# network share) must not hang `cadabby audit` -- or a pre-commit hook -- forever.
GIT_TIMEOUT_SECONDS = 30


class GitTimeoutError(OSError):
    """A git subprocess exceeded GIT_TIMEOUT_SECONDS (IO_ERROR, §5.4)."""


# Options that keep a repository's own config from running code during an
# audit. A vault delivered as an archive or synced folder can carry its own
# .git/config; `core.fsmonitor` there names a program git executes (§3.5).
_GIT_SAFETY = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "diff.external=")


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run git with a timeout and repository-config hardening; GitTimeoutError if it hangs."""
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    try:
        return subprocess.run(
            ["git", *_GIT_SAFETY, *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired as e:
        raise GitTimeoutError(f"git {args[0]} did not finish within {GIT_TIMEOUT_SECONDS}s") from e


def is_git_repository(vault: Vault) -> bool:
    """Return True if vault directory is inside a Git worktree."""
    try:
        res = _git(["rev-parse", "--is-inside-work-tree"], vault.root)
        return res.returncode == 0 and res.stdout.strip() == "true"
    except OSError:  # no git binary, or it hung: treated as no repository (a notice)
        return False


@dataclass
class _HumanEntry:
    """One `verified:` item attributed to a human, with the file lines it spans."""

    actor: str
    by_line: int
    lines: list[int]


def _human_entries(raw_fm: str, re_by: re.Pattern[str]) -> list[_HumanEntry]:
    """Every `human:*` item under `verified:`, each with all of its own lines.

    Line numbers are file lines as Git counts them. The parser has already
    refused a lone carriage return in frontmatter, so `split_lines` and Git's
    `\n`-only numbering agree; before that, a `\r`-joined forged entry hid on
    one Git line and borrowed the blame of an older, genuine one.
    """
    entries: list[_HumanEntry] = []
    current: list[int] | None = None
    actor: str | None = None
    by_line = 0
    in_verified = False

    def close() -> None:
        if current and actor is not None:
            entries.append(_HumanEntry(actor=actor, by_line=by_line, lines=current))

    for line_no, line in enumerate(split_lines(raw_fm), start=2):
        clean_line, _ = split_comment(line)
        if not clean_line.strip():
            continue
        indent = len(clean_line) - len(clean_line.lstrip(" "))
        if indent == 0:
            close()
            current, actor = None, None
            in_verified = bool(re.match(r"^verified\s*:", clean_line))
            continue
        if not in_verified:
            continue
        if clean_line.lstrip().startswith("-"):
            close()
            current, actor = [line_no], None
        elif current is not None:
            current.append(line_no)
        else:
            continue
        match = re_by.match(clean_line)
        if match and is_actor_human(match.group(1)):
            actor, by_line = match.group(1).strip(), line_no
    close()
    return entries


def _blamed_lines(porcelain: str) -> list[tuple[str, str]]:
    """`(commit, author email)` for each line `git blame --porcelain` reports, in order.

    Porcelain prints a commit's author headers only on its first line, so the
    email is remembered per commit.
    """
    mails: dict[str, str] = {}
    lines: list[tuple[str, str]] = []
    commit = ""
    for raw in porcelain.splitlines():
        if raw.startswith("\t"):
            lines.append((commit, mails.get(commit, "")))
            continue
        parts = raw.split()
        if len(parts) in (3, 4) and all(p.isdigit() for p in parts[1:]):
            commit = parts[0]
        elif raw.startswith("author-mail "):
            mails[commit] = raw[len("author-mail ") :].strip().strip("<>").strip()
    return lines


def run_vault_audit(vault: Vault, require_signed: bool = False) -> tuple[list[AuditFinding], str | None]:
    """Audit all human:* verification entries against Git commit provenance.

    Every line of a human entry is blamed, not just its `by:` line: an agent
    that rewrote only `of:` in a person's entry (after editing the body) left
    the `by:` line with the person's commit, and the audit clean.

    Returns:
        (findings, notice_or_none)
    """
    if not is_git_repository(vault):
        return [], "Vault is not a Git repository. Provenance audit requires Git history."

    identities: dict[str, list[str]] = vault.config["identities"]
    findings: list[AuditFinding] = []

    # A `by:` line under `verified:`, actor grammar taken from the spec's one pattern (§3.4).
    re_by = re.compile(rf"^\s*(?:-\s*)?by:\s*[\"']?({ACTOR_PATTERN.strip('^$')})[\"']?\s*$")

    for file_path, _, _ in vault.iter_domain_notes():
        rel_path = vault.rel_path(file_path)
        try:
            # Bytes: text mode would turn a lone `\r` into `\n` and hide it from the parser.
            content = file_path.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue

        try:
            raw_fm, _ = split_frontmatter(content)
        except FrontmatterParseError as exc:
            # Fail closed: an endorsement the audit cannot read is not a clean one.
            if "human:" in content:
                findings.append(
                    AuditFinding(
                        code="PROVENANCE_MISMATCH",
                        rel_path=rel_path,
                        line=exc.line_number,
                        actor="",
                        commit="",
                        message=f"frontmatter does not parse ({exc.message}); its human:* endorsements cannot be audited",
                    )
                )
            continue

        if not raw_fm:
            continue

        for entry in _human_entries(raw_fm, re_by):
            findings.extend(_audit_entry(vault, rel_path, entry, identities.get(entry.actor, []), require_signed))

    return findings, None


def _audit_entry(
    vault: Vault, rel_path: str, entry: _HumanEntry, allowed_emails: list[str], require_signed: bool
) -> list[AuditFinding]:
    """Findings for one human entry: every line must be committed by one of the actor's identities."""
    first, last = entry.lines[0], entry.lines[-1]

    def finding(code: str, line: int, commit: str, message: str) -> AuditFinding:
        return AuditFinding(code=code, rel_path=rel_path, line=line, actor=entry.actor, commit=commit, message=message)

    blame_res = _git(["blame", "--porcelain", f"-L{first},{last}", "--", rel_path], vault.root)
    if blame_res.returncode != 0 or not blame_res.stdout:
        # Git has no history for this line: the file is untracked or
        # ignored, which is exactly where the cheapest forgery lives (a
        # new file carrying `by: human:...`). An endorsement with no
        # author to check is a mismatch, as for an uncommitted line (§3.5).
        reason = (blame_res.stderr or "").strip().splitlines()[:1]
        return [
            finding(
                "PROVENANCE_MISMATCH",
                entry.by_line,
                "",
                f"'{entry.actor}' attestation has no Git history (file untracked or ignored"
                + (f": {reason[0]}" if reason else "")
                + "); commit it to make its provenance auditable",
            )
        ]

    blamed = _blamed_lines(blame_res.stdout)
    # The by: line first, so the common case reports what it always has.
    ordered = [entry.by_line] + [n for n in entry.lines if n != entry.by_line]
    commits: list[str] = []
    for line_no in ordered:
        offset = line_no - first
        commit, author_mail = blamed[offset] if offset < len(blamed) else ("", "")
        which = "attestation" if line_no == entry.by_line else f"attestation (its line {line_no})"
        if commit == UNCOMMITTED_SHA:
            return [
                finding(
                    "PROVENANCE_MISMATCH",
                    line_no,
                    commit,
                    f"'{entry.actor}' {which} is not yet committed, so Git records no author for it; "
                    "commit it to make its provenance auditable",
                )
            ]
        # An empty author (unparseable blame) never matches, even if "" crept
        # into the identities list.
        if not author_mail or author_mail not in allowed_emails:
            return [
                finding(
                    "PROVENANCE_MISMATCH",
                    line_no,
                    commit,
                    f"Commit author '{author_mail}' ({commit[:8]}) of the '{entry.actor}' {which} "
                    f"is not mapped to '{entry.actor}' in .cadabby.json identities",
                )
            ]
        if commit not in commits:
            commits.append(commit)

    if not require_signed:
        return []
    # %G? is 'G' (good) or 'U' (good, untrusted key) for a valid signature;
    # anything else -- 'N' unsigned, 'B' bad, 'E' unverifiable -- is flagged,
    # and so is a signature check that could not run (fail closed).
    for commit in commits:
        try:
            status_flag = _git(["log", "-1", "--format=%G?", commit, "--"], vault.root).stdout.strip()
        except GitTimeoutError:
            raise
        except OSError as exc:
            status_flag = f"unknown: {exc}"
        if status_flag not in ("G", "U"):
            return [
                finding(
                    "UNSIGNED_ENDORSEMENT",
                    entry.by_line,
                    commit,
                    f"Commit {commit[:8]} carrying '{entry.actor}' attestation is unsigned (status={status_flag})",
                )
            ]
    return []

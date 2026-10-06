"""Git provenance audit of human:* verification endorsements.

Conforms strictly to Cadabby Technical Specification §3.5.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from cadabby.frontmatter import split_frontmatter
from cadabby.vault import Vault


@dataclass
class AuditFinding:
    code: str  # "PROVENANCE_MISMATCH" | "UNSIGNED_ENDORSEMENT"
    rel_path: str
    line: int
    actor: str
    commit: str
    message: str


def is_git_repository(vault: Vault) -> bool:
    """Return True if vault directory is inside a Git worktree."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=vault.root,
            capture_output=True,
            text=True,
            check=False,
        )
        return res.returncode == 0 and res.stdout.strip() == "true"
    except (FileNotFoundError, OSError):
        return False


def run_vault_audit(vault: Vault, require_signed: bool = False) -> tuple[list[AuditFinding], str | None]:
    """Audit all human:* verification entries against Git commit provenance.

    Returns:
        (findings, notice_or_none)
    """
    if not is_git_repository(vault):
        return [], "Vault is not a Git repository. Provenance audit requires Git history."

    identities: dict[str, list[str]] = vault.config.get("identities", {})
    findings: list[AuditFinding] = []

    all_note_files = [p for p, _, _ in vault.iter_domain_notes()]

    re_human_by = re.compile(r"^\s*-\s*by:\s*[\"']?(human:[A-Za-z0-9._\-/]+)[\"']?\s*")

    for file_path in all_note_files:
        rel_path = vault.rel_path(file_path)
        try:
            content = file_path.read_text("utf-8")
        except OSError:
            continue

        try:
            raw_fm, _, _ = split_frontmatter(content)
        except Exception:
            continue

        if not raw_fm:
            continue

        for line_no, line in enumerate(raw_fm.splitlines(), start=2):
            match = re_human_by.match(line)
            if not match:
                continue

            actor = match.group(1).strip()
            allowed_emails = identities.get(actor, [])

            # Run git blame --porcelain for this specific line
            cmd = ["git", "blame", "--porcelain", f"-L{line_no},{line_no}", "--", rel_path]
            try:
                blame_res = subprocess.run(
                    cmd,
                    cwd=vault.root,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except OSError:
                continue

            if blame_res.returncode != 0 or not blame_res.stdout:
                continue

            # Parse git blame porcelain output
            commit_hash = ""
            author_mail = ""
            for blame_line in blame_res.stdout.splitlines():
                if not commit_hash and blame_line:
                    commit_hash = blame_line.split()[0]
                elif blame_line.startswith("author-mail "):
                    raw_mail = blame_line[len("author-mail ") :].strip()
                    author_mail = raw_mail.strip("<>").strip()

            # Cross-reference with identities map
            if not allowed_emails or author_mail not in allowed_emails:
                findings.append(
                    AuditFinding(
                        code="PROVENANCE_MISMATCH",
                        rel_path=rel_path,
                        line=line_no,
                        actor=actor,
                        commit=commit_hash,
                        message=(
                            f"Commit author '{author_mail}' ({commit_hash[:8]}) is not mapped "
                            f"to '{actor}' in .cadabby.json identities"
                        ),
                    )
                )

            # Check commit signature if requested
            if require_signed and commit_hash:
                try:
                    sig_res = subprocess.run(
                        ["git", "log", "-1", "--format=%G?", commit_hash],
                        cwd=vault.root,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    status_flag = sig_res.stdout.strip()
                    if status_flag not in ("G", "U"):
                        findings.append(
                            AuditFinding(
                                code="UNSIGNED_ENDORSEMENT",
                                rel_path=rel_path,
                                line=line_no,
                                actor=actor,
                                commit=commit_hash,
                                message=f"Commit {commit_hash[:8]} carrying '{actor}' attestation is unsigned (status={status_flag})",
                            )
                        )
                except OSError:
                    pass

    return findings, None

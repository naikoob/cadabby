"""index.md gap report generation, log appending, and ledger rotation.

Conforms to Cadabby Technical Specification §2.5, §4.3, §8.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from cadabby.okf import utc_now_iso

from cadabby.cache import VaultCache, unprocessed_raw_paths
from cadabby.constants import LEDGER_TITLE, TYPE_MOC
from cadabby.fsutil import advisory_lock, append_ledger, atomic_write
from cadabby.vault import Vault

RE_LEDGER_HEADER_YEAR = re.compile(r"^#\s+Activity Ledger\s+\((\d{4})\)", re.MULTILINE)
RE_LEDGER_ENTRY_YEAR = re.compile(r"^-\s+\[(\d{4})-\d{2}-\d{2}T", re.MULTILINE)


def _detect_ledger_year(content: str, fallback_year: str) -> str:
    """Extract the active calendar year from log.md header or earliest entry timestamp."""
    m_hdr = RE_LEDGER_HEADER_YEAR.search(content)
    if m_hdr:
        return m_hdr.group(1)
    m_ent = RE_LEDGER_ENTRY_YEAR.search(content)
    if m_ent:
        return m_ent.group(1)
    return fallback_year


def append_vault_log(vault: Vault, message: str, actor: str | None = None) -> None:
    """Append a timestamped entry to the active log.md ledger."""
    prefix = f"[{utc_now_iso()}]"
    if actor:
        prefix += f" {actor}:"
    entry = f"- {prefix} {message}"
    append_ledger(vault.log_path, entry)


def rotate_vault_log(vault: Vault) -> Path | None:
    """Rotate log.md into log/<year>.md if it exceeds log_rotate_bytes or crosses a calendar year (§8).

    The size, the year and the decision are all read under the lock: decided
    outside it, two concurrent syncs could both choose to rotate and archive
    the same ledger twice.
    """
    log_file = vault.log_path
    max_bytes = vault.config["log_rotate_bytes"]

    with advisory_lock(vault.lock_path):
        if not log_file.exists():
            return None
        content = log_file.read_text("utf-8")
        current_year = utc_now_iso()[:4]
        ledger_year = _detect_ledger_year(content, current_year)
        if len(content.encode("utf-8")) < max_bytes and ledger_year == current_year:
            return None

        target_rotated = vault.log_dir / f"{ledger_year}.md"
        target_rotated.parent.mkdir(parents=True, exist_ok=True)
        if target_rotated.exists():
            header_prefix = f"{LEDGER_TITLE} ({ledger_year})"
            body_content = content.removeprefix(header_prefix).lstrip("\r\n") if content.startswith(
                header_prefix
            ) else content
            if body_content:
                append_ledger(target_rotated, body_content)
        else:
            atomic_write(target_rotated, content)

        atomic_write(log_file, f"{LEDGER_TITLE} ({current_year})\n\n")

    return target_rotated


def _cell(value: Any, fallback: str = "") -> str:
    """Escape a value for a markdown table cell."""
    return str(value or fallback).replace("\n", " ").replace("|", "\\|")


def _entry_points(conn: sqlite3.Connection) -> list[str]:
    """Section 1: every MOC, linked. The human's starting page (§2.5)."""
    rows = conn.execute(
        """
        SELECT stem, description FROM notes
        WHERE layer = 'wiki' AND type = ? AND parse_error IS NULL
        ORDER BY stem ASC;
        """,
        (TYPE_MOC,),
    ).fetchall()
    if not rows:
        return []
    lines = ["## Entry Points", "", "| Map of Content | Description |", "| :--- | :--- |"]
    lines += [f"| [[{r['stem']}]] | {_cell(r['description'])} |" for r in rows]
    return lines + [""]


def _unfiled(conn: sqlite3.Connection) -> list[str]:
    """Section 2: wiki notes no MOC reaches by forward link at any depth (§2.5).

    The one failure mode flat layout introduces, and the one `lint` cannot
    catch: a note that links outward has edges, so gate 5 sees no orphan.

    Reachability is transitive and may travel through any layer -- a link is a
    link, and a wiki note pulled in via a domain note is still filed. Only wiki
    notes are *reported*, because MOCs are a `wiki/` construct; domains carry
    their own topology in folders (§2.5). `deprecated` notes are excluded: the
    section is a worklist, and filing a retired note is not work. Without that
    exclusion the report has a floor it can never reach zero from, which would
    defeat the point of shrinking it.
    """
    rows = conn.execute(
        """
        WITH RECURSIVE filed(cid) AS (
            SELECT cid FROM notes
            WHERE layer = 'wiki' AND type = ? AND parse_error IS NULL
          UNION
            SELECT l.target_cid FROM links l
            JOIN filed f ON l.source_cid = f.cid
            WHERE l.target_cid IS NOT NULL
        )
        SELECT stem, description FROM notes
        WHERE layer = 'wiki'
          AND parse_error IS NULL
          AND COALESCE(status, 'active') != 'deprecated'
          AND cid NOT IN (SELECT cid FROM filed)
        ORDER BY stem ASC;
        """,
        (TYPE_MOC,),
    ).fetchall()
    if not rows:
        return []
    lines = [
        "## Unfiled Notes",
        "",
        "> Reachable from no Map of Content. Link each from the MOC it belongs to.",
        "",
        "| Note | Description |",
        "| :--- | :--- |",
    ]
    lines += [f"| [[{r['stem']}]] | {_cell(r['description'])} |" for r in rows]
    return lines + [""]


def _raw_queue(conn: sqlite3.Connection) -> list[str]:
    """Section 3: the ingestion queue. Vault-wide -- any domain may cite raw/."""
    pending = unprocessed_raw_paths(conn)
    if not pending:
        return []
    lines = [
        "## Raw Sources",
        "",
        "> Unprocessed: no note cites them yet. Processed sources are omitted.",
        "",
        "| Source File |",
        "| :--- |",
    ]
    lines += [f"| `{path}` |" for path in pending]
    return lines + [""]


def _verification_debt(conn: sqlite3.Connection) -> list[str]:
    """Section 4: notes whose verification entries have all gone stale (§3.4).

    Vault-wide: any note in any domain can carry verification entries.
    """
    rows = conn.execute(
        """
        SELECT cid FROM notes
        WHERE trust_tier = 'stale-verified' AND parse_error IS NULL
        ORDER BY cid ASC;
        """
    ).fetchall()
    if not rows:
        return []
    lines = [
        "## Verification Debt",
        "",
        "> Body changed since the last attestation. Re-verify or retire.",
        "",
        "| Note |",
        "| :--- |",
    ]
    lines += [f"| `{r['cid']}` |" for r in rows]
    return lines + [""]


def _build_index_lines_from_conn(vault: Vault, conn: sqlite3.Connection) -> str:
    """Render the vault-wide gap report (§2.5).

    Deliberately not a catalog. A table of every wiki note duplicates what MOCs
    curate, what a file explorer shows for a flat folder, and what search ranks
    better -- while churning the Git diff on every note added. Each section
    below is omitted when empty, so a fully filed, fully ingested, fully
    verified vault produces its entry points and one "Nothing outstanding" line. Shrinking this file is
    the objective: every row is work someone still has to do.
    """
    lines = [
        f"# {vault.config['vault_name']} Index",
        "",
        "> Auto-generated gap report (§2.5). Not a catalog, and never hand-edited:",
        "> edits are overwritten on the next scan that finds changes.",
        "",
    ]

    gaps = _unfiled(conn) + _raw_queue(conn) + _verification_debt(conn)
    lines += _entry_points(conn)
    if not gaps:
        lines.append("Nothing outstanding: every note is filed, every source processed, every attestation current.")
    lines += gaps

    return "\n".join(lines).strip() + "\n"


def generate_index_markdown(vault: Vault, cache: VaultCache) -> str:
    """Render the gap report for index.md (§2.5) from an already-scanned cache.

    Deterministic sort order guarantees zero Git diff churn on idempotent runs.
    """
    return _build_index_lines_from_conn(vault, cache.get_connection())


def sync_vault_index(vault: Vault, cache: VaultCache) -> bool:
    """Regenerate index.md under advisory lock only if bytes have changed.

    Returns True if file was rewritten, False if unchanged.
    """
    new_content = generate_index_markdown(vault, cache=cache)
    index_path = vault.index_path

    with advisory_lock(vault.lock_path):
        if index_path.exists():
            existing = index_path.read_text("utf-8")
            if existing == new_content:
                return False

        atomic_write(index_path, new_content)
        return True

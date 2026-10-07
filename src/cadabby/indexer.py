"""index.md gap report generation, log appending, and ledger rotation.

Conforms to Cadabby Technical Specification §2.5, §4.3, §8.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cadabby.constants import TYPE_MOC
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
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    prefix = f"[{now_iso}]"
    if actor:
        prefix += f" {actor}:"
    entry = f"- {prefix} {message}"
    append_ledger(vault.log_path, entry)


def rotate_vault_log(vault: Vault) -> Path | None:
    """Rotate log.md into log/<year>.md if file exceeds log_rotate_bytes or crosses a calendar year (§8)."""
    log_file = vault.log_path
    if not log_file.exists():
        return None

    max_bytes = vault.config.get("log_rotate_bytes", 262144)
    st = log_file.stat()
    current_year = datetime.now(UTC).strftime("%Y")
    initial_content = log_file.read_text("utf-8")
    ledger_year = _detect_ledger_year(initial_content, current_year)

    if st.st_size < max_bytes and ledger_year == current_year:
        return None

    log_dir = vault.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    target_rotated = log_dir / f"{ledger_year}.md"

    with advisory_lock(vault.lock_path):
        # Append existing log content into archive
        content = log_file.read_text("utf-8")
        if target_rotated.exists():
            header_prefix = f"# Activity Ledger ({ledger_year})"
            body_content = content
            if body_content.startswith(header_prefix):
                body_content = body_content[len(header_prefix) :].lstrip("\r\n")
            if body_content:
                append_ledger(target_rotated, body_content)
        else:
            atomic_write(target_rotated, content)

        # Fresh active log
        fresh_header = f"# Activity Ledger ({current_year})\n\n"
        atomic_write(log_file, fresh_header)

    return target_rotated


def _cell(value: Any, fallback: str = "") -> str:
    """Escape a value for a markdown table cell."""
    return (value or fallback).replace("|", "\\|")


def _entry_points(conn: Any) -> list[str]:
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


def _unfiled(conn: Any) -> list[str]:
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


def _raw_queue(conn: Any) -> list[str]:
    """Section 3: the ingestion queue. Vault-wide -- any domain may cite raw/."""
    rows = conn.execute(
        """
        SELECT r.rel_path,
               (SELECT COUNT(*) FROM sources s WHERE s.raw_path = r.rel_path) AS citation_count
        FROM notes r
        WHERE r.layer = 'raw'
        ORDER BY r.rel_path ASC;
        """
    ).fetchall()
    pending = [r for r in rows if not r["citation_count"]]
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
    lines += [f"| `{r['rel_path']}` |" for r in pending]
    return lines + [""]


def _verification_debt(conn: Any) -> list[str]:
    """Section 4: notes whose verification entries have all gone stale (§3.4).

    Vault-wide: any note in any domain can carry verification entries.
    """
    rows = conn.execute(
        """
        SELECT cid, stem, trust_tier FROM notes
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


def _build_index_lines_from_conn(vault: Vault, conn: Any) -> str:
    """Render the vault-wide gap report (§2.5).

    Deliberately not a catalog. A table of every wiki note duplicates what MOCs
    curate, what a file explorer shows for a flat folder, and what search ranks
    better -- while churning the Git diff on every note added. Each section
    below is omitted when empty, so a fully filed, fully ingested, fully
    verified vault produces a header and nothing else. Shrinking this file is
    the objective: every row is work someone still has to do.
    """
    lines = [
        f"# {vault.config.get('vault_name', 'Wiki')} Index",
        "",
        "> Auto-generated gap report (§2.5). Not a catalog, and never hand-edited:",
        "> edits are overwritten on the next scan that finds changes.",
        "",
    ]

    sections = (
        _entry_points(conn)
        + _unfiled(conn)
        + _raw_queue(conn)
        + _verification_debt(conn)
    )
    if not sections:
        lines.append("Nothing outstanding: every note is filed, every source processed, every attestation current.")
    lines += sections

    return "\n".join(lines).strip() + "\n"


def generate_index_markdown(vault: Vault, cache: Any | None = None) -> str:
    """Render the gap report for index.md (§2.5).

    Deterministic sort order guarantees zero Git diff churn on idempotent runs.
    """
    if cache is not None:
        return _build_index_lines_from_conn(vault, cache.get_connection())

    from cadabby.cache import VaultCache

    with VaultCache(vault) as new_cache:
        new_cache.scan(regenerate_index=False)
        return _build_index_lines_from_conn(vault, new_cache.get_connection())


def sync_vault_index(vault: Vault, cache: Any | None = None) -> bool:
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

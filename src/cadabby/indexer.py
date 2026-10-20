"""index.md gap report generation, log appending, and ledger rotation.

Conforms to Cadabby Technical Specification §2.5, §4.3, §8.
"""

from __future__ import annotations

import os
import re
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

from cadabby.constants import DIR_WIKI, LEDGER_TITLE
from cadabby.fsutil import advisory_lock, append_ledger, atomic_write
from cadabby.okf import ledger_line, utc_now_iso
from cadabby.ports import IndexCachePort
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
    append_ledger(vault.contained_path(vault.log_path), ledger_line(message, actor))


def rotate_vault_log(vault: Vault) -> Path | None:
    """Rotate log.md into log/<year>.md if it exceeds log_rotate_bytes or crosses a calendar year (§8).

    The size, the year and the decision are all read under the lock: decided
    outside it, two concurrent syncs could both choose to rotate and archive
    the same ledger twice.

    Appends take no lock (§8), so the live ledger is moved aside before it is
    archived. Reading it and then writing a fresh header over it lost any
    entry appended in between; after the rename, a new append creates a new
    log.md instead, and nothing overwrites it.
    """
    log_file = vault.contained_path(vault.log_path)
    max_bytes = vault.config["log_rotate_bytes"]

    with advisory_lock(vault.contained_path(vault.lock_path)):
        if not log_file.exists():
            return None
        content = log_file.read_text("utf-8")
        current_year = utc_now_iso()[:4]
        ledger_year = _detect_ledger_year(content, current_year)
        if len(content.encode("utf-8")) < max_bytes and ledger_year == current_year:
            return None

        target_rotated = vault.contained_path(vault.log_dir / f"{ledger_year}.md")
        target_rotated.parent.mkdir(parents=True, exist_ok=True)
        pending = vault.contained_path(vault.log_dir / f".rotating-{uuid.uuid4().hex}.md")
        os.replace(log_file, pending)
        try:
            content = pending.read_text("utf-8")
            _archive_ledger(target_rotated, content, ledger_year)
            # A writer that opened log.md before the rename appends to `pending`;
            # one more read catches what it wrote while we archived.
            tail = pending.read_text("utf-8")[len(content):]
            if tail:
                append_ledger(target_rotated, tail)
        except BaseException:
            # Never leave the whole ledger in a hidden `.rotating-*` file: put it
            # back unless an append has already started a new log.md.
            if not log_file.exists():
                os.replace(pending, log_file)
            raise
        pending.unlink()

        # O_EXCL: if an append already created the new ledger, keep its entries
        # and go without the heading (year detection falls back to the first
        # entry's timestamp) rather than overwrite them.
        try:
            fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(f"{LEDGER_TITLE} ({current_year})\n\n")

    return target_rotated


def _archive_ledger(target_rotated: Path, content: str, ledger_year: str) -> None:
    """Write `content` to the year's archive, appending (minus its heading) if one exists."""
    if target_rotated.exists():
        header_prefix = f"{LEDGER_TITLE} ({ledger_year})"
        body_content = content.removeprefix(header_prefix).lstrip("\r\n") if content.startswith(
            header_prefix
        ) else content
        if body_content:
            append_ledger(target_rotated, body_content)
    else:
        atomic_write(target_rotated, content)


def _cell(value: Any, fallback: str = "") -> str:
    """Escape a value for a markdown table cell."""
    return str(value or fallback).replace("\n", " ").replace("|", "\\|")


# Characters a wikilink target cannot carry: `#` and `^` open an anchor, `|` an
# alias, and `]` closes the link -- `[[C#]]` would point at note `C`.
_WIKILINK_UNSAFE = frozenset("#|]^[")


def _note_link(stem: str, rel_path: str) -> str:
    """A table-cell link to a note: `[[stem]]`, or a Markdown link when the stem cannot be a wikilink."""
    if not _WIKILINK_UNSAFE.intersection(stem):
        return f"[[{stem}]]"
    label = stem.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]").replace("|", "\\|")
    return f"[{label}]({urllib.parse.quote(rel_path)})"


def _entry_points(cache: IndexCachePort) -> list[str]:
    """Section 1: every MOC, linked. The human's starting page (§2.5).

    A wiki MOC is linked by its bare stem, as before. A domain MOC is linked by
    its full CID (`[[services/services-moc]]`): a bare stem opens a non-wiki
    note only until another note shares the name.
    """
    rows = cache.entry_point_rows()
    if not rows:
        return []
    lines = ["## Entry Points", "", "| Map of Content | Description |", "| :--- | :--- |"]
    lines += [
        f"| {_note_link(r['stem'] if r['layer'] == DIR_WIKI else r['cid'], r['rel_path'])} | {_cell(r['description'])} |"
        for r in rows
    ]
    return lines + [""]


def _unfiled(cache: IndexCachePort) -> list[str]:
    """Section 2: wiki notes no MOC reaches by forward link at any depth (§2.5).

    The one failure mode flat layout introduces, and the one `lint` cannot
    catch: a note that links outward has edges, so gate 5 sees no orphan.

    Reachability starts at every MOC outside archive/ (a domain's `type: moc`
    counts too), is transitive and may travel through any layer -- a link is a
    link, and a wiki note pulled in via a domain note is still filed. Only wiki
    notes are *reported*: the flat namespace is the one place a note has no
    folder to be found by; domains carry their own topology in folders (§2.5). `deprecated` notes are excluded: the
    section is a worklist, and filing a retired note is not work. Without that
    exclusion the report has a floor it can never reach zero from, which would
    defeat the point of shrinking it. The definition lives in
    `cache.unfiled_note_rows`, shared with the `vault_status` queue.
    """
    rows = cache.unfiled_note_rows()
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
    lines += [f"| {_note_link(r['stem'], r['rel_path'])} | {_cell(r['description'])} |" for r in rows]
    return lines + [""]


def _raw_queue(cache: IndexCachePort) -> list[str]:
    """Section 3: the ingestion queue. Vault-wide -- any domain may cite raw/."""
    pending = cache.unprocessed_raw_paths()
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


def _verification_debt(cache: IndexCachePort) -> list[str]:
    """Section 4: notes whose verification entries have all gone stale (§3.4).

    Vault-wide across active domains: retired notes (`archive/`, `deprecated`,
    or `abandoned`) are excluded because they are no longer maintained. The
    definition lives in `cache.verification_debt_rows`, shared with the
    `vault_status` queue.
    """
    rows = cache.verification_debt_rows()
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
    lines += [f"| `{r.cid}` |" for r in rows]
    return lines + [""]


def _build_index_lines(vault: Vault, cache: IndexCachePort) -> str:
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

    gaps = _unfiled(cache) + _raw_queue(cache) + _verification_debt(cache)
    lines += _entry_points(cache)
    if not gaps:
        lines.append("Nothing outstanding: every note is filed, every source processed, every attestation current.")
    lines += gaps

    return "\n".join(lines).strip() + "\n"


def generate_index_markdown(vault: Vault, cache: IndexCachePort) -> str:
    """Render the gap report for index.md (§2.5) from an already-scanned cache.

    Deterministic sort order guarantees zero Git diff churn on idempotent runs.
    """
    return _build_index_lines(vault, cache)


def sync_vault_index(vault: Vault, cache: IndexCachePort) -> bool:
    """Regenerate index.md under advisory lock only if bytes have changed.

    Returns True if file was rewritten, False if unchanged.
    """
    index_path = vault.contained_path(vault.index_path)

    # Generated under the lock: built outside it, a slower writer could replace
    # a newer report with the older snapshot it read first.
    with advisory_lock(vault.contained_path(vault.lock_path)):
        new_content = generate_index_markdown(vault, cache=cache)
        # Bytes: index.md is engine-owned, and a stray non-UTF-8 byte in it made
        # the strict decode fail every write *after* the note was committed.
        if index_path.exists() and index_path.read_bytes() == new_content.encode("utf-8"):
            return False

        atomic_write(index_path, new_content)
        return True

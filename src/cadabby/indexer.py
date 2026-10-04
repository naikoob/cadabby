"""Index catalog generation, log appending, and ledger rotation.

Conforms to Cadabby Technical Specification §2.1, §6.1, §8.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from cadabby.constants import DIR_LOG, DIR_WIKI, NOTE_TYPES
from cadabby.fsutil import advisory_lock, append_ledger, atomic_write
from cadabby.vault import Vault


def append_vault_log(vault: Vault, message: str, actor: str | None = None) -> None:
    """Append a timestamped entry to the active log.md ledger."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    prefix = f"[{now_iso}]"
    if actor:
        prefix += f" {actor}:"
    entry = f"- {prefix} {message}"
    append_ledger(vault.log_path, entry)


def rotate_vault_log(vault: Vault) -> Path | None:
    """Rotate log.md into log/<year>.md if file exceeds log_rotate_bytes."""
    log_file = vault.log_path
    if not log_file.exists():
        return None

    max_bytes = vault.config.get("log_rotate_bytes", 262144)
    st = log_file.stat()
    if st.st_size < max_bytes:
        return None

    year = datetime.now(timezone.utc).strftime("%Y")
    log_dir = vault.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    target_rotated = log_dir / f"{year}.md"

    with advisory_lock(vault.lock_path):
        # Append existing log content into archive
        content = log_file.read_text("utf-8")
        if target_rotated.exists():
            append_ledger(target_rotated, content)
        else:
            atomic_write(target_rotated, content)

        # Fresh active log
        fresh_header = f"# Activity Ledger ({year})\n\n"
        atomic_write(log_file, fresh_header)

    return target_rotated


def generate_index_markdown(vault: Vault) -> str:
    """Generate deterministic catalog markdown for index.md.

    Deterministic sort order guarantees zero Git diff churn on idempotent runs.
    """
    from cadabby.cache import VaultCache

    with VaultCache(vault) as cache:
        cache.scan()
        conn = cache.get_connection()

        lines = [
            f"# {vault.config.get('vault_name', 'Wiki')} Catalog",
            "",
            "> Auto-generated catalog linking compiled wiki knowledge and raw ground truth sources.",
            "",
        ]

        # 1. Wiki notes grouped by type
        type_plural_map = {
            "entity": "Entities",
            "concept": "Concepts",
            "synthesis": "Syntheses",
            "comparison": "Comparisons",
            "guide": "Guides",
        }

        for n_type in NOTE_TYPES:
            heading = type_plural_map.get(n_type, n_type.capitalize())
            cur = conn.execute(
                """
                SELECT cid, stem, title, description, trust_tier, status
                FROM notes
                WHERE layer = 'wiki' AND type = ? AND parse_error IS NULL
                ORDER BY stem ASC;
                """,
                (n_type,),
            )
            rows = cur.fetchall()
            if rows:
                lines.append(f"## {heading}")
                lines.append("")
                lines.append("| Note | Description | Trust | Status |")
                lines.append("| :--- | :--- | :--- | :--- |")
                for r in rows:
                    desc = (r["description"] or "").replace("|", "\\|")
                    tier = r["trust_tier"] or "unverified"
                    status = r["status"] or "active"
                    lines.append(f"| [[{r['stem']}]] | {desc} | `{tier}` | `{status}` |")
                lines.append("")

        # 2. Raw sources tracking
        cur = conn.execute(
            """
            SELECT r.rel_path, r.stem,
                   (SELECT COUNT(*) FROM sources s WHERE s.raw_path = r.rel_path) AS citation_count
            FROM notes r
            WHERE r.layer = 'raw'
            ORDER BY r.rel_path ASC;
            """
        )
        raw_rows = cur.fetchall()
        if raw_rows:
            lines.append("## Raw Sources")
            lines.append("")
            lines.append("| Source File | Status | Citations |")
            lines.append("| :--- | :--- | :--- |")
            for r in raw_rows:
                citations = r["citation_count"]
                status_badge = "Processed" if citations > 0 else "**Unprocessed**"
                lines.append(f"| `{r['rel_path']}` | {status_badge} | {citations} |")
            lines.append("")

        return "\n".join(lines).strip() + "\n"


def sync_vault_index(vault: Vault) -> bool:
    """Regenerate index.md under advisory lock only if bytes have changed.

    Returns True if file was rewritten, False if unchanged.
    """
    new_content = generate_index_markdown(vault)
    index_path = vault.index_path

    with advisory_lock(vault.lock_path):
        if index_path.exists():
            existing = index_path.read_text("utf-8")
            if existing == new_content:
                return False

        atomic_write(index_path, new_content)
        return True

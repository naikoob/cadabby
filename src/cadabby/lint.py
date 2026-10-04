"""Deterministic epistemic linting executing the six normative gates.

Conforms strictly to Cadabby Technical Specification §6.3.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from cadabby.cache import VaultCache
from cadabby.constants import (
    NOTE_STATUSES,
    NOTE_TYPES,
    REQUIRED_FRONTMATTER_FIELDS,
)
from cadabby.frontmatter import FrontmatterParseError, parse_frontmatter
from cadabby.okf import (
    compute_body_hash,
    is_valid_actor,
    is_valid_timestamp,
)
from cadabby.ops import TYPE_TO_DIR
from cadabby.vault import Vault, cid_to_path, path_to_cid


@dataclass
class LintFinding:
    code: str
    severity: str  # "error" | "warning"
    rel_path: str
    line: int | None
    message: str


def run_vault_lint(vault: Vault, cache: VaultCache | None = None) -> list[LintFinding]:
    """Execute the six normative lint gates against the vault.

    1. Schema Integrity
    2. Layout Consistency
    3. Link Consistency
    4. Provenance Check
    5. Graph Connectivity
    6. Verification Integrity
    """
    # Ensure cache is fresh
    active_cache = cache if cache is not None else VaultCache(vault)
    active_cache.scan()
    conn = active_cache.get_connection()

    findings: list[LintFinding] = []

    # Map of CID to note details for graph and anchor checks
    cur = conn.execute("SELECT cid, rel_path, body FROM notes WHERE layer = 'wiki';")
    notes_by_cid = {r["cid"]: dict(r) for r in cur.fetchall()}

    # Scan all markdown files in wiki/
    wiki_files = sorted(vault.wiki_dir.rglob("*.md")) if vault.wiki_dir.exists() else []

    for file_path in wiki_files:
        rel_path = vault.rel_path(file_path)
        cid = path_to_cid(rel_path)
        content = file_path.read_text("utf-8", errors="replace")

        # --- GATE 1: Schema Integrity ---
        try:
            fm, body = parse_frontmatter(content)
        except FrontmatterParseError as e:
            findings.append(
                LintFinding(
                    code="FRONTMATTER_UNPARSEABLE",
                    severity="error",
                    rel_path=rel_path,
                    line=e.line_number,
                    message=e.message,
                )
            )
            continue

        if not fm:
            findings.append(
                LintFinding(
                    code="FRONTMATTER_UNPARSEABLE",
                    severity="error",
                    rel_path=rel_path,
                    line=1,
                    message="Missing frontmatter block",
                )
            )
            continue

        # Required fields
        for field_name in REQUIRED_FRONTMATTER_FIELDS:
            if field_name not in fm or fm[field_name] is None:
                findings.append(
                    LintFinding(
                        code="FIELD_MISSING",
                        severity="error",
                        rel_path=rel_path,
                        line=None,
                        message=f"Missing required field '{field_name}'",
                    )
                )

        note_type = fm.get("type")
        if note_type and note_type not in NOTE_TYPES:
            findings.append(
                LintFinding(
                    code="ENUM_INVALID",
                    severity="error",
                    rel_path=rel_path,
                    line=None,
                    message=f"Invalid type '{note_type}'. Must be one of {NOTE_TYPES}",
                )
            )

        note_status = fm.get("status")
        if note_status and note_status not in NOTE_STATUSES:
            findings.append(
                LintFinding(
                    code="ENUM_INVALID",
                    severity="error",
                    rel_path=rel_path,
                    line=None,
                    message=f"Invalid status '{note_status}'. Must be one of {NOTE_STATUSES}",
                )
            )

        # Timestamps check
        gen_block = fm.get("generated")
        if isinstance(gen_block, dict):
            gen_at = gen_block.get("at")
            if gen_at and not is_valid_timestamp(gen_at):
                findings.append(
                    LintFinding(
                        code="TIMESTAMP_INVALID",
                        severity="error",
                        rel_path=rel_path,
                        line=None,
                        message=f"Invalid RFC 3339 timestamp in generated.at: '{gen_at}'",
                    )
                )

        verified_entries = fm.get("verified", [])
        if isinstance(verified_entries, list):
            for v_idx, v_entry in enumerate(verified_entries):
                if isinstance(v_entry, dict):
                    v_at = v_entry.get("at")
                    if v_at and not is_valid_timestamp(v_at):
                        findings.append(
                            LintFinding(
                                code="TIMESTAMP_INVALID",
                                severity="error",
                                rel_path=rel_path,
                                line=None,
                                message=f"Invalid RFC 3339 timestamp in verified[{v_idx}].at: '{v_at}'",
                            )
                        )

        # --- GATE 2: Layout Consistency ---
        if note_type in TYPE_TO_DIR:
            expected_parent = TYPE_TO_DIR[note_type]
            actual_parent = file_path.parent.name
            if actual_parent != expected_parent:
                findings.append(
                    LintFinding(
                        code="TYPE_DIR_MISMATCH",
                        severity="error",
                        rel_path=rel_path,
                        line=None,
                        message=f"Note of type '{note_type}' is in 'wiki/{actual_parent}/', expected 'wiki/{expected_parent}/'",
                    )
                )

        # --- GATE 6: Verification Integrity ---
        current_body_hash = compute_body_hash(body)
        if isinstance(verified_entries, list):
            for v_entry in verified_entries:
                if not isinstance(v_entry, dict):
                    continue
                actor = v_entry.get("by")
                of_hash = v_entry.get("of")

                if not actor or not is_valid_actor(actor):
                    findings.append(
                        LintFinding(
                            code="ACTOR_MALFORMED",
                            severity="error",
                            rel_path=rel_path,
                            line=None,
                            message=f"Malformed actor identity: '{actor}'",
                        )
                    )

                if not of_hash:
                    findings.append(
                        LintFinding(
                            code="VERIFICATION_UNBOUND",
                            severity="error",
                            rel_path=rel_path,
                            line=None,
                            message=f"Verification entry by '{actor}' missing 'of:' body hash binding",
                        )
                    )
                elif of_hash != current_body_hash:
                    findings.append(
                        LintFinding(
                            code="VERIFICATION_STALE",
                            severity="warning",
                            rel_path=rel_path,
                            line=None,
                            message=f"Verification entry by '{actor}' is stale (hash drifted from {of_hash[:16]}...)",
                        )
                    )

    # --- GATE 3: Link Consistency ---
    cur = conn.execute(
        """
        SELECT source_cid, target_raw, target_cid, anchor
        FROM links;
        """
    )
    for link_row in cur.fetchall():
        source_cid = link_row["source_cid"]
        target_raw = link_row["target_raw"]
        target_cid = link_row["target_cid"]
        anchor = link_row["anchor"]
        rel_path = cid_to_path(source_cid)

        if target_cid is None:
            findings.append(
                LintFinding(
                    code="LINK_DEAD",
                    severity="error",
                    rel_path=rel_path,
                    line=None,
                    message=f"Dead wikilink [[{target_raw}]]",
                )
            )
        elif anchor:
            # Check anchor against headings in target note
            target_note = notes_by_cid.get(target_cid)
            if target_note:
                target_body = target_note["body"] or ""
                # Search for # Anchor or ## Anchor etc.
                anchor_slug = anchor.lower().replace("-", " ")
                headings = re.findall(r"^#{1,6}\s+(.+)$", target_body, flags=re.MULTILINE)
                normalized_headings = [h.strip().lower() for h in headings]

                if not any(anchor.lower() == h or anchor_slug == h for h in normalized_headings):
                    findings.append(
                        LintFinding(
                            code="ANCHOR_MISSING",
                            severity="warning",
                            rel_path=rel_path,
                            line=None,
                            message=f"Anchor '#{anchor}' not found in target [[{target_cid}]]",
                        )
                    )

    # --- GATE 4: Provenance Check ---
    cur = conn.execute(
        """
        SELECT source_cid, raw_path, resolved
        FROM sources
        WHERE resolved = 0;
        """
    )
    for src_row in cur.fetchall():
        rel_path = cid_to_path(src_row["source_cid"])
        findings.append(
            LintFinding(
                code="SOURCE_MISSING",
                severity="error",
                rel_path=rel_path,
                line=None,
                message=f"Referenced source does not exist: '{src_row['raw_path']}'",
            )
        )

    # --- GATE 5: Graph Connectivity ---
    for cid, note_info in notes_by_cid.items():
        rel_path = note_info["rel_path"]

        cur = conn.execute("SELECT COUNT(*) FROM links WHERE source_cid = ?;", (cid,))
        outbound = cur.fetchone()[0]

        cur = conn.execute("SELECT COUNT(*) FROM links WHERE target_cid = ?;", (cid,))
        inbound = cur.fetchone()[0]

        if outbound == 0 and inbound == 0:
            findings.append(
                LintFinding(
                    code="NOTE_ORPHAN",
                    severity="warning",
                    rel_path=rel_path,
                    line=None,
                    message="Orphan note: 0 inbound and 0 outbound links",
                )
            )

    return findings

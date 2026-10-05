"""Deterministic epistemic linting executing the six normative gates.

Conforms strictly to Cadabby Technical Specification §6.3.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cadabby.cache import VaultCache
from cadabby.constants import (
    DEFAULT_IGNORED_DIRS,
    DIR_WIKI,
    FILE_AGENTS,
    NOTE_STATUSES,
    REQUIRED_FRONTMATTER_FIELDS,
)
from cadabby.domain import DomainDefinition
from cadabby.frontmatter import FrontmatterParseError, parse_frontmatter
from cadabby.okf import (
    compute_body_hash,
    is_canonical_tag,
    is_valid_actor,
    is_valid_timestamp,
    tag_has_whitespace,
)
from cadabby.vault import Vault, cid_to_path


@dataclass
class LintFinding:
    code: str
    severity: str  # "error" | "warning"
    rel_path: str
    line: int | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        """Convert LintFinding to serializable dictionary."""
        return {
            "code": self.code,
            "severity": self.severity,
            "rel_path": self.rel_path,
            "line": self.line,
            "message": self.message,
        }


def run_vault_lint(vault: Vault, cache: VaultCache | None = None) -> list[LintFinding]:
    """Execute the six normative lint gates against the vault.

    1. Schema Integrity
    2. Layout Consistency
    3. Link Consistency
    4. Provenance Check
    5. Graph Connectivity
    6. Verification Integrity
    """
    if cache is not None:
        return _run_vault_lint_impl(vault, cache)
    with VaultCache(vault) as local_cache:
        return _run_vault_lint_impl(vault, local_cache)


def _run_vault_lint_impl(vault: Vault, cache: VaultCache) -> list[LintFinding]:
    # Ensure cache is fresh
    cache.scan()
    conn = cache.get_connection()

    findings: list[LintFinding] = []

    # Map of CID to note details for graph and anchor checks
    cur = conn.execute("SELECT cid, rel_path, body FROM notes WHERE layer != 'raw';")
    notes_by_cid = {r["cid"]: dict(r) for r in cur.fetchall()}

    domains = vault.discover_domains()

    # Scan all markdown files across all discovered cognitive domains
    all_note_files: list[tuple[Path, str, DomainDefinition]] = []
    for domain_name, domain_def in domains.items():
        if domain_def.path.exists():
            for root, dirs, files in os.walk(domain_def.path, topdown=True):
                dirs[:] = [d for d in dirs if d not in DEFAULT_IGNORED_DIRS and not d.startswith(".")]
                for f in sorted(files):
                    if f.endswith(".md") and not f.startswith("."):
                        if f == FILE_AGENTS:
                            continue
                        all_note_files.append((Path(root) / f, domain_name, domain_def))

    for file_path, domain_name, domain_def in all_note_files:
        rel_path = vault.rel_path(file_path)
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
        if note_type:
            if domain_def.allowed_types is not None:
                if note_type not in domain_def.allowed_types:
                    findings.append(
                        LintFinding(
                            code="ENUM_INVALID",
                            severity="error",
                            rel_path=rel_path,
                            line=None,
                            message=f"Invalid type '{note_type}'. Must be one of {domain_def.allowed_types}",
                        )
                    )
            elif not isinstance(note_type, str) or not note_type.strip():
                findings.append(
                    LintFinding(
                        code="ENUM_INVALID",
                        severity="error",
                        rel_path=rel_path,
                        line=None,
                        message=f"Invalid type '{note_type}'. Type must be a non-empty string",
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

        # Tag grammar (§3.1). Whitespace is an error because it corrupts the
        # space-joined `--tag` facet (§4.2); a merely non-canonical tag is a
        # warning because the canonicalizing writer repairs it (§3.2).
        note_tags = fm.get("tags")
        if note_tags is not None and not isinstance(note_tags, list):
            findings.append(
                LintFinding(
                    code="TAG_MALFORMED",
                    severity="error",
                    rel_path=rel_path,
                    line=None,
                    message=f"'tags' must be a sequence of strings, got {type(note_tags).__name__}",
                )
            )
        elif isinstance(note_tags, list):
            for tag in note_tags:
                if not isinstance(tag, str):
                    findings.append(
                        LintFinding(
                            code="TAG_MALFORMED",
                            severity="error",
                            rel_path=rel_path,
                            line=None,
                            message=f"Invalid tag {tag!r}. Tags must be strings",
                        )
                    )
                elif tag_has_whitespace(tag):
                    findings.append(
                        LintFinding(
                            code="TAG_MALFORMED",
                            severity="error",
                            rel_path=rel_path,
                            line=None,
                            message=(
                                f"Invalid tag '{tag}'. Tags must not contain whitespace, "
                                "which breaks --tag filtering; use kebab-case"
                            ),
                        )
                    )
                elif not is_canonical_tag(tag):
                    findings.append(
                        LintFinding(
                            code="TAG_MALFORMED",
                            severity="warning",
                            rel_path=rel_path,
                            line=None,
                            message=(
                                f"Non-canonical tag '{tag}'. Canonical form is lowercase "
                                "kebab-case, optionally '/'-nested (e.g. 'trust/human-reviewed')"
                            ),
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

        # --- GATE 2: Layout Consistency (Flat Wiki Enforced) ---
        if domain_name == DIR_WIKI:
            actual_parent = file_path.parent.name
            if actual_parent != DIR_WIKI:
                findings.append(
                    LintFinding(
                        code="WIKI_NESTING_DISALLOWED",
                        severity="error",
                        rel_path=rel_path,
                        line=None,
                        message=f"Note is nested in 'wiki/{actual_parent}/'. The wiki domain must be completely flat.",
                    )
                )

        # --- GATE 4 (Domain Provenance Requirement) ---
        if domain_def.require_sources:
            sources = fm.get("sources")
            if not sources or not isinstance(sources, list) or len(sources) == 0:
                findings.append(
                    LintFinding(
                        code="SOURCE_MISSING",
                        severity="error",
                        rel_path=rel_path,
                        line=None,
                        message=f"Domain '{domain_name}' requires 'sources:' provenance list",
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
    cur = conn.execute(
        """
        SELECT n.rel_path
        FROM notes n
        WHERE n.layer = 'wiki'
          AND NOT EXISTS (SELECT 1 FROM links l1 WHERE l1.source_cid = n.cid)
          AND NOT EXISTS (SELECT 1 FROM links l2 WHERE l2.target_cid = n.cid)
        ORDER BY n.rel_path;
        """
    )
    for row in cur.fetchall():
        findings.append(
            LintFinding(
                code="NOTE_ORPHAN",
                severity="warning",
                rel_path=row["rel_path"],
                line=None,
                message="Orphan note: 0 inbound and 0 outbound links",
            )
        )

    return findings

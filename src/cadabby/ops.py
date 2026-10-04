"""High-level atomic vault operations: scaffold, update, verify, and ground.

Conforms strictly to Cadabby Technical Specification §3, §5.1, §6, §8.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from cadabby.cache import VaultCache
from cadabby.constants import NOTE_TYPES
from cadabby.frontmatter import parse_frontmatter, serialize_frontmatter
from cadabby.fsutil import atomic_replace_checked, atomic_write, compute_file_sha256
from cadabby.indexer import append_vault_log
from cadabby.okf import compute_body_hash, derive_trust_tier, is_valid_actor
from cadabby.vault import Vault, cid_to_path, path_to_cid

TYPE_TO_DIR = {
    "entity": "entities",
    "concept": "concepts",
    "synthesis": "syntheses",
    "comparison": "comparisons",
    "guide": "guides",
}


def sanitize_filename(title: str) -> str:
    """Convert a human-readable title into a safe filename stem."""
    # Replace whitespace and invalid characters with dashes
    slug = re.sub(r"[^\w\s-]", "", title).strip()
    slug = re.sub(r"[-\s]+", "-", slug)
    return slug or "untitled"


def scaffold_note(
    vault: Vault,
    title: str,
    type_: str,
    description: str,
    tags: list[str] | None = None,
    sources: list[str] | None = None,
    body: str = "",
    actor: str = "agent:unknown",
) -> Path:
    """Scaffold a new wiki note in the directory matching its type.

    Refuses to overwrite if note already exists.
    """
    if type_ not in NOTE_TYPES:
        raise ValueError(f"Invalid note type '{type_}'. Must be one of {NOTE_TYPES}")

    sub_dir = TYPE_TO_DIR[type_]
    stem = sanitize_filename(title)
    target_path = vault.wiki_dir / sub_dir / f"{stem}.md"

    if target_path.exists():
        raise FileExistsError(f"Note already exists at {vault.rel_path(target_path)}")

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    fm_data = {
        "type": type_,
        "title": title,
        "description": description,
        "status": "draft",
        "tags": tags or [],
        "sources": sources or [],
        "verified": [],
        "generated": {
            "by": actor,
            "at": now_iso,
        },
    }

    initial_body = body.strip() or f"# {title}\n\n"
    content = serialize_frontmatter(fm_data, initial_body)

    atomic_write(target_path, content)
    append_vault_log(vault, f"Scaffolded {vault.rel_path(target_path)}", actor=actor)

    return target_path


def update_note(
    vault: Vault,
    cid_or_path: str,
    frontmatter_patch: dict[str, Any] | None = None,
    append_section: tuple[str, str] | None = None,
    replace_section: tuple[str, str] | None = None,
    expected_hash: str | None = None,
    actor: str = "agent:unknown",
) -> Path:
    """Non-destructively update note frontmatter or content sections.

    Supports read-modify-write safety via expected_hash.
    """
    rel_path = cid_to_path(cid_or_path)
    target_path = vault.abs_path(rel_path)

    if not target_path.exists():
        raise FileNotFoundError(f"Note not found: {rel_path}")

    current_raw = target_path.read_text("utf-8")
    fm, body = parse_frontmatter(current_raw)

    # 1. Apply frontmatter patches
    if frontmatter_patch:
        for k, v in frontmatter_patch.items():
            if v is None:
                fm.pop(k, None)
            else:
                fm[k] = v

    # 2. Section modifications
    updated_body = body

    if replace_section:
        heading, new_section_content = replace_section
        # Regex matching ## Heading up to the next heading or EOF
        pattern = rf"(^##\s+{re.escape(heading)}\s*$\n?)([\s\S]*?)(?=^##\s|\Z)"
        replacement = f"## {heading}\n\n{new_section_content.strip()}\n\n"
        if re.search(pattern, updated_body, flags=re.MULTILINE):
            updated_body = re.sub(pattern, replacement, updated_body, flags=re.MULTILINE)
        else:
            # Heading not found; append section
            updated_body = updated_body.rstrip() + f"\n\n## {heading}\n\n{new_section_content.strip()}\n"

    if append_section:
        heading, section_content = append_section
        updated_body = updated_body.rstrip() + f"\n\n## {heading}\n\n{section_content.strip()}\n"

    new_content = serialize_frontmatter(fm, updated_body)
    atomic_replace_checked(target_path, new_content, expected_hash=expected_hash)
    append_vault_log(vault, f"Updated {rel_path}", actor=actor)

    return target_path


def verify_note(
    vault: Vault,
    cid_or_path: str,
    actor: str,
    method: str = "manual-review",
    is_human_authorized: bool = False,
) -> dict[str, Any]:
    """Appends a content-bound verification attestation to a note.

    Refuses human:* attestations over MCP without explicit human authorization.
    """
    if actor.startswith("human:") and not is_human_authorized:
        raise PermissionError(
            f"Cannot stamp '{actor}' verification over MCP. Human verification requires interactive CLI."
        )

    if not is_valid_actor(actor):
        raise ValueError(f"Invalid actor format '{actor}'. Must match ^(human|agent|process):[A-Za-z0-9._\\-/]+$")

    rel_path = cid_to_path(cid_or_path)
    target_path = vault.abs_path(rel_path)

    if not target_path.exists():
        raise FileNotFoundError(f"Note not found: {rel_path}")

    current_raw = target_path.read_text("utf-8")
    fm, body = parse_frontmatter(current_raw)

    b_hash = compute_body_hash(body)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    verified_list = fm.get("verified")
    if not isinstance(verified_list, list):
        verified_list = []
        fm["verified"] = verified_list

    # Deduplicate exact matching attestation
    for entry in verified_list:
        if isinstance(entry, dict) and entry.get("by") == actor and entry.get("of") == b_hash:
            return {
                "cid": path_to_cid(rel_path),
                "actor": actor,
                "of": b_hash,
                "trust_tier": derive_trust_tier(verified_list, b_hash),
                "already_verified": True,
            }

    attestation = {
        "by": actor,
        "at": now_iso,
        "method": method,
        "of": b_hash,
    }
    verified_list.append(attestation)

    new_content = serialize_frontmatter(fm, body)
    atomic_write(target_path, new_content)

    new_tier = derive_trust_tier(verified_list, b_hash)
    append_vault_log(vault, f"Verified {rel_path} ({actor}) -> {new_tier}", actor=actor)

    return {
        "cid": path_to_cid(rel_path),
        "actor": actor,
        "of": b_hash,
        "trust_tier": new_tier,
        "already_verified": False,
    }


def ground_notes(
    vault: Vault,
    cids: Sequence[str],
    budget_tokens: int | None = None,
) -> list[dict[str, Any]]:
    """Retrieve full content and 1-hop graph neighborhood for the specified CIDs.

    Applies token budgeting heuristic (len // 4) truncating at clean section boundaries.
    """
    cache = VaultCache(vault)
    cache.scan()
    conn = cache.get_connection()

    grounded = []

    for raw_cid in cids:
        cid = path_to_cid(raw_cid)
        rel_path = cid_to_path(cid)
        abs_path = vault.abs_path(rel_path)

        if not abs_path.exists():
            continue

        text = abs_path.read_text("utf-8", errors="replace")
        fm, body = parse_frontmatter(text)

        # 1-hop forward links
        cur = conn.execute(
            "SELECT target_raw, target_cid FROM links WHERE source_cid = ?;",
            (cid,),
        )
        links = [{"target": r["target_raw"], "resolved_cid": r["target_cid"]} for r in cur.fetchall()]

        # 1-hop backlinks
        cur = conn.execute(
            "SELECT source_cid, target_raw FROM links WHERE target_cid = ?;",
            (cid,),
        )
        backlinks = [{"source_cid": r["source_cid"], "target_raw": r["target_raw"]} for r in cur.fetchall()]

        # Sources
        cur = conn.execute(
            "SELECT raw_path, resolved FROM sources WHERE source_cid = ?;",
            (cid,),
        )
        sources = [{"path": r["raw_path"], "exists": bool(r["resolved"])} for r in cur.fetchall()]

        # Budget calculation
        truncated = False
        content_to_return = text

        if budget_tokens is not None and budget_tokens > 0:
            char_budget = budget_tokens * 4
            if len(text) > char_budget:
                sections = re.split(r"(^##\s+)", body, flags=re.MULTILINE)
                # Reconstruct sections respecting budget
                accumulated = [sections[0]] if sections else [""]
                current_len = len(text) - len(body) + len(accumulated[0])

                total_sections = max(1, len(sections) // 2)
                included_sections = 0

                i = 1
                while i < len(sections):
                    sec_header = sections[i]
                    sec_body = sections[i + 1] if i + 1 < len(sections) else ""
                    sec_full = sec_header + sec_body
                    if current_len + len(sec_full) > char_budget:
                        break
                    accumulated.append(sec_full)
                    current_len += len(sec_full)
                    included_sections += 1
                    i += 2

                truncated_body = "".join(accumulated).rstrip()
                marker = f"\n\n> [truncated: {included_sections} of {total_sections} sections included]\n"
                content_to_return = serialize_frontmatter(fm, truncated_body + marker)
                truncated = True

        grounded.append(
            {
                "cid": cid,
                "title": fm.get("title", abs_path.stem),
                "type": fm.get("type", "unknown"),
                "status": fm.get("status", "unknown"),
                "trust_tier": derive_trust_tier(fm.get("verified"), compute_body_hash(body)),
                "content": content_to_return,
                "links": links,
                "backlinks": backlinks,
                "sources": sources,
                "truncated": truncated,
            }
        )

    return grounded

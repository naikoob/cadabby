"""High-level atomic vault operations: scaffold, update, verify, and ground.

Conforms strictly to Cadabby Technical Specification §3, §5.1, §6, §8.
Implements the Driving Use Cases layer in Hexagonal Architecture.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from cadabby.adapters.disk_storage import DiskNoteStorage, FileLedger
from cadabby.cache import VaultCache
from cadabby.constants import NOTE_TYPES
from cadabby.domain import Note, VerificationResult
from cadabby.frontmatter import parse_frontmatter, serialize_frontmatter
from cadabby.okf import compute_body_hash, derive_trust_tier, is_valid_actor
from cadabby.ports import LedgerPort, NoteStoragePort
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
    slug = re.sub(r"[^\w\s-]", "", title).strip()
    slug = re.sub(r"[-\s]+", "-", slug)
    return slug or "untitled"


# ============================================================================
# Use Cases (Driving Ports)
# ============================================================================


class ScaffoldNoteUseCase:
    """Driving use case for scaffolding a new typed OKF note (§6)."""

    def __init__(self, storage: NoteStoragePort, ledger: LedgerPort):
        self.storage = storage
        self.ledger = ledger

    def execute(
        self,
        title: str,
        type_: str,
        description: str,
        tags: list[str] | None = None,
        sources: list[str] | None = None,
        body: str = "",
        actor: str = "agent:unknown",
    ) -> Note:
        if type_ not in NOTE_TYPES:
            raise ValueError(f"Invalid note type '{type_}'. Must be one of {NOTE_TYPES}")

        sub_dir = TYPE_TO_DIR[type_]
        stem = sanitize_filename(title)
        rel_path = f"wiki/{sub_dir}/{stem}.md"
        cid = f"wiki/{sub_dir}/{stem}"

        if self.storage.note_exists(cid):
            raise FileExistsError(f"Note already exists at {rel_path}")

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
        note = Note(cid=cid, rel_path=rel_path, frontmatter=fm_data, body=initial_body)
        self.storage.save_note(note)
        self.ledger.append(f"Scaffolded {rel_path}", actor=actor)
        return note


class UpdateNoteUseCase:
    """Driving use case for updating note frontmatter or sections non-destructively (§8.1)."""

    def __init__(self, storage: NoteStoragePort, ledger: LedgerPort):
        self.storage = storage
        self.ledger = ledger

    def execute(
        self,
        cid_or_path: str,
        frontmatter_patch: dict[str, Any] | None = None,
        append_section: tuple[str, str] | None = None,
        replace_section: tuple[str, str] | None = None,
        expected_hash: str | None = None,
        actor: str = "agent:unknown",
    ) -> Note:
        note = self.storage.get_note(cid_or_path)
        if note is None:
            rel = cid_to_path(cid_or_path)
            raise FileNotFoundError(f"Note not found: {rel}")

        if frontmatter_patch:
            note.patch_frontmatter(frontmatter_patch)

        if replace_section:
            heading, new_section_content = replace_section
            note.replace_section(heading, new_section_content)

        if append_section:
            heading, section_content = append_section
            note.append_section(heading, section_content)

        self.storage.save_note(note, expected_hash=expected_hash)
        self.ledger.append(f"Updated {note.rel_path}", actor=actor)
        return note


class VerifyNoteUseCase:
    """Driving use case for stamping content-bound verification attestation (§3.4, §8.2)."""

    def __init__(self, storage: NoteStoragePort, ledger: LedgerPort):
        self.storage = storage
        self.ledger = ledger

    def execute(
        self,
        cid_or_path: str,
        actor: str,
        method: str = "manual-review",
        is_human_authorized: bool = False,
        at_iso: str | None = None,
    ) -> VerificationResult:
        if actor.startswith("human:") and not is_human_authorized:
            raise PermissionError(
                f"Cannot stamp '{actor}' verification over MCP. Human verification requires interactive CLI."
            )

        if not is_valid_actor(actor):
            raise ValueError(f"Invalid actor format '{actor}'. Must match ^(human|agent|process):[A-Za-z0-9._\\-/]+$")

        note = self.storage.get_note(cid_or_path)
        if note is None:
            rel = cid_to_path(cid_or_path)
            raise FileNotFoundError(f"Note not found: {rel}")

        attestation, new_tier, already_verified = note.add_attestation(actor=actor, method=method, at_iso=at_iso)

        if not already_verified:
            self.storage.save_note(note)
            self.ledger.append(f"Verified {note.rel_path} ({actor}) -> {new_tier}", actor=actor)

        return VerificationResult(
            cid=note.cid,
            actor=actor,
            of=attestation["of"],
            trust_tier=new_tier,
            already_verified=already_verified,
        )


class GroundNotesUseCase:
    """Driving use case for retrieving notes and 1-hop graph neighborhood with token budgeting (§8.3)."""

    def __init__(self, vault: Vault, cache: VaultCache | None = None):
        self.vault = vault
        self.cache = cache or VaultCache(vault)

    def execute(
        self,
        cids: Sequence[str],
        budget_tokens: int | None = None,
    ) -> list[dict[str, Any]]:
        self.cache.scan()
        conn = self.cache.get_connection()

        grounded = []

        for raw_cid in cids:
            cid = path_to_cid(raw_cid)
            rel_path = cid_to_path(cid)
            abs_path = self.vault.abs_path(rel_path)

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


# ============================================================================
# Facade Functions (Preserve 100% Backward Compatibility)
# ============================================================================


def scaffold_note(
    vault: Vault,
    title: str,
    type_: str,
    description: str,
    tags: list[str] | None = None,
    sources: list[str] | None = None,
    body: str = "",
    actor: str = "agent:unknown",
    storage: NoteStoragePort | None = None,
    ledger: LedgerPort | None = None,
) -> Path:
    """Scaffold a new wiki note in the directory matching its type."""
    active_storage = storage or DiskNoteStorage(vault)
    active_ledger = ledger or FileLedger(vault)
    uc = ScaffoldNoteUseCase(active_storage, active_ledger)
    note = uc.execute(
        title=title,
        type_=type_,
        description=description,
        tags=tags,
        sources=sources,
        body=body,
        actor=actor,
    )
    return vault.abs_path(note.rel_path)


def update_note(
    vault: Vault,
    cid_or_path: str,
    frontmatter_patch: dict[str, Any] | None = None,
    append_section: tuple[str, str] | None = None,
    replace_section: tuple[str, str] | None = None,
    expected_hash: str | None = None,
    actor: str = "agent:unknown",
    storage: NoteStoragePort | None = None,
    ledger: LedgerPort | None = None,
) -> Path:
    """Non-destructively update note frontmatter or content sections."""
    active_storage = storage or DiskNoteStorage(vault)
    active_ledger = ledger or FileLedger(vault)
    uc = UpdateNoteUseCase(active_storage, active_ledger)
    note = uc.execute(
        cid_or_path=cid_or_path,
        frontmatter_patch=frontmatter_patch,
        append_section=append_section,
        replace_section=replace_section,
        expected_hash=expected_hash,
        actor=actor,
    )
    return vault.abs_path(note.rel_path)


def verify_note(
    vault: Vault,
    cid_or_path: str,
    actor: str,
    method: str = "manual-review",
    is_human_authorized: bool = False,
    storage: NoteStoragePort | None = None,
    ledger: LedgerPort | None = None,
) -> dict[str, Any]:
    """Appends a content-bound verification attestation to a note."""
    active_storage = storage or DiskNoteStorage(vault)
    active_ledger = ledger or FileLedger(vault)
    uc = VerifyNoteUseCase(active_storage, active_ledger)
    res = uc.execute(
        cid_or_path=cid_or_path,
        actor=actor,
        method=method,
        is_human_authorized=is_human_authorized,
    )
    return {
        "cid": res.cid,
        "actor": res.actor,
        "of": res.of,
        "trust_tier": res.trust_tier,
        "already_verified": res.already_verified,
    }


def ground_notes(
    vault: Vault,
    cids: Sequence[str],
    budget_tokens: int | None = None,
    cache: VaultCache | None = None,
) -> list[dict[str, Any]]:
    """Retrieve full content and 1-hop graph neighborhood for the specified CIDs."""
    uc = GroundNotesUseCase(vault, cache=cache)
    return uc.execute(cids=cids, budget_tokens=budget_tokens)

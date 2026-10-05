"""High-level atomic vault operations: scaffold, update, verify, and ground.

Conforms strictly to Cadabby Technical Specification §3, §5.1, §6, §8.
Implements the Driving Use Cases layer in Hexagonal Architecture.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cadabby.adapters.disk_storage import DiskNoteStorage, FileLedger
from cadabby.cache import VaultCache
from cadabby.constants import NOTE_TYPES
from cadabby.domain import Note, VerificationResult, split_markdown_sections
from cadabby.frontmatter import serialize_frontmatter
from cadabby.graph import LinkTargetIndex
from cadabby.okf import is_valid_actor
from cadabby.ports import IndexCachePort, LedgerPort, NoteStoragePort
from cadabby.vault import Vault, cid_to_path, path_to_cid, path_to_layer


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

    def __init__(self, storage: NoteStoragePort, ledger: LedgerPort, vault: Vault | None = None):
        self.storage = storage
        self.ledger = ledger
        self.vault = vault

    def execute(
        self,
        title: str,
        type_: str,
        description: str,
        tags: list[str] | None = None,
        sources: list[str] | None = None,
        body: str = "",
        actor: str = "agent:unknown",
        domain: str = "wiki",
        path: str | None = None,
    ) -> Note:
        if not type_ or not isinstance(type_, str) or not type_.strip():
            raise ValueError("Note type must be a non-empty string")

        target_domain = domain or "wiki"

        if path:
            raw_path = str(path).replace("\\", "/")
            if raw_path.startswith("/") or Path(path).is_absolute():
                raise ValueError(f"Path traversal detected in '{path}': absolute paths are forbidden")
            clean_path = raw_path.strip("/")
            parts = [p for p in clean_path.split("/") if p]
            if any(p == ".." for p in parts):
                raise ValueError(f"Path traversal detected in '{path}': must not contain '..'")
            if len(parts) == 1:
                clean_path = f"{target_domain}/{clean_path}"
            elif parts[0] != target_domain and domain != "wiki":
                clean_path = f"{domain}/{clean_path}"
            rel_path = clean_path if clean_path.endswith(".md") else f"{clean_path}.md"
            cid = path_to_cid(rel_path)
            target_domain = path_to_layer(rel_path)
        else:
            stem = sanitize_filename(title)
            rel_path = f"{target_domain}/{stem}.md"
            cid = f"{target_domain}/{stem}"

        # If vault is available, check allowed_types for the domain
        if self.vault is not None:
            domains = self.vault.discover_domains()
            domain_def = domains.get(target_domain)
            if domain_def and domain_def.allowed_types is not None and type_ not in domain_def.allowed_types:
                raise ValueError(
                    f"Invalid note type '{type_}'. In domain '{target_domain}', allowed types are {domain_def.allowed_types}"
                )
        elif target_domain == "wiki" and type_ not in NOTE_TYPES:
            raise ValueError(f"Invalid note type '{type_}'. Must be one of {NOTE_TYPES}")

        if self.storage.note_exists(cid):
            raise FileExistsError(f"Note already exists at {rel_path}")

        now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

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

    def __init__(
        self,
        vault_or_storage: Vault | NoteStoragePort,
        cache: IndexCachePort | VaultCache | None = None,
        storage: NoteStoragePort | None = None,
        vault: Vault | None = None,
    ):
        if isinstance(vault_or_storage, Vault):
            self.vault = vault_or_storage
            self.storage = storage or DiskNoteStorage(vault_or_storage)
            self.cache = cache or VaultCache(vault_or_storage)
        else:
            self.storage = vault_or_storage
            self.cache = cache
            self.vault = vault

    def execute(
        self,
        cids: Sequence[str],
        budget_tokens: int | None = None,
    ) -> list[dict[str, Any]]:
        conn = self.cache.get_connection() if self.cache is not None else None

        grounded = []

        domains = self.vault.discover_domains() if self.vault is not None else {}

        resolver: LinkTargetIndex | None = None
        if self.cache is not None and hasattr(self.cache, "get_link_resolver"):
            resolver = self.cache.get_link_resolver()
        elif conn is not None:
            cur = conn.execute("SELECT cid FROM notes WHERE layer != 'raw' AND parse_error IS NULL;")
            known = [r["cid"] for r in cur.fetchall()]
            resolver = LinkTargetIndex(known)
        elif self.storage is not None and hasattr(self.storage, "list_note_cids"):
            resolver = LinkTargetIndex(self.storage.list_note_cids())

        for raw_target in cids:
            clean = raw_target.strip()
            if clean.startswith("[[") and clean.endswith("]]"):
                clean = clean[2:-2].strip()
            clean = clean.split("|")[0].split("#")[0].strip()

            cid = path_to_cid(clean)
            note = self.storage.get_note(cid)
            if note is None and resolver is not None:
                resolved = resolver.resolve(clean)
                if resolved:
                    note = self.storage.get_note(resolved)

            if note is None:
                continue

            cid = note.cid

            domain_name = path_to_layer(note.rel_path)
            domain_def = domains.get(domain_name)
            directives = domain_def.directives_markdown if domain_def is not None else ""

            links: list[dict[str, Any]] = []
            backlinks: list[dict[str, Any]] = []
            sources: list[dict[str, Any]] = []

            if conn is not None:
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

            # Budget calculation using code-fence aware section splitting
            full_text = note.serialize()
            truncated = False
            content_to_return = full_text

            if budget_tokens is not None and budget_tokens > 0:
                char_budget = budget_tokens * 4
                if len(full_text) > char_budget:
                    sections = split_markdown_sections(note.body)
                    preamble = sections[0][1] if sections else ""
                    accumulated = [preamble]
                    current_len = len(full_text) - len(note.body) + len(preamble)

                    total_sections = max(1, len(sections))
                    included_sections = 1 if preamble else 0

                    for _, sec_text in sections[1:]:
                        if current_len + len(sec_text) > char_budget:
                            break
                        accumulated.append(sec_text)
                        current_len += len(sec_text)
                        included_sections += 1

                    truncated_body = "".join(accumulated).rstrip()
                    marker = f"\n\n> [truncated: {included_sections} of {total_sections} sections included]\n"
                    content_to_return = serialize_frontmatter(note.frontmatter, truncated_body + marker)
                    truncated = True

            grounded.append(
                {
                    "cid": cid,
                    "domain": domain_name,
                    "title": note.title or Path(note.rel_path).stem,
                    "type": note.type,
                    "status": note.status,
                    "trust_tier": note.trust_tier,
                    "directives": directives,
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


def _resolve_adapters(
    vault: Vault,
    storage: NoteStoragePort | None = None,
    ledger: LedgerPort | None = None,
) -> tuple[NoteStoragePort, LedgerPort]:
    """Helper to instantiate default disk storage and ledger adapters."""
    return storage or DiskNoteStorage(vault), ledger or FileLedger(vault)


def scaffold_note(
    vault: Vault,
    title: str,
    type_: str,
    description: str,
    tags: list[str] | None = None,
    sources: list[str] | None = None,
    body: str = "",
    actor: str = "agent:unknown",
    domain: str = "wiki",
    path: str | None = None,
    storage: NoteStoragePort | None = None,
    ledger: LedgerPort | None = None,
) -> Path:
    """Scaffold a new note in the directory matching its type or custom path."""
    active_storage, active_ledger = _resolve_adapters(vault, storage, ledger)
    uc = ScaffoldNoteUseCase(active_storage, active_ledger, vault=vault)
    note = uc.execute(
        title=title,
        type_=type_,
        description=description,
        tags=tags,
        sources=sources,
        body=body,
        actor=actor,
        domain=domain,
        path=path,
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
    active_storage, active_ledger = _resolve_adapters(vault, storage, ledger)
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
    active_storage, active_ledger = _resolve_adapters(vault, storage, ledger)
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
    storage: NoteStoragePort | None = None,
) -> list[dict[str, Any]]:
    """Retrieve full content and 1-hop graph neighborhood for the specified CIDs."""
    active_storage, _ = _resolve_adapters(vault, storage)
    if cache is None:
        active_cache = VaultCache(vault)
        active_cache.scan()
    else:
        active_cache = cache
    uc = GroundNotesUseCase(vault_or_storage=active_storage, cache=active_cache, vault=vault)
    return uc.execute(cids=cids, budget_tokens=budget_tokens)

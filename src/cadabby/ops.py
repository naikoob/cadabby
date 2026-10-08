"""High-level atomic vault operations: scaffold, update, verify, and ground.

Conforms strictly to Cadabby Technical Specification §3, §5.1, §6, §8.
Implements the Driving Use Cases layer in Hexagonal Architecture.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from cadabby.adapters.disk_storage import DiskNoteStorage, FileLedger
from cadabby.cache import VaultCache
from cadabby.constants import DIR_WIKI, FILE_AGENTS, TEMPLATE_TITLE_PLACEHOLDER
from cadabby.domain import Note, VerificationResult, split_markdown_sections
from cadabby.errors import HumanAttestationRefusedError
from cadabby.frontmatter import serialize_frontmatter
from cadabby.graph import LinkTargetIndex, normalize_link_target
from cadabby.okf import utc_now_iso
from cadabby.ports import IndexCachePort, LedgerPort, NoteStoragePort
from cadabby.vault import Vault, cid_to_path, note_path_violation, path_to_cid, path_to_layer


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

    def __init__(self, storage: NoteStoragePort, ledger: LedgerPort, vault: Vault | None = None) -> None:
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
        domain: str = DIR_WIKI,
        path: str | None = None,
        template: str | None = None,
    ) -> Note:
        if not type_ or not isinstance(type_, str) or not type_.strip():
            raise ValueError("Note type must be a non-empty string")

        if template:
            if body.strip():
                raise ValueError("Pass either 'body' or 'template', not both")
            if self.vault is None:
                raise ValueError("Templates require a vault; none is bound to this use case")
            body = self.vault.read_template_body(template).replace(
                TEMPLATE_TITLE_PLACEHOLDER, title
            )

        target_domain = domain or DIR_WIKI

        if path:
            raw_path = str(path).replace("\\", "/")
            if raw_path.startswith("/") or Path(path).is_absolute():
                raise ValueError(f"Path traversal detected in '{path}': absolute paths are forbidden")
            parts = [p for p in raw_path.strip("/").split("/") if p]
            if not parts or any(p in (".", "..") for p in parts):
                raise ValueError(f"Path traversal detected in '{path}': must not contain '.' or '..'")
            clean_path = "/".join(parts)
            if len(parts) == 1:
                clean_path = f"{target_domain}/{clean_path}"
            elif parts[0] != target_domain and domain != DIR_WIKI:
                clean_path = f"{domain}/{clean_path}"
            rel_path = clean_path if clean_path.endswith(".md") else f"{clean_path}.md"
            cid = path_to_cid(rel_path)
            target_domain = path_to_layer(rel_path)
        else:
            stem = sanitize_filename(title)
            rel_path = f"{target_domain}/{stem}.md"
            cid = f"{target_domain}/{stem}"

        # Refuse a location the scan would never index (§2.1, §2.2, §7.5);
        # writing there would mint a file no search, lint or index ever sees.
        if self.vault is not None:
            why = self.vault.note_path_violation(rel_path, for_write=True)
        else:
            why = note_path_violation(rel_path, for_write=True)
        if why:
            raise ValueError(f"Cannot scaffold at '{rel_path}': {why}")

        # If vault is available, check allowed_types for the domain
        if self.vault is not None:
            domains = self.vault.discover_domains()
            domain_def = domains.get(target_domain)
            if domain_def and domain_def.manifest_error:
                raise ValueError(
                    f"Cannot scaffold into domain '{target_domain}': its {FILE_AGENTS} is invalid "
                    f"({domain_def.manifest_error}). Fix the manifest first"
                )
            if domain_def and domain_def.allowed_types is not None and type_ not in domain_def.allowed_types:
                raise ValueError(
                    f"Invalid note type '{type_}'. In domain '{target_domain}', allowed types are {domain_def.allowed_types}"
                )

        if self.storage.note_exists(cid):
            raise FileExistsError(f"Note already exists at {rel_path}")

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
                "at": utc_now_iso(),
            },
        }

        initial_body = body.strip() or f"# {title}\n\n"
        note = Note(cid=cid, rel_path=rel_path, frontmatter=fm_data, body=initial_body)
        self.storage.save_note(note)
        self.ledger.append(f"Scaffolded {rel_path}", actor=actor)
        return note


EDIT_OPS: dict[str, tuple[str, ...]] = {
    "replace_text": ("old", "new"),
    "replace_section": ("heading", "body"),
    "append_section": ("heading", "body"),
}


def _validate_edits(edits: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Check every edit's shape before any is applied, so a bad one writes nothing."""
    if not isinstance(edits, list):
        raise ValueError("edits must be a list of {op, ...} objects")
    clean: list[dict[str, str]] = []
    for i, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise ValueError(f"edits[{i}] must be an object")
        op = edit.get("op")
        if op not in EDIT_OPS:
            raise ValueError(f"edits[{i}].op must be one of {sorted(EDIT_OPS)}, got {op!r}")
        required = EDIT_OPS[op]
        # `body`/`new` may legitimately be empty strings; only absence is an error.
        missing = [k for k in required if not isinstance(edit.get(k), str)]
        if missing:
            raise ValueError(f"edits[{i}] ({op}) requires string field(s): {', '.join(missing)}")
        clean.append({"op": op, **{k: edit[k] for k in required}})
    return clean


class UpdateNoteUseCase:
    """Driving use case for updating note frontmatter or sections non-destructively (§5.1, §8)."""

    def __init__(self, storage: NoteStoragePort, ledger: LedgerPort) -> None:
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
        edits: list[dict[str, Any]] | None = None,
    ) -> Note:
        # Legacy single-operation parameters keep their historical order
        # (replace, then append) and run before any `edits`.
        ops: list[dict[str, Any]] = []
        if replace_section:
            ops.append({"op": "replace_section", "heading": replace_section[0], "body": replace_section[1]})
        if append_section:
            ops.append({"op": "append_section", "heading": append_section[0], "body": append_section[1]})
        if edits is not None:
            ops.extend(edits)
        validated = _validate_edits(ops)

        note = self.storage.get_note(cid_or_path)
        if note is None:
            rel = cid_to_path(cid_or_path)
            raise FileNotFoundError(f"Note not found: {rel}")

        if frontmatter_patch:
            note.patch_frontmatter(frontmatter_patch)

        # Every edit is applied in memory first; a failure raises before
        # save_note, so the file is either fully updated or untouched.
        for edit in validated:
            if edit["op"] == "replace_text":
                note.replace_text(edit["old"], edit["new"])
            elif edit["op"] == "replace_section":
                note.replace_section(edit["heading"], edit["body"])
            else:
                note.append_section(edit["heading"], edit["body"])

        effective_expected = expected_hash if expected_hash is not None else note.source_hash
        self.storage.save_note(note, expected_hash=effective_expected)
        suffix = f" ({len(validated)} edits)" if len(validated) > 1 else ""
        self.ledger.append(f"Updated {note.rel_path}{suffix}", actor=actor)
        return note


class VerifyNoteUseCase:
    """Driving use case for stamping content-bound verification attestation (§3.4, §8)."""

    def __init__(self, storage: NoteStoragePort, ledger: LedgerPort) -> None:
        self.storage = storage
        self.ledger = ledger

    def execute(
        self,
        cid_or_path: str,
        actor: str,
        method: str = "manual-review",
        is_human_authorized: bool = False,
    ) -> VerificationResult:
        if actor.startswith("human:") and not is_human_authorized:
            raise HumanAttestationRefusedError(
                f"Cannot stamp '{actor}': human verification requires the interactive CLI (cadabby verify --human)."
            )

        # Actor syntax is validated once, by Note.add_attestation.

        note = self.storage.get_note(cid_or_path)
        if note is None:
            rel = cid_to_path(cid_or_path)
            raise FileNotFoundError(f"Note not found: {rel}")

        attestation, new_tier, already_verified = note.add_attestation(actor=actor, method=method)

        if not already_verified:
            self.storage.save_note(note, expected_hash=note.source_hash)
            self.ledger.append(f"Verified {note.rel_path} ({actor}) -> {new_tier}", actor=actor)

        return VerificationResult(
            cid=note.cid,
            actor=actor,
            of=attestation["of"],
            trust_tier=new_tier,
            already_verified=already_verified,
        )


def _truncate_to_budget(note: Note, budget_tokens: int | None) -> tuple[str, bool]:
    """Return (serialized_content, was_truncated) honoring a token budget via section splitting (§5.4)."""
    full_text = note.serialize()
    if budget_tokens is None or budget_tokens <= 0:
        return full_text, False

    char_budget = budget_tokens * 4
    if len(full_text) <= char_budget:
        return full_text, False

    sections = split_markdown_sections(note.body)
    total_sections = max(1, len(sections))
    fm_overhead = len(full_text) - len(note.body)
    accumulated: list[str] = []
    current_len = fm_overhead
    included_sections = 0

    for _, sec_text in sections:
        if current_len + len(sec_text) > char_budget:
            break
        accumulated.append(sec_text)
        current_len += len(sec_text)
        included_sections += 1

    marker = f"\n\n> [truncated: {included_sections} of {total_sections} sections included]\n"
    if accumulated:
        truncated_body = "".join(accumulated).rstrip()
    else:
        remaining = max(0, char_budget - fm_overhead - len(marker))
        truncated_body = note.body[:remaining].rstrip()
    return serialize_frontmatter(note.frontmatter, truncated_body + marker), True


class GroundNotesUseCase:
    """Driving use case for retrieving notes and 1-hop graph neighborhood with token budgeting (§5.1)."""

    def __init__(
        self,
        storage: NoteStoragePort,
        cache: IndexCachePort | None = None,
        vault: Vault | None = None,
    ) -> None:
        self.storage = storage
        self.cache = cache
        self.vault = vault

    def execute(
        self,
        cids: Sequence[str],
        budget_tokens: int | None = None,
    ) -> list[dict[str, Any]]:
        grounded: list[dict[str, Any]] = []
        domains = self.vault.discover_domains() if self.vault is not None else {}
        resolver = (
            self.cache.get_link_resolver()
            if self.cache is not None
            else LinkTargetIndex(self.storage.list_note_cids())
        )

        for raw_target in cids:
            clean = normalize_link_target(raw_target)

            note = self.storage.get_note(path_to_cid(clean))
            if note is None:
                resolved = resolver.resolve(clean)
                if resolved:
                    note = self.storage.get_note(resolved)
            if note is None:
                continue

            cid = note.cid
            domain_name = path_to_layer(note.rel_path)
            domain_def = domains.get(domain_name)
            directives = domain_def.directives_markdown if domain_def is not None else ""

            if self.cache is not None:
                links, backlinks, sources = self.cache.get_note_neighborhood(cid)
            else:
                links, backlinks, sources = [], [], []

            content_to_return, truncated = _truncate_to_budget(note, budget_tokens)

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


def _refresh(cache: IndexCachePort | None) -> None:
    """End a write with a scan, so search and index.md see it at once (§4.3).

    The CLI and MCP both pass their cache; a library caller that passes none
    gets the write without the refresh, and its next scan picks the file up.
    """
    if cache is not None:
        cache.scan()


def scaffold_note(
    vault: Vault,
    title: str,
    type_: str,
    description: str,
    tags: list[str] | None = None,
    sources: list[str] | None = None,
    body: str = "",
    actor: str = "agent:unknown",
    domain: str = DIR_WIKI,
    path: str | None = None,
    template: str | None = None,
    storage: NoteStoragePort | None = None,
    ledger: LedgerPort | None = None,
    cache: IndexCachePort | None = None,
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
        template=template,
    )
    _refresh(cache)
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
    cache: IndexCachePort | None = None,
    edits: list[dict[str, Any]] | None = None,
) -> Path:
    """Non-destructively update note frontmatter, sections, or exact body text."""
    active_storage, active_ledger = _resolve_adapters(vault, storage, ledger)
    uc = UpdateNoteUseCase(active_storage, active_ledger)
    note = uc.execute(
        cid_or_path=cid_or_path,
        frontmatter_patch=frontmatter_patch,
        append_section=append_section,
        replace_section=replace_section,
        expected_hash=expected_hash,
        actor=actor,
        edits=edits,
    )
    _refresh(cache)
    return vault.abs_path(note.rel_path)


def verify_note(
    vault: Vault,
    cid_or_path: str,
    actor: str,
    method: str = "manual-review",
    is_human_authorized: bool = False,
    storage: NoteStoragePort | None = None,
    ledger: LedgerPort | None = None,
    cache: IndexCachePort | None = None,
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
    _refresh(cache)
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
    cache: IndexCachePort | None = None,
    storage: NoteStoragePort | None = None,
) -> list[dict[str, Any]]:
    """Retrieve full content and 1-hop graph neighborhood for the specified CIDs."""
    active_storage, _ = _resolve_adapters(vault, storage)
    if cache is None:
        with VaultCache(vault) as active_cache:
            active_cache.scan()
            uc = GroundNotesUseCase(storage=active_storage, cache=active_cache, vault=vault)
            return uc.execute(cids=cids, budget_tokens=budget_tokens)
    uc = GroundNotesUseCase(storage=active_storage, cache=cache, vault=vault)
    return uc.execute(cids=cids, budget_tokens=budget_tokens)

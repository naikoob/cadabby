"""High-level atomic vault operations: scaffold, update, move, verify, and ground.

Conforms strictly to Cadabby Technical Specification §3, §5.1, §6, §8, §9.1.
Implements the Driving Use Cases layer in Hexagonal Architecture.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cadabby.adapters.disk_storage import DiskNoteStorage, FileLedger
from cadabby.cache import VaultCache, moc_overview
from cadabby.constants import DIR_RAW, DIR_WIKI, TEMPLATE_TITLE_PLACEHOLDER
from cadabby.domain import (
    ENGINE_OWNED_FIELDS,
    Note,
    VerificationResult,
    validate_frontmatter_patch,
)
from cadabby.errors import HumanAttestationRefusedError, VaultConflictError, classify
from cadabby.fsutil import compute_bytes_sha256
from cadabby.graph import LinkTargetIndex, get_note_graph, strip_link_wrapper
from cadabby.ground import (
    RAW_GROUND_MAX_BYTES,
    GroundNotesUseCase,
    _truncate_sections,
    _truncate_to_budget,
)
from cadabby.move import (
    MoveNoteUseCase,
    MovePlan,
    _apply_spans,
    _markdown_path_text,
    _MovedNote,
    _reparse,
    _Rewrite,
    _shortest_wiki_target,
    _write_steps,
)
from cadabby.ports import IndexCachePort, LedgerPort, NoteStoragePort, VaultLayoutPort
from cadabby.resolve import (
    _check_new_location,
    _default_layout,
    _FORBIDDEN_NAME_CHARS,
    _match_listed_path,
    _name_violation,
    _parse_relative_target,
    _require_actor,
    _strip_dot_slash,
    _vault_name_parts,
    load_existing_note,
    resolve_note_cid,
    sanitize_filename,
    with_md_suffix,
)
from cadabby.vault import Vault, cid_to_path, path_to_cid

__all__ = [
    "EDIT_OPS",
    "ENGINE_OWNED_FIELDS",
    "GRAPH_VIEWS",
    "GroundNotesUseCase",
    "MoveNoteUseCase",
    "MovePlan",
    "RAW_GROUND_MAX_BYTES",
    "ScaffoldNoteUseCase",
    "UpdateNoteUseCase",
    "VerifyNoteUseCase",
    "WriteResult",
    "_FORBIDDEN_NAME_CHARS",
    "_MovedNote",
    "_Rewrite",
    "_apply_spans",
    "_check_new_location",
    "_default_layout",
    "_markdown_path_text",
    "_match_listed_path",
    "_name_violation",
    "_parse_relative_target",
    "_reparse",
    "_require_actor",
    "_shortest_wiki_target",
    "_strip_dot_slash",
    "_truncate_sections",
    "_truncate_to_budget",
    "_validate_edits",
    "_validate_frontmatter_patch",
    "_vault_name_parts",
    "_write_steps",
    "graph_view",
    "ground_notes",
    "load_existing_note",
    "move_note",
    "resolve_note_cid",
    "sanitize_filename",
    "scaffold_note",
    "scaffold_note_result",
    "update_note",
    "update_note_result",
    "verify_note",
    "with_md_suffix",
]


# ============================================================================
# Use Cases (Driving Ports)
# ============================================================================


class ScaffoldNoteUseCase:
    """Driving use case for scaffolding a new typed OKF note (§6)."""

    def __init__(self, storage: NoteStoragePort, ledger: LedgerPort, vault: VaultLayoutPort | None = None) -> None:
        self.storage = storage
        self.ledger = ledger
        self.vault = _default_layout(storage, vault)

    @staticmethod
    def _resolve_scaffold_destination(title: str, domain: str, path: str | None) -> tuple[str, str]:
        """Resolve `(rel_path, cid)` for a newly scaffolded note (§5.1)."""
        # An empty domain is the default, like an empty path: the CLI passes ""
        # for an omitted --domain, which used to build `/name.md`.
        domain = domain or DIR_WIKI
        target_domain = domain
        # An empty path is the same as none: some clients send "" for every
        # optional string they were not given.
        if path is not None and path.strip():
            raw_path, parts = _parse_relative_target(
                path,
                absolute_error=f"Path traversal detected in '{path}': absolute paths are forbidden",
            )
            if raw_path.endswith("/"):
                # A trailing slash names a folder, as in vault_move_note.
                parts.append(sanitize_filename(title))
            clean_path = "/".join(parts)
            if len(parts) == 1:
                clean_path = f"{target_domain}/{clean_path}"
            elif parts[0] != target_domain and domain != DIR_WIKI:
                clean_path = f"{domain}/{clean_path}"
            rel_path = with_md_suffix(clean_path)
            return rel_path, path_to_cid(rel_path)

        stem = sanitize_filename(title)
        return f"{target_domain}/{stem}.md", f"{target_domain}/{stem}"

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
        for name, value in (("title", title), ("description", description)):
            if not isinstance(value, str):
                raise ValueError(f"Note {name} must be a string")
            if not value.strip():
                # Lint would report the note FIELD_MISSING the moment it exists (§6.3).
                raise ValueError(f"Note {name} must not be empty")
        if path is not None and not isinstance(path, str):
            raise ValueError("Note path must be a string")
        # generated.by is attribution like verified[].by, so the same grammar binds (§3.4).
        _require_actor(actor)

        if template:
            if body.strip():
                raise ValueError("Pass either 'body' or 'template', not both")
            body = self.vault.read_template_body(template).replace(
                TEMPLATE_TITLE_PLACEHOLDER, title
            )

        rel_path, cid = self._resolve_scaffold_destination(title, domain, path)
        _check_new_location(self.storage, self.vault, rel_path, type_, verb="scaffold")

        note = Note.new(
            cid=cid,
            rel_path=rel_path,
            type_=type_,
            title=title,
            description=description,
            tags=tags,
            sources=sources,
            actor=actor,
            body=body,
        )
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


# The engine-owned-field guard lives on the aggregate (domain.Note.patch_frontmatter);
# ops calls it early too, so a bad patch is refused before the note is even read.
_validate_frontmatter_patch = validate_frontmatter_patch


class UpdateNoteUseCase:
    """Driving use case for updating note frontmatter or sections non-destructively (§5.1, §8)."""

    def __init__(self, storage: NoteStoragePort, ledger: LedgerPort) -> None:
        self.storage = storage
        self.ledger = ledger

    def execute(
        self,
        cid_or_path: str,
        frontmatter_patch: dict[str, Any] | None = None,
        *,
        expected_hash: str | None = None,
        actor: str = "agent:unknown",
        edits: list[dict[str, Any]] | None = None,
    ) -> Note:
        _require_actor(actor)
        validated = _validate_edits(list(edits) if edits is not None else [])
        _validate_frontmatter_patch(frontmatter_patch)

        note = load_existing_note(self.storage, cid_or_path)
        before = (copy.deepcopy(note.frontmatter), note.body)

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

        if (note.frontmatter, note.body) == before:
            # Writing anyway would canonicalize the file and log an "Updated"
            # that changed nothing -- a retried call would look like two edits.
            raise ValueError(
                f"Nothing to update in {note.rel_path}: the note already has these values"
                if validated or frontmatter_patch
                else "Nothing to update: pass patch_frontmatter or edits"
            )

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
        expected_body_hash: str | None = None,
    ) -> VerificationResult:
        """Stamp an attestation bound to the note's current body hash.

        `expected_body_hash` is the hash the attester actually reviewed. Given,
        a body changed since then is a VAULT_CONFLICT and nothing is signed:
        `verify --human` showed one hash and re-read the note to stamp, so an
        agent editing the body while the person typed the token got a human
        attestation on text the person never saw (§3.5).
        """
        if actor.startswith("human:") and not is_human_authorized:
            raise HumanAttestationRefusedError(
                f"Cannot stamp '{actor}': human verification requires the interactive CLI (cadabby verify --human)."
            )

        # Actor syntax is validated once, by Note.add_attestation.

        note = load_existing_note(self.storage, cid_or_path)
        if expected_body_hash is not None and note.body_hash != expected_body_hash:
            raise VaultConflictError(
                f"VAULT_CONFLICT: {note.rel_path} changed since it was reviewed "
                f"(reviewed {expected_body_hash[:19]}..., now {note.body_hash[:19]}...); nothing was signed"
            )

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


def _refresh(cache: IndexCachePort | None) -> list[str]:
    """End a write with a scan, so search and index.md see it at once (§4.3).

    The CLI and MCP both pass their cache; a library caller that passes none
    gets the write without the refresh, and its next scan picks the file up.

    Returns warnings rather than raising: the note is already written and
    logged, and reporting the call as failed (a stray byte in index.md, a busy
    cache) sent agents into a retry that met ALREADY_EXISTS or wrote twice.
    """
    if cache is None:
        return []
    try:
        cache.scan()
    except Exception as exc:  # noqa: BLE001 -- the write itself succeeded; say what did not
        info = classify(exc)
        return [f"The write succeeded, but search and index.md were not refreshed ({info.code}: {info.message})"]
    return []


@dataclass(frozen=True, slots=True)
class WriteResult:
    """What a note write returns over MCP and `--json` (§5.1): structured, never prose.

    `source_hash` is the concurrency token of the bytes just written, so it
    chains straight into the next call's `expected_hash`.
    """

    cid: str
    rel_path: str
    source_hash: str
    trust_tier: str
    warnings: tuple[str, ...] = ()

    @classmethod
    def of(cls, note: Note, warnings: list[str]) -> WriteResult:
        return cls(
            cid=note.cid,
            rel_path=note.rel_path,
            source_hash=compute_bytes_sha256(note.serialize().encode("utf-8")),
            trust_tier=note.trust_tier,
            warnings=tuple(warnings),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "cid": self.cid,
            "rel_path": self.rel_path,
            "source_hash": self.source_hash,
            "trust_tier": self.trust_tier,
            "warnings": list(self.warnings),
        }


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
    result = scaffold_note_result(
        vault, title, type_, description, tags=tags, sources=sources, body=body, actor=actor,
        domain=domain, path=path, template=template, storage=storage, ledger=ledger, cache=cache,
    )
    return vault.abs_path(result.rel_path)


def scaffold_note_result(
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
) -> WriteResult:
    """`scaffold_note`, returning the structured result the MCP tool and `--json` report."""
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
    return WriteResult.of(note, _refresh(cache))


def update_note(
    vault: Vault,
    cid_or_path: str,
    frontmatter_patch: dict[str, Any] | None = None,
    *,
    edits: list[dict[str, Any]] | None = None,
    expected_hash: str | None = None,
    actor: str = "agent:unknown",
    storage: NoteStoragePort | None = None,
    ledger: LedgerPort | None = None,
    cache: IndexCachePort | None = None,
) -> Path:
    """Non-destructively patch note frontmatter and apply ordered body `edits` (§5.1)."""
    result = update_note_result(
        vault, cid_or_path, frontmatter_patch, edits=edits, expected_hash=expected_hash, actor=actor,
        storage=storage, ledger=ledger, cache=cache,
    )
    return vault.abs_path(result.rel_path)


def update_note_result(
    vault: Vault,
    cid_or_path: str,
    frontmatter_patch: dict[str, Any] | None = None,
    *,
    edits: list[dict[str, Any]] | None = None,
    expected_hash: str | None = None,
    actor: str = "agent:unknown",
    storage: NoteStoragePort | None = None,
    ledger: LedgerPort | None = None,
    cache: IndexCachePort | None = None,
) -> WriteResult:
    """`update_note`, returning the structured result the MCP tool and `--json` report."""
    active_storage, active_ledger = _resolve_adapters(vault, storage, ledger)
    uc = UpdateNoteUseCase(active_storage, active_ledger)
    note = uc.execute(
        cid_or_path=cid_or_path,
        frontmatter_patch=frontmatter_patch,
        expected_hash=expected_hash,
        actor=actor,
        edits=edits,
    )
    return WriteResult.of(note, _refresh(cache))


def verify_note(
    vault: Vault,
    cid_or_path: str,
    actor: str,
    method: str = "manual-review",
    is_human_authorized: bool = False,
    storage: NoteStoragePort | None = None,
    ledger: LedgerPort | None = None,
    cache: IndexCachePort | None = None,
    expected_body_hash: str | None = None,
) -> dict[str, Any]:
    """Appends a content-bound verification attestation to a note."""
    active_storage, active_ledger = _resolve_adapters(vault, storage, ledger)
    uc = VerifyNoteUseCase(active_storage, active_ledger)
    res = uc.execute(
        cid_or_path=cid_or_path,
        actor=actor,
        method=method,
        is_human_authorized=is_human_authorized,
        expected_body_hash=expected_body_hash,
    )
    warnings = _refresh(cache)
    return {
        "cid": res.cid,
        "actor": res.actor,
        "of": res.of,
        "trust_tier": res.trust_tier,
        "already_verified": res.already_verified,
        "warnings": warnings,
    }


def ground_notes(
    vault: Vault,
    targets: Sequence[str],
    budget_tokens: int | None = None,
    cache: IndexCachePort | None = None,
    storage: NoteStoragePort | None = None,
) -> list[dict[str, Any]]:
    """Retrieve notes (content and 1-hop graph) or raw/ sources (text and citing notes) (§5.1)."""
    active_storage, _ = _resolve_adapters(vault, storage)
    if cache is None:
        with VaultCache(vault) as active_cache:
            active_cache.scan()
            uc = GroundNotesUseCase(storage=active_storage, cache=active_cache, vault=vault)
            return uc.execute(targets=targets, budget_tokens=budget_tokens)
    uc = GroundNotesUseCase(storage=active_storage, cache=cache, vault=vault)
    return uc.execute(targets=targets, budget_tokens=budget_tokens)


GRAPH_VIEWS = ("neighborhood", "mocs")


def graph_view(
    vault: Vault,
    cache: VaultCache,
    *,
    view: str = "neighborhood",
    target: str | None = None,
    limit: int = 20,
    storage: NoteStoragePort | None = None,
) -> dict[str, Any]:
    """Link structure without note text: one note's neighborhood, or the MOC overview (§5.1 `vault_graph`).

    `target` names a note exactly as for ground and the write tools, so a name
    resolves (or is refused as ambiguous) the same way on every surface. The
    cache must already be scanned; every entry point scans first (§4.3).
    """
    if view not in GRAPH_VIEWS:
        raise ValueError(f"view must be one of {list(GRAPH_VIEWS)}")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if view == "mocs":
        if target is not None:
            raise ValueError("view 'mocs' covers every MOC; omit 'target'")
        return {"view": "mocs"} | moc_overview(cache.get_connection(), limit)
    if not target or not target.strip():
        raise ValueError("view 'neighborhood' needs a 'target' note")
    if _strip_dot_slash(strip_link_wrapper(target)).startswith(f"{DIR_RAW}/"):
        raise ValueError("raw/ sources are not graph nodes; vault_ground a raw/ path lists the notes citing it")
    active_storage, _ = _resolve_adapters(vault, storage)
    listed = list(active_storage.list_note_cids())
    cid = resolve_note_cid(active_storage, target, LinkTargetIndex(listed), cids=listed)
    # Read first, as ground and update do: a note whose frontmatter does not
    # parse is FRONTMATTER_UNPARSEABLE, not a node with no title and "no
    # backlinks" (links into it never resolve, so that would be false).
    load_existing_note(active_storage, cid)
    data = get_note_graph(cache.get_connection(), cid, limit=limit)
    if data is None:
        raise FileNotFoundError(f"Note not found: {cid_to_path(cid)}")
    return {"view": "neighborhood"} | data


def move_note(
    vault: Vault,
    cid_or_path: str,
    to: str,
    *,
    apply: bool = False,
    approve_stale: Sequence[str] = (),
    plan_token: str | None = None,
    actor: str = "agent:unknown",
    storage: NoteStoragePort | None = None,
    ledger: LedgerPort | None = None,
    cache: IndexCachePort | None = None,
) -> dict[str, Any]:
    """Move or rename a note and repoint its links; a dry-run unless `apply` (§5.1).

    Passing the dry-run's `plan_token` to the apply guarantees that what is
    written is what the user saw: a vault changed in between is a VAULT_CONFLICT.
    """
    active_storage, active_ledger = _resolve_adapters(vault, storage, ledger)
    uc = MoveNoteUseCase(active_storage, active_ledger, vault=vault)
    plan = uc.plan(cid_or_path, to, approve_stale=approve_stale)
    if apply:
        if plan_token is not None and plan_token != plan.token:
            raise VaultConflictError(
                "VAULT_CONFLICT: the vault changed since the dry run that produced this plan_token; "
                "run the dry run again and review the new plan"
            )
        uc.apply(plan, actor=actor)
        return plan.to_dict(applied=True) | {"warnings": _refresh(cache)}
    return plan.to_dict(applied=False)

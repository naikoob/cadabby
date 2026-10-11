"""High-level atomic vault operations: scaffold, update, move, verify, and ground.

Conforms strictly to Cadabby Technical Specification §3, §5.1, §6, §8.
Implements the Driving Use Cases layer in Hexagonal Architecture.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

from cadabby.adapters.disk_storage import DiskNoteStorage, FileLedger
from cadabby.cache import VaultCache
from cadabby.constants import DIR_RAW, DIR_WIKI, FILE_AGENTS, TEMPLATE_TITLE_PLACEHOLDER
from cadabby.domain import Note, VerificationResult, split_markdown_sections
from cadabby.errors import AmbiguousNoteError, HumanAttestationRefusedError, VaultConflictError
from cadabby.frontmatter import FrontmatterParseError, serialize_frontmatter, split_frontmatter
from cadabby.graph import (
    LINK_KIND_IMAGE,
    LINK_KIND_MARKDOWN,
    LINK_KIND_REFDEF,
    LINK_KIND_WIKI,
    RE_URL_SCHEME,
    LinkSpan,
    LinkTargetIndex,
    is_attachment_target,
    is_raw_path,
    iter_link_spans,
    iter_relative_path_spans,
    normalize_link_target,
    relative_note_path,
    resolve_markdown_target,
    resolve_relative_path,
)
from cadabby.okf import utc_now_iso
from cadabby.ports import IndexCachePort, LedgerPort, NoteStoragePort
from cadabby.vault import Vault, cid_to_path, normalize_rel, note_path_violation, path_to_cid, path_to_layer


def sanitize_filename(title: str) -> str:
    """Lowercase kebab-case stem for a title (STYLE.md); 'untitled' if nothing survives."""
    slug = re.sub(r"[^\w\s-]", "", title).strip().lower()
    slug = re.sub(r"[-\s_]+", "-", slug).strip("-")
    return slug or "untitled"


def resolve_note_cid(
    storage: NoteStoragePort,
    cid_or_path: str,
    resolver: LinkTargetIndex | None = None,
) -> str:
    """The canonical CID of an existing note named by path, stem, or wikilink (§5.1).

    The result is always a member of `storage.list_note_cids()`. That matters
    on case-insensitive filesystems, where the storage would happily open
    `wiki/flash-attention.md` for `wiki/Flash-Attention.md` and then name the
    note after the caller's spelling. Order: the exact path (so a filename
    containing `#` or `|` still resolves), a unique letter-case-insensitive
    path, then the name read as a wikilink. A name that could mean several
    notes raises AmbiguousNoteError rather than picking one.
    """
    if not isinstance(cid_or_path, str) or not cid_or_path.strip():
        raise ValueError("Note CID or path must be a non-empty string")
    raw = cid_or_path.strip().replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    if raw.startswith("/") or Path(raw).is_absolute() or ".." in raw.split("/"):
        raise FileNotFoundError(f"Note not found: {cid_or_path.strip()} (names must be vault-relative)")

    cids = list(storage.list_note_cids())
    exact = path_to_cid(cid_to_path(raw))
    if exact in cids:
        return exact
    folded = sorted(c for c in cids if c.casefold() == exact.casefold())
    if len(folded) == 1:
        return folded[0]
    if folded:
        raise AmbiguousNoteError(cid_or_path.strip(), folded)

    clean = normalize_link_target(raw)
    found = (resolver if resolver is not None else LinkTargetIndex(cids)).candidates(clean) if clean else []
    if len(found) == 1:
        return found[0]
    if found:
        raise AmbiguousNoteError(cid_or_path.strip(), found)
    raise FileNotFoundError(f"Note not found: {cid_or_path.strip()}")


def load_existing_note(
    storage: NoteStoragePort,
    cid_or_path: str,
    resolver: LinkTargetIndex | None = None,
) -> Note:
    """The note `cid_or_path` names, resolved by `resolve_note_cid`; FileNotFoundError if it is gone."""
    cid = resolve_note_cid(storage, cid_or_path, resolver)
    note = storage.get_note(cid)
    if note is None:
        raise FileNotFoundError(f"Note not found: {cid_to_path(cid)}")
    return note


# Characters that end or split a link target in at least one of the forms a
# note can be linked by (`[[x#a|b]]`, `[x](<p>)`, inline code), so a note named
# with one could never be linked reliably. Obsidian forbids most of them too.
_FORBIDDEN_NAME_CHARS = frozenset("#|[]^`<>")


def _name_violation(rel_path: str) -> str | None:
    """Why no link could reliably address a note at `rel_path`, or None (§5.1)."""
    parts = normalize_rel(rel_path).split("/")
    for segment in parts:
        if segment != segment.strip():
            return f"folder and file names may not start or end with whitespace ('{segment}')"
        bad = sorted(_FORBIDDEN_NAME_CHARS.intersection(segment))
        if bad:
            return f"names may not contain {' '.join(bad)}, which links to the note could not carry"
    if DIR_RAW in parts[1:]:
        return f"a '{DIR_RAW}' folder inside a domain makes Markdown links to the note read as raw/ citations"
    return None


def _check_new_location(
    storage: NoteStoragePort,
    vault: Vault | None,
    rel_path: str,
    type_: str,
    *,
    verb: str,
    replacing: str | None = None,
) -> None:
    """Refuse a path no note may be created at (§2.1, §2.2, §5.1, §7.5).

    Scaffold and move share it, so both enforce one rule set: a path the scan
    would never index, a name links could not carry, a domain whose manifest
    is broken or whose `allowed_types` excludes the note, an occupied path,
    and a case-only twin of an existing note (a duplicate on macOS/Windows, a
    near-duplicate wikilink target on Linux). `replacing` names the note being
    moved, so a rename that only changes its own letter case is not a collision.
    """
    if vault is not None:
        why = vault.note_path_violation(rel_path, for_write=True)
    else:
        why = note_path_violation(rel_path, for_write=True)
    why = why or _name_violation(rel_path)
    if why:
        raise ValueError(f"Cannot {verb} at '{rel_path}': {why}")

    if vault is not None:
        domain = path_to_layer(rel_path)
        domain_def = vault.discover_domains().get(domain)
        if domain_def and domain_def.manifest_error:
            raise ValueError(
                f"Cannot {verb} into domain '{domain}': its {FILE_AGENTS} is invalid "
                f"({domain_def.manifest_error}). Fix the manifest first"
            )
        if domain_def and domain_def.allowed_types is not None and type_ not in domain_def.allowed_types:
            raise ValueError(
                f"Invalid note type '{type_}'. In domain '{domain}', allowed types are {domain_def.allowed_types}"
            )

    cid = path_to_cid(rel_path)
    others = [c for c in storage.list_note_cids() if c != replacing]
    if cid in others:
        raise FileExistsError(f"Note already exists at {rel_path}")
    clash = next((c for c in others if c.casefold() == cid.casefold()), None)
    if clash is not None:
        raise FileExistsError(
            f"Note already exists at {cid_to_path(clash)} (differs from '{rel_path}' only by letter case)"
        )
    # The listing walks real folders only; the storage also sees files reached
    # through a symlinked folder, which a write would otherwise overwrite.
    own_spelling = replacing is not None and cid.casefold() == replacing.casefold()
    if not own_spelling and storage.note_exists(cid):
        raise FileExistsError(f"Note already exists at {rel_path}")


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
        for name, value in (("title", title), ("description", description)):
            if not isinstance(value, str):
                raise ValueError(f"Note {name} must be a string")
        if path is not None and not isinstance(path, str):
            raise ValueError("Note path must be a string")

        if template:
            if body.strip():
                raise ValueError("Pass either 'body' or 'template', not both")
            if self.vault is None:
                raise ValueError("Templates require a vault; none is bound to this use case")
            body = self.vault.read_template_body(template).replace(
                TEMPLATE_TITLE_PLACEHOLDER, title
            )

        target_domain = domain or DIR_WIKI

        # An empty path is the same as none: some clients send "" for every
        # optional string they were not given.
        if path is not None and path.strip():
            raw_path = path.replace("\\", "/").strip()
            if raw_path.startswith("/") or Path(raw_path).is_absolute():
                raise ValueError(f"Path traversal detected in '{path}': absolute paths are forbidden")
            parts = [p for p in raw_path.strip("/").split("/") if p]
            if not parts or any(p in (".", "..") for p in parts):
                raise ValueError(f"Path traversal detected in '{path}': must not contain '.' or '..'")
            if raw_path.endswith("/"):
                # A trailing slash names a folder, as in vault_move_note.
                parts.append(sanitize_filename(title))
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

        _check_new_location(self.storage, self.vault, rel_path, type_, verb="scaffold")

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


# Written by the engine alone: `verified` only by vault_verify_note / cadabby
# verify, `generated` only at scaffold. A patch able to set them could mint a
# `human:*` attestation that vault_verify_note refuses, or erase the audit trail.
ENGINE_OWNED_FIELDS: tuple[str, ...] = ("verified", "generated")
_STRING_FIELDS: tuple[str, ...] = ("title", "description", "type", "status")


def _validate_frontmatter_patch(patch: dict[str, Any] | None) -> None:
    """Refuse a patch that touches engine-owned fields or gives a core field a non-string value (§3.4, §5.1)."""
    if patch is None:
        return
    if not isinstance(patch, dict):
        raise ValueError("patch_frontmatter must be an object of field: value pairs")
    owned = [k for k in ENGINE_OWNED_FIELDS if k in patch]
    if owned:
        entries = patch.get("verified")
        entries = entries if isinstance(entries, list) else [entries]
        if any(isinstance(e, dict) and str(e.get("by", "")).startswith("human:") for e in entries):
            raise HumanAttestationRefusedError(
                "A human:* attestation cannot be written by patching frontmatter; the person runs cadabby verify --human."
            )
        raise ValueError(
            f"{', '.join(owned)} {'is' if len(owned) == 1 else 'are'} engine-owned: attest with vault_verify_note "
            "(or cadabby verify), and never remove attestation history"
        )
    for key in _STRING_FIELDS:
        if patch.get(key) is not None and not isinstance(patch[key], str):
            raise ValueError(f"'{key}' must be a string")


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
        _validate_frontmatter_patch(frontmatter_patch)

        note = load_existing_note(self.storage, cid_or_path)

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

        note = load_existing_note(self.storage, cid_or_path)

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


def _shortest_wiki_target(cid: str, resolver: LinkTargetIndex) -> str:
    """The shortest wikilink text that opens `cid`: its stem, then longer path suffixes, then the CID."""
    parts = cid.split("/")
    for i in range(len(parts) - 1, -1, -1):
        candidate = "/".join(parts[i:])
        if resolver.resolve(candidate) == cid:
            return candidate
    return cid


def _markdown_path_text(path: str, angle: bool) -> str:
    """Spell a relative path so the link parser reads it back as the same path.

    The parser percent-decodes both forms and splits on the first `#`, so `%`
    and `#` are always encoded. A bare link also ends at whitespace or `)`, so
    it is percent-encoded whenever it carries one of those. A first segment
    that looks like `scheme:` gets `./`, or the link would read as a URL.
    """
    if RE_URL_SCHEME.match(path):
        path = "./" + path
    if angle:
        return path.replace("%", "%25").replace("#", "%23")
    if not any(ch.isspace() or ch in "()%#<>" for ch in path):
        return path
    return quote(path, safe="/._-~!$&'*+,;=:@")


def _apply_spans(body: str, edits: list[tuple[LinkSpan, str]]) -> str:
    """Replace each span's target text, leaving every other byte of the body as it was.

    `edits` never overlap: `MoveNoteUseCase._link_edits` is the one place that
    drops a link nested inside another being rewritten.
    """
    out: list[str] = []
    cursor = 0
    for span, text in sorted(edits, key=lambda e: e[0].start):
        out.append(body[cursor : span.start])
        out.append(text)
        cursor = span.end
    out.append(body[cursor:])
    return "".join(out)


def _reparse(span: LinkSpan, text: str) -> LinkSpan | None:
    """How the parser reads `span`'s link once its target is `text`; None if it no longer reads as one link."""
    if span.kind == LINK_KIND_WIKI:
        anchor = f"#{span.anchor}" if span.anchor else ""
        alias = f"|{span.alias}" if span.alias else ""
        probe = f"{'!' if span.embed else ''}[[{text}{anchor}{alias}]]"
    else:
        _, sep, anchor = span.target_raw.partition("#")
        dest = f"{text}{sep}{anchor}"
        dest = f"<{dest}>" if span.angle else dest
        probe = {
            LINK_KIND_MARKDOWN: f"[x]({dest})",
            LINK_KIND_IMAGE: f"![x]({dest})",
            LINK_KIND_REFDEF: f"[x]: {dest}",
        }[span.kind]
    found = [s for s in (*iter_link_spans(probe), *iter_relative_path_spans(probe)) if s.kind == span.kind]
    return found[0] if len(found) == 1 else None


@dataclass
class _Rewrite:
    original: Note  # as read, carrying its concurrency token
    updated: Note
    links: int


@dataclass
class MovePlan:
    """Everything a move would write, computed from the notes as they are now (§5.1).

    `token` fingerprints it: a dry-run hands it to the user, and an apply that
    passes it back is refused if the plan it recomputes is any different.
    """

    source: Note
    destination: Note
    moved_links: int = 0
    rewrites: list[_Rewrite] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    newly_resolved: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def links_rewritten(self) -> int:
        return self.moved_links + sum(r.links for r in self.rewrites)

    @property
    def token(self) -> str:
        """A fingerprint of what this plan would write and refuse, over the exact bytes it read."""

        def digest(text: str) -> str:
            return hashlib.sha256(text.encode("utf-8")).hexdigest()

        state = {
            "source": [self.source.cid, str(self.source.source_hash)],
            "destination": [self.destination.cid, digest(self.destination.serialize())],
            "rewrites": sorted(
                [r.original.cid, str(r.original.source_hash), digest(r.updated.serialize())] for r in self.rewrites
            ),
            "blocked": sorted(self.blocked),
        }
        return digest(json.dumps(state, sort_keys=True))[:32]

    def to_dict(self, *, applied: bool) -> dict[str, Any]:
        def impact(cid: str, links: int, before: Note, after: Note) -> dict[str, Any]:
            return {"cid": cid, "links_rewritten": links, "tier_before": before.trust_tier, "tier_after": after.trust_tier}

        return {
            "from": self.source.cid,
            "to": self.destination.cid,
            "applied": applied,
            "plan_token": self.token,
            "moved_note": impact(self.destination.cid, self.moved_links, self.source, self.destination),
            "notes": [impact(r.original.cid, r.links, r.original, r.updated) for r in self.rewrites],
            "blocked": list(self.blocked),
            "newly_resolved": list(self.newly_resolved),
            "warnings": list(self.warnings),
        }


class MoveNoteUseCase:
    """Driving use case for moving or renaming a note and repointing every link to it (§5.1).

    Only links that would otherwise break or open a different note are
    rewritten, because each rewrite changes a body and so unbinds that note's
    attestations. The moved note keeps its tier unless its own relative paths
    must change: verification binds to content, not to the path (§3.4).
    """

    def __init__(self, storage: NoteStoragePort, ledger: LedgerPort, vault: Vault | None = None) -> None:
        self.storage = storage
        self.ledger = ledger
        self.vault = vault

    def plan(self, cid_or_path: str, to: str, approve_stale: Sequence[str] = ()) -> MovePlan:
        cids = list(self.storage.list_note_cids())
        before = LinkTargetIndex(cids)
        source = load_existing_note(self.storage, cid_or_path, before)
        new_rel = self._destination(source, to, cids)
        new_cid = path_to_cid(new_rel)
        plan = MovePlan(source=source, destination=replace(source, cid=new_cid, rel_path=new_rel))
        after = LinkTargetIndex([c for c in cids if c != source.cid] + [new_cid])
        approved = self._approvals(approve_stale, source.cid, new_cid, before)

        for cid in cids:
            note = source if cid == source.cid else self._read(cid, plan, before, after)
            if note is None:
                continue
            edits = self._link_edits(note, source.cid, new_cid, before, after, plan)
            if not edits:
                continue
            body = _apply_spans(note.body, edits)
            if cid == source.cid:
                plan.destination = replace(plan.destination, body=body)
                plan.moved_links = len(edits)
            else:
                plan.rewrites.append(_Rewrite(original=note, updated=replace(note, body=body), links=len(edits)))
            if note.trust_tier == "human-reviewed" and cid not in approved:
                plan.blocked.append(
                    f"{note.rel_path} is human-reviewed; rewriting its links would make it stale-verified. "
                    f"Ask the user, then pass it in approve_stale"
                )
        return plan

    def apply(self, plan: MovePlan, actor: str) -> None:
        """Write the plan destination-first, so a crash leaves a duplicate, never a lost note."""
        if plan.blocked:
            raise ValueError("Move refused: " + "; ".join(plan.blocked))
        source, destination = plan.source, plan.destination
        # On a case-insensitive filesystem both spellings are one file, so
        # write-then-delete would delete it: retire the old name first instead.
        case_only = source.cid.casefold() == destination.cid.casefold()
        undo: list[Callable[[], None]] = []
        try:
            if case_only:
                self.storage.delete_note(source.cid, expected_hash=source.source_hash)
                undo.append(lambda: self.storage.save_note(source))
            if self.storage.note_exists(destination.cid):
                raise FileExistsError(f"Note already exists at {destination.rel_path}")
            self.storage.save_note(destination)
            undo.append(self._undo_create(destination.cid))
            for rw in plan.rewrites:
                self.storage.save_note(rw.updated, expected_hash=rw.original.source_hash)
                undo.append(self._undo_rewrite(rw))
            if not case_only:
                self.storage.delete_note(source.cid, expected_hash=source.source_hash)
        except Exception as exc:
            failures = []
            for step in reversed(undo):
                try:
                    step()
                except Exception as undo_exc:  # noqa: BLE001 -- report every file left behind
                    failures.append(str(undo_exc))
            if failures:
                raise RuntimeError(f"{exc}; rollback incomplete, check by hand: {'; '.join(failures)}") from exc
            raise

        touched = len(plan.rewrites) + (1 if plan.moved_links else 0)
        self.ledger.append(
            f"Moved {source.rel_path} -> {destination.rel_path} "
            f"(rewrote {plan.links_rewritten} links in {touched} notes)",
            actor=actor,
        )

    def _destination(self, source: Note, to: str, cids: Sequence[str]) -> str:
        if not isinstance(to, str) or not to.strip():
            raise ValueError(f"Move destination must be a vault-relative path, got '{to}'")
        raw = to.replace("\\", "/").strip()
        if raw.startswith("/") or Path(raw).is_absolute():
            raise ValueError(f"Move destination must be a vault-relative path, got '{to}'")
        raw_parts = [p for p in raw.strip("/").split("/") if p]
        if not raw_parts or any(p in (".", "..") for p in raw_parts):
            raise ValueError(f"Path traversal detected in '{to}': must not contain '.' or '..'")
        if raw.endswith("/"):
            raw_parts.append(posixpath.basename(source.rel_path))
        else:
            if "/" not in raw:  # a bare name renames in place
                parent = posixpath.dirname(source.rel_path)
                if parent:
                    raw_parts.insert(0, parent)
            folder = "/".join(raw_parts)
            is_folder = any(c.startswith(folder + "/") for c in cids) or (
                self.vault is not None and (self.vault.root / folder).is_dir()
            )
            if is_folder and not raw.lower().endswith(".md"):
                raise ValueError(
                    f"'{to}' is an existing folder: end it with '/' to move the note into it, "
                    f"or add '.md' to name a file"
                )
        rel = cid_to_path("/".join(raw_parts))
        if path_to_cid(rel) == source.cid:
            raise ValueError(f"'{source.rel_path}' is already at '{rel}'")
        _check_new_location(self.storage, self.vault, rel, source.type, verb="move", replacing=source.cid)
        return rel

    def _approvals(self, items: Sequence[str], source_cid: str, new_cid: str, resolver: LinkTargetIndex) -> set[str]:
        """The CIDs the user agreed may go stale. The moved note may be named by where it is going."""
        approved: set[str] = set()
        for item in items:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("approve_stale entries must be non-empty note names")
            named = normalize_link_target(item.strip().replace("\\", "/"))
            while named.startswith("./"):
                named = named[2:]
            if named and path_to_cid(cid_to_path(named)) == new_cid:
                approved.add(source_cid)
            else:
                approved.add(resolve_note_cid(self.storage, item, resolver))
        return approved

    def _read(self, cid: str, plan: MovePlan, before: LinkTargetIndex, after: LinkTargetIndex) -> Note | None:
        """The note, or None. An unparseable note blocks only if its raw text links to the moved note."""
        try:
            return self.storage.get_note(cid)
        except Exception as exc:  # noqa: BLE001 -- an unreadable note never aborts the plan
            rel = cid_to_path(cid)
            try:
                raw = self.storage.read_raw(cid)
            except Exception:  # noqa: BLE001 -- unreadable even as text: block below
                raw = None
            if raw is None:
                plan.blocked.append(f"{rel} cannot be read, so its links cannot be checked ({exc})")
                return None
            try:
                _, body = split_frontmatter(raw)
            except FrontmatterParseError:
                body = raw
            scratch = MovePlan(source=plan.source, destination=plan.destination)
            probe = Note(cid=cid, rel_path=rel, body=body)
            if self._link_edits(probe, plan.source.cid, plan.destination.cid, before, after, scratch) or scratch.blocked:
                plan.blocked.append(f"{rel} cannot be parsed ({exc}) and links to the moved note; fix it first")
            else:
                plan.warnings.append(f"{rel} cannot be parsed ({exc}); read as raw text, none of its links change")
            return None

    @staticmethod
    def _link_edits(
        note: Note,
        old_cid: str,
        new_cid: str,
        before: LinkTargetIndex,
        after: LinkTargetIndex,
        plan: MovePlan,
    ) -> list[tuple[LinkSpan, str]]:
        """The target rewrites a move makes in `note`, each checked to read back as the same link."""
        raw_edits: list[tuple[LinkSpan, str]] = []
        moving = note.cid == old_cid
        for span in (*iter_link_spans(note.body), *iter_relative_path_spans(note.body)):
            if span.kind == LINK_KIND_WIKI:
                was, will = before.resolve(span.target), after.resolve(span.target)
                if was == old_cid:
                    if will != new_cid:
                        text = _shortest_wiki_target(new_cid, after)
                        if span.embed and is_attachment_target(text):
                            text += ".md"  # `![[v1.5]]` reads as an attachment, `![[v1.5.md]]` as the note
                        raw_edits.append((span, text))
                elif was != will and was is None:
                    plan.newly_resolved.append(f"{note.rel_path}: [[{span.target}]] was dead and would open {new_cid}")
                elif was != will:
                    plan.blocked.append(
                        f"{note.rel_path}: [[{span.target}]] opens {was} today but would open {will} after the move"
                    )
            elif moving:
                # The moved note's own relative paths keep naming the same file from the new folder.
                if not span.target or posixpath.normpath(span.target) == ".":
                    continue
                target = resolve_relative_path(old_cid, span.target)
                if target is None:
                    continue
                if target == cid_to_path(old_cid):
                    target = cid_to_path(new_cid)
                if resolve_relative_path(new_cid, span.target) == target:
                    continue
                text = posixpath.relpath(target, posixpath.dirname(cid_to_path(new_cid)) or ".")
                raw_edits.append((span, _markdown_path_text(text, span.angle)))
            elif span.kind != LINK_KIND_IMAGE and span.target.lower().endswith(".md") and not is_raw_path(span.target):
                target_cid = resolve_markdown_target(note.cid, span.target_raw)
                if target_cid == old_cid:
                    raw_edits.append((span, _markdown_path_text(relative_note_path(note.cid, new_cid), span.angle)))
                elif target_cid == new_cid:
                    plan.newly_resolved.append(f"{note.rel_path}: ({span.target}) was dead and would open {new_cid}")

        edits: list[tuple[LinkSpan, str]] = []
        cursor = 0
        for span, text in sorted(raw_edits, key=lambda e: (e[0].start, -e[0].end)):
            if span.start < cursor:
                continue  # nested inside a link already being rewritten, which carries it along
            edits.append((span, text))
            cursor = span.end
        for span, text in edits:
            if not MoveNoteUseCase._reads_back(note, span, text, old_cid, new_cid, after):
                plan.blocked.append(
                    f"{note.rel_path}: '{span.target_raw}' rewritten as '{text}' would no longer open the same file; "
                    f"choose a destination name links can carry"
                )
        return edits

    @staticmethod
    def _reads_back(note: Note, span: LinkSpan, text: str, old_cid: str, new_cid: str, after: LinkTargetIndex) -> bool:
        """Whether the rewritten link still parses as one link of its kind, naming what it should."""
        probe = _reparse(span, text)
        if probe is None:
            return False
        if span.kind == LINK_KIND_WIKI:
            return after.resolve(probe.target) == new_cid
        if note.cid == old_cid:
            expected = resolve_relative_path(old_cid, span.target)
            if expected == cid_to_path(old_cid):
                expected = cid_to_path(new_cid)
            return resolve_relative_path(new_cid, probe.target) == expected and (
                is_raw_path(probe.target) == is_raw_path(span.target)
            )
        return (
            probe.target.lower().endswith(".md")
            and not is_raw_path(probe.target)
            and resolve_markdown_target(note.cid, probe.target_raw) == new_cid
        )

    def _undo_create(self, cid: str) -> Callable[[], None]:
        written = self._token(cid)
        return lambda: self.storage.delete_note(cid, expected_hash=written, prune_empty_dirs=True)

    def _undo_rewrite(self, rw: _Rewrite) -> Callable[[], None]:
        written = self._token(rw.updated.cid)
        return lambda: self.storage.save_note(rw.original, expected_hash=written)

    def _token(self, cid: str) -> str | None:
        """The concurrency token of what was just written, so an undo never clobbers a newer edit."""
        note = self.storage.get_note(cid)
        return note.source_hash if note is not None else None


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
        _refresh(cache)
    return plan.to_dict(applied=apply)

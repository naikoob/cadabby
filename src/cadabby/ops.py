"""High-level atomic vault operations: scaffold, update, move, verify, and ground.

Conforms strictly to Cadabby Technical Specification §3, §5.1, §6, §8.
Implements the Driving Use Cases layer in Hexagonal Architecture.
"""

from __future__ import annotations

import copy
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
from cadabby.cache import VaultCache, is_raw_text_path, moc_overview
from cadabby.constants import ACTOR_PATTERN, DIR_RAW, DIR_WIKI, FILE_AGENTS, TEMPLATE_TITLE_PLACEHOLDER
from cadabby.domain import (
    Note,
    VerificationResult,
    extract_headings,
    extract_markdown_section,
    split_markdown_sections,
)
from cadabby.errors import AmbiguousNoteError, HumanAttestationRefusedError, VaultConflictError, classify
from cadabby.frontmatter import FrontmatterParseError, serialize_frontmatter, split_frontmatter
from cadabby.graph import (
    LINK_KIND_IMAGE,
    LINK_KIND_MARKDOWN,
    LINK_KIND_REFDEF,
    LINK_KIND_WIKI,
    RE_URL_SCHEME,
    get_note_graph,
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
    split_link_target_and_anchor,
    strip_link_wrapper,
)
from cadabby.okf import is_valid_actor, utc_now_iso
from cadabby.ports import IndexCachePort, LedgerPort, NoteStoragePort
from cadabby.vault import Vault, cid_to_path, normalize_rel, note_path_violation, path_to_cid, path_to_layer


def sanitize_filename(title: str) -> str:
    """Lowercase kebab-case stem for a title (STYLE.md); 'untitled' if nothing survives."""
    slug = re.sub(r"[^\w\s-]", "", title).strip().lower()
    slug = re.sub(r"[-\s_]+", "-", slug).strip("-")
    return slug or "untitled"


def _strip_dot_slash(text: str) -> str:
    """Normalize backslashes to '/' and strip leading './' prefixes."""
    raw = text.strip().replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    return raw


def _vault_name_parts(text: str) -> tuple[str, list[str]] | None:
    """`(normalized, segments)` for a vault-relative name, or None if it is absolute or climbs.

    The one reading of a name the caller typed, shared by note resolution,
    folder resolution and destinations: backslashes become '/', leading `./`
    is dropped, and a trailing '/' survives in `normalized` (it means "folder").
    """
    raw = _strip_dot_slash(text)
    if not raw or raw.startswith("/") or Path(raw).is_absolute():
        return None
    parts = [p for p in raw.strip("/").split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        return None
    return raw, parts


def _parse_relative_target(raw_input: str, *, absolute_error: str) -> tuple[str, list[str]]:
    """Validate and split a vault-relative target path into (normalized_raw, parts)."""
    if not isinstance(raw_input, str) or not raw_input.strip():
        raise ValueError(absolute_error)
    stripped = _strip_dot_slash(raw_input)
    if stripped.startswith("/") or Path(stripped).is_absolute():
        raise ValueError(absolute_error)
    parsed = _vault_name_parts(raw_input)
    if parsed is None:
        raise ValueError(f"Path traversal detected in '{raw_input}': must not contain '.' or '..'")
    return parsed


def with_md_suffix(name: str) -> str:
    """`name` as a note path: a `.md` suffix in any letter case becomes `.md`, else one is added.

    `Foo.MD` would otherwise become `Foo.MD.md`, or a file the scan ignores.
    """
    return f"{name[:-3]}.md" if name.lower().endswith(".md") else f"{name}.md"


def _match_listed_path(cids: Sequence[str], raw: str, shown: str) -> str | None:
    """The listed CID `raw` names as a path -- exactly, else uniquely ignoring case (§5.1 steps 1-2).

    None when neither matches; AmbiguousNoteError when several notes differ
    from it only by letter case.
    """
    exact = path_to_cid(cid_to_path(raw))
    if exact in cids:
        return exact
    folded = sorted(c for c in cids if c.casefold() == exact.casefold())
    if len(folded) > 1:
        raise AmbiguousNoteError(shown, folded)
    return folded[0] if folded else None


def _require_actor(actor: str) -> None:
    """Refuse an actor outside the §3.4 grammar before anything is written.

    Every write ends in a ledger entry attributed to `actor`; checking only at
    the ledger would leave the note written and the call failed.
    """
    if not is_valid_actor(actor):
        raise ValueError(f"Invalid actor format {actor!r}. Must match {ACTOR_PATTERN}")


def resolve_note_cid(
    storage: NoteStoragePort,
    cid_or_path: str,
    resolver: LinkTargetIndex | None = None,
    *,
    cids: Sequence[str] | None = None,
) -> str:
    """The canonical CID of an existing note named by path, stem, or wikilink (§5.1).

    The result is always a member of `storage.list_note_cids()`. That matters
    on case-insensitive filesystems, where the storage would happily open
    `wiki/flash-attention.md` for `wiki/Flash-Attention.md` and then name the
    note after the caller's spelling. Order: the exact path (so a filename
    containing `#` or `|` still resolves), a unique letter-case-insensitive
    path, then the name read as a wikilink. A name that could mean several
    notes raises AmbiguousNoteError rather than picking one. Callers resolving
    many names pass `cids` (and `resolver`) once instead of relisting per name.
    """
    if not isinstance(cid_or_path, str) or not cid_or_path.strip():
        raise ValueError("Note CID or path must be a non-empty string")
    shown = cid_or_path.strip()
    parsed = _vault_name_parts(cid_or_path)
    if parsed is None:
        raise FileNotFoundError(f"Note not found: {shown} (names must be vault-relative)")
    raw = parsed[0]

    listed = list(cids) if cids is not None else list(storage.list_note_cids())
    if (hit := _match_listed_path(listed, raw, shown)) is not None:
        return hit

    clean = normalize_link_target(raw)
    found = (resolver if resolver is not None else LinkTargetIndex(listed)).candidates(clean) if clean else []
    if len(found) == 1:
        return found[0]
    if found:
        raise AmbiguousNoteError(shown, found)
    raise FileNotFoundError(f"Note not found: {shown}")


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
    replacing_group: frozenset[str] = frozenset(),
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
    excluded = replacing_group | ({replacing} if replacing is not None else set())
    others = [c for c in storage.list_note_cids() if c not in excluded]
    if cid in others:
        raise FileExistsError(f"Note already exists at {rel_path}")
    clash = next((c for c in others if c.casefold() == cid.casefold()), None)
    if clash is not None:
        raise FileExistsError(
            f"Note already exists at {cid_to_path(clash)} (differs from '{rel_path}' only by letter case)"
        )
    # The listing walks real folders only; the storage also sees files reached
    # through a symlinked folder, which a write would otherwise overwrite.
    own_spelling = any(cid.casefold() == r.casefold() for r in excluded)
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
            if self.vault is None:
                raise ValueError("Templates require a vault; none is bound to this use case")
            body = self.vault.read_template_body(template).replace(
                TEMPLATE_TITLE_PLACEHOLDER, title
            )

        rel_path, cid = self._resolve_scaffold_destination(title, domain, path)
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
class _MovedNote:
    source: Note
    destination: Note
    links: int = 0


def _write_steps(moved: list[_MovedNote]) -> list[tuple[str, _MovedNote]]:
    """Order a move's ("write" | "retire", item) steps so no note is ever absent from disk (§5.1).

    A destination still occupied by another moving note's source is written only
    after that source has been copied to its own destination and retired, so each
    collision chain of an overlapping subtree shift (`a/` -> `a/b/`) runs from its
    tail. The one retire-first step is a case-only rename, whose source and
    destination are a single file on a case-insensitive filesystem (§5.1 allows
    it). Sources that block nothing are retired later, after the rewrites.
    Paths compare case-insensitively, as the filesystem may. A cycle cannot arise
    from a prefix shift; one would raise here, before anything is written.
    """
    def fold(note: Note) -> str:
        return note.cid.casefold()

    dest_keys = {fold(m.destination) for m in moved}
    pending = list(moved)
    live_sources = {fold(m.source): m for m in moved}
    steps: list[tuple[str, _MovedNote]] = []
    while pending:
        progressed = False
        for item in list(pending):
            occupant = live_sources.get(fold(item.destination))
            if occupant is not None and occupant is not item:
                continue  # its destination still holds another note's only copy
            if occupant is item:  # case-only rename: one file, so retire, then write
                steps.append(("retire", item))
                live_sources.pop(fold(item.source))
            steps.append(("write", item))
            if fold(item.source) in live_sources and fold(item.source) in dest_keys:
                # Now copied, its old path is someone's destination: free it.
                steps.append(("retire", item))
                live_sources.pop(fold(item.source))
            pending.remove(item)
            progressed = True
        if not progressed:
            raise ValueError(
                "Move refused: destinations form a cycle ("
                + ", ".join(f"{m.source.cid} -> {m.destination.cid}" for m in pending)
                + "); move in two steps through a temporary name"
            )
    return steps


@dataclass
class MovePlan:
    """Everything a move would write, computed from the notes as they are now (§5.1).

    `token` fingerprints it: a dry-run hands it to the user, and an apply that
    passes it back is refused if the plan it recomputes is any different.
    """

    moved: list[_MovedNote]
    from_target: str = ""
    to_target: str = ""
    is_folder: bool = False
    rewrites: list[_Rewrite] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    newly_resolved: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def source(self) -> Note:
        return self.moved[0].source

    @property
    def destination(self) -> Note:
        return self.moved[0].destination

    @property
    def moved_links(self) -> int:
        return sum(m.links for m in self.moved)

    @property
    def links_rewritten(self) -> int:
        return self.moved_links + sum(r.links for r in self.rewrites)

    @property
    def token(self) -> str:
        """A fingerprint of what this plan would write and refuse, over the exact bytes it read."""

        def digest(text: str) -> str:
            return hashlib.sha256(text.encode("utf-8")).hexdigest()

        base: dict[str, Any] = {
            "rewrites": sorted(
                [r.original.cid, str(r.original.source_hash), digest(r.updated.serialize())] for r in self.rewrites
            ),
            "blocked": sorted(self.blocked),
        }
        if not self.is_folder and len(self.moved) == 1:
            state = {
                **base,
                "source": [self.source.cid, str(self.source.source_hash)],
                "destination": [self.destination.cid, digest(self.destination.serialize())],
            }
        else:
            state = {
                **base,
                "from": self.from_target,
                "to": self.to_target,
                "moved": [
                    [m.source.cid, str(m.source.source_hash), m.destination.cid, digest(m.destination.serialize())]
                    for m in self.moved
                ],
            }
        return digest(json.dumps(state, sort_keys=True))[:32]

    def to_dict(self, *, applied: bool) -> dict[str, Any]:
        def impact(cid: str, links: int, before: Note, after: Note) -> dict[str, Any]:
            return {"cid": cid, "links_rewritten": links, "tier_before": before.trust_tier, "tier_after": after.trust_tier}

        first = self.moved[0]
        return {
            "from": self.from_target or self.source.cid,
            "to": self.to_target or self.destination.cid,
            "applied": applied,
            "plan_token": self.token,
            "moved_note": impact(first.destination.cid, first.links, first.source, first.destination),
            "moved_notes": [
                {**impact(m.destination.cid, m.links, m.source, m.destination), "from": m.source.cid}
                for m in self.moved
            ],
            "notes": [impact(r.original.cid, r.links, r.original, r.updated) for r in self.rewrites],
            "blocked": list(self.blocked),
            "newly_resolved": list(self.newly_resolved),
            "warnings": list(self.warnings),
        }


class MoveNoteUseCase:
    """Driving use case for moving or renaming a note or folder subtree and repointing every link to it (§5.1).

    Only links that would otherwise break or open a different note are
    rewritten, because each rewrite changes a body and so unbinds that note's
    attestations. Moved notes keep their tier unless their own relative paths
    must change: verification binds to content, not to the path (§3.4).
    """

    def __init__(self, storage: NoteStoragePort, ledger: LedgerPort, vault: Vault | None = None) -> None:
        self.storage = storage
        self.ledger = ledger
        self.vault = vault

    def plan(self, cid_or_path: str, to: str, approve_stale: Sequence[str] = ()) -> MovePlan:
        cids = list(self.storage.list_note_cids())
        before = LinkTargetIndex(cids)
        # §5.1 order: exact path, case-insensitive path, wikilink reading, and
        # only then a folder prefix -- so `move projects` means the note
        # `wiki/projects` when there is one, as it does for update and verify.
        # A trailing '/' always names a folder.
        source: Note | None = None
        folder_prefix = None
        if isinstance(cid_or_path, str) and cid_or_path.rstrip().endswith("/"):
            folder_prefix = self._folder_prefix(cid_or_path, cids)
            if folder_prefix is None:
                raise FileNotFoundError(f"Note not found: {cid_or_path.strip()} (names must be vault-relative)")
        else:
            try:
                source = load_existing_note(self.storage, cid_or_path, before)
            except FileNotFoundError:
                folder_prefix = self._folder_prefix(cid_or_path, cids)
                if folder_prefix is None:
                    raise
        if folder_prefix is not None:
            src_prefix, folder_cids = folder_prefix
            dst_prefix, moved_items, unreadable = self._folder_destinations(src_prefix, folder_cids, to)
            plan = MovePlan(
                moved=moved_items,
                from_target=f"{src_prefix}/",
                to_target=f"{dst_prefix}/",
                is_folder=True,
                blocked=unreadable,
            )
            leftover = self._leftover_folder_files(src_prefix, folder_cids)
            if leftover:
                sample = ", ".join(leftover[:3])
                more = f" (+{len(leftover) - 3} more)" if len(leftover) > 3 else ""
                plan.warnings.append(
                    f"Non-note files under '{src_prefix}/' are not moved and will remain in place: {sample}{more}"
                )
        else:
            assert source is not None
            new_rel = self._destination(source, to, cids)
            new_cid = path_to_cid(new_rel)
            plan = MovePlan(
                moved=[_MovedNote(source=source, destination=replace(source, cid=new_cid, rel_path=new_rel))]
            )

        moved_by_cid = {m.source.cid: m for m in plan.moved}
        moves = {m.source.cid: m.destination.cid for m in plan.moved}
        after = LinkTargetIndex([c for c in cids if c not in moves] + list(moves.values()))
        approved = self._approvals(approve_stale, moves, before)

        for cid in cids:
            moving_item = moved_by_cid.get(cid)
            note = moving_item.source if moving_item is not None else self._read(cid, plan, moves, before, after)
            if note is None:
                continue
            edits = self._link_edits(note, moves, before, after, plan.newly_resolved, plan.blocked)
            if not edits:
                continue
            body = _apply_spans(note.body, edits)
            if moving_item is not None:
                moving_item.destination = replace(moving_item.destination, body=body)
                moving_item.links = len(edits)
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
        _require_actor(actor)
        if plan.blocked:
            raise ValueError("Move refused: " + "; ".join(plan.blocked))
        steps = _write_steps(plan.moved)  # computed before any write; raises on a cycle
        undo: list[Callable[[], None]] = []
        retired: set[str] = set()
        try:
            for action, item in steps:
                if action == "retire":
                    self._retire_source(item.source, undo)
                    retired.add(item.source.cid)
                    continue
                destination = item.destination
                if self.storage.note_exists(destination.cid):
                    raise FileExistsError(f"Note already exists at {destination.rel_path}")
                self.storage.save_note(destination)
                undo.append(self._undo_create(destination.cid))
            for rw in plan.rewrites:
                self.storage.save_note(rw.updated, expected_hash=rw.original.source_hash)
                undo.append(self._undo_rewrite(rw))
            for item in plan.moved:
                if item.source.cid not in retired:
                    self._retire_source(item.source, undo)
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

        touched = len(plan.rewrites) + sum(1 for m in plan.moved if m.links)
        if plan.is_folder:
            self.ledger.append(
                f"Moved {plan.from_target} -> {plan.to_target} "
                f"({len(plan.moved)} notes; rewrote {plan.links_rewritten} links in {touched} notes)",
                actor=actor,
            )
        else:
            self.ledger.append(
                f"Moved {plan.source.rel_path} -> {plan.destination.rel_path} "
                f"(rewrote {plan.links_rewritten} links in {touched} notes)",
                actor=actor,
            )

    def _retire_source(self, source: Note, undo: list[Callable[[], None]]) -> None:
        self.storage.delete_note(source.cid, expected_hash=source.source_hash, prune_empty_dirs=True)
        undo.append(lambda s=source: self.storage.save_note(s))

    @staticmethod
    def _parse_destination_path(to: str) -> tuple[str, list[str]]:
        return _parse_relative_target(
            to,
            absolute_error=f"Move destination must be a vault-relative path, got '{to}'",
        )

    @staticmethod
    def _folder_prefix(cid_or_path: str, cids: Sequence[str]) -> tuple[str, list[str]] | None:
        if not isinstance(cid_or_path, str) or not cid_or_path.strip():
            return None
        parsed = _vault_name_parts(cid_or_path)
        if parsed is None:
            return None
        raw, parts = parsed
        prefix = "/".join(parts)
        if not raw.endswith("/"):
            if raw.lower().endswith(".md"):
                return None
            exact = path_to_cid(cid_to_path(raw))
            if exact in cids or any(c.casefold() == exact.casefold() for c in cids):
                return None
        matches = sorted(c for c in cids if c.startswith(prefix + "/"))
        if matches:
            return prefix, matches
        folded = sorted(c for c in cids if c.casefold().startswith(prefix.casefold() + "/"))
        if folded:
            depth = len(parts)
            real_prefixes = sorted({"/".join(c.split("/")[:depth]) for c in folded})
            if len(real_prefixes) == 1:
                return real_prefixes[0], folded
            raise AmbiguousNoteError(cid_or_path.strip(), folded)
        if raw.endswith("/"):
            raise FileNotFoundError(f"Note not found: {cid_or_path.strip()} (no notes under '{prefix}/')")
        return None

    def _leftover_folder_files(self, src_prefix: str, folder_cids: Sequence[str]) -> list[str]:
        if self.vault is None:
            return []
        src_dir = self.vault.root / src_prefix
        if not src_dir.is_dir():
            return []
        moving_rels = {cid_to_path(c).casefold() for c in folder_cids}
        leftover: list[str] = []
        for path in sorted(src_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.vault.root).as_posix()
            if any(part.startswith(".") for part in rel.split("/")):
                continue
            if rel.casefold() not in moving_rels:
                leftover.append(rel)
        return leftover

    def _folder_destinations(
        self, src_prefix: str, folder_cids: list[str], to: str
    ) -> tuple[str, list[_MovedNote], list[str]]:
        """Each note's destination, plus a blocker per note whose frontmatter does not parse (§5.1).

        A note that cannot be parsed is never written (§3.2), so it cannot be
        moved; reporting it as a blocker lets the dry run list it beside every
        other reason instead of failing on the first such file.
        """
        raw, raw_parts = self._parse_destination_path(to)
        if raw.lower().endswith(".md"):
            raise ValueError(f"Cannot move folder '{src_prefix}/' to a single file '{to}'")
        dst_prefix = "/".join(raw_parts)
        if dst_prefix == src_prefix:
            raise ValueError(f"'{src_prefix}/' is already at '{dst_prefix}/'")

        moving_set = frozenset(folder_cids)
        moved_items: list[_MovedNote] = []
        unreadable: list[str] = []
        for old_cid in folder_cids:
            try:
                src_note = self.storage.get_note(old_cid)
            except FrontmatterParseError as exc:
                unreadable.append(f"{cid_to_path(old_cid)} cannot be parsed ({exc}); fix it or move it out first")
                continue
            if src_note is None:
                raise FileNotFoundError(f"Note not found: {cid_to_path(old_cid)}")
            suffix = old_cid[len(src_prefix) + 1 :]
            new_rel = cid_to_path(f"{dst_prefix}/{suffix}")
            new_cid = path_to_cid(new_rel)
            _check_new_location(
                self.storage,
                self.vault,
                new_rel,
                src_note.type,
                verb="move",
                replacing=old_cid,
                replacing_group=moving_set,
            )
            moved_items.append(
                _MovedNote(
                    source=src_note,
                    destination=replace(src_note, cid=new_cid, rel_path=new_rel),
                )
            )
        return dst_prefix, moved_items, unreadable

    def _destination(self, source: Note, to: str, cids: Sequence[str]) -> str:
        raw, raw_parts = self._parse_destination_path(to)
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
        rel = with_md_suffix("/".join(raw_parts))
        if path_to_cid(rel) == source.cid:
            raise ValueError(f"'{source.rel_path}' is already at '{rel}'")
        _check_new_location(self.storage, self.vault, rel, source.type, verb="move", replacing=source.cid)
        return rel

    def _approvals(self, items: Sequence[str], moves: dict[str, str], resolver: LinkTargetIndex) -> set[str]:
        """The CIDs the user agreed may go stale. Moved notes or folders may be named by where they are going."""
        approved: set[str] = set()
        new_to_old = {v: k for k, v in moves.items()}
        for item in items:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("approve_stale entries must be non-empty note names")
            named = _strip_dot_slash(normalize_link_target(item.strip()))
            dest_cid = path_to_cid(cid_to_path(named)) if named else ""
            if dest_cid in new_to_old:
                approved.add(new_to_old[dest_cid])
                continue
            pfx = named.strip("/")
            if pfx:
                folder_hits = [
                    old_cid
                    for old_cid, new_cid in moves.items()
                    if old_cid.casefold().startswith(pfx.casefold() + "/")
                    or new_cid.casefold().startswith(pfx.casefold() + "/")
                ]
                if folder_hits:
                    approved.update(folder_hits)
                    continue
            approved.add(resolve_note_cid(self.storage, item, resolver))
        return approved

    def _read(
        self,
        cid: str,
        plan: MovePlan,
        moves: dict[str, str],
        before: LinkTargetIndex,
        after: LinkTargetIndex,
    ) -> Note | None:
        """The note, or None. An unparseable note blocks only if its raw text links to a moved note."""
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
            scratch_blocked: list[str] = []
            probe = Note(cid=cid, rel_path=rel, body=body)
            if self._link_edits(probe, moves, before, after, [], scratch_blocked) or scratch_blocked:
                plan.blocked.append(f"{rel} cannot be parsed ({exc}) and links to the moved note; fix it first")
            else:
                plan.warnings.append(f"{rel} cannot be parsed ({exc}); read as raw text, none of its links change")
            return None

    @staticmethod
    def _expected_relative_target(
        old_cid: str,
        raw_target: str,
        old_paths_to_new: dict[str, str],
    ) -> str | None:
        target = resolve_relative_path(old_cid, raw_target)
        if target is None:
            return None
        return old_paths_to_new.get(target, target)

    @staticmethod
    def _link_edits(
        note: Note,
        moves: dict[str, str],
        before: LinkTargetIndex,
        after: LinkTargetIndex,
        newly_resolved: list[str],
        blocked: list[str],
    ) -> list[tuple[LinkSpan, str]]:
        """The target rewrites a move makes in `note`, each checked to read back as the same link."""
        raw_edits: list[tuple[LinkSpan, str]] = []
        moving = note.cid in moves
        old_paths_to_new = {cid_to_path(k): cid_to_path(v) for k, v in moves.items()}
        new_cids = frozenset(moves.values())

        for span in (*iter_link_spans(note.body), *iter_relative_path_spans(note.body)):
            if span.kind == LINK_KIND_WIKI:
                was, will = before.resolve(span.target), after.resolve(span.target)
                if was in moves:
                    expected_cid = moves[was]
                    if will != expected_cid:
                        text = _shortest_wiki_target(expected_cid, after)
                        if span.embed and is_attachment_target(text):
                            text += ".md"  # `![[v1.5]]` reads as an attachment, `![[v1.5.md]]` as the note
                        raw_edits.append((span, text))
                elif was != will and was is None:
                    newly_resolved.append(f"{note.rel_path}: [[{span.target}]] was dead and would open {will}")
                elif was != will:
                    blocked.append(
                        f"{note.rel_path}: [[{span.target}]] opens {was} today but would open {will} after the move"
                    )
            elif moving:
                # A moved note's own relative paths keep naming the same file from the new folder.
                old_cid = note.cid
                new_cid = moves[old_cid]
                if not span.target or posixpath.normpath(span.target) == ".":
                    continue
                target = MoveNoteUseCase._expected_relative_target(old_cid, span.target, old_paths_to_new)
                if target is None or resolve_relative_path(new_cid, span.target) == target:
                    continue
                text = posixpath.relpath(target, posixpath.dirname(cid_to_path(new_cid)) or ".")
                raw_edits.append((span, _markdown_path_text(text, span.angle)))
            elif span.kind != LINK_KIND_IMAGE and span.target.lower().endswith(".md") and not is_raw_path(span.target):
                target_cid = resolve_markdown_target(note.cid, span.target_raw)
                if target_cid in moves:
                    dest_cid = moves[target_cid]
                    raw_edits.append((span, _markdown_path_text(relative_note_path(note.cid, dest_cid), span.angle)))
                elif target_cid in new_cids:
                    newly_resolved.append(f"{note.rel_path}: ({span.target}) was dead and would open {target_cid}")

        edits: list[tuple[LinkSpan, str]] = []
        cursor = 0
        for span, text in sorted(raw_edits, key=lambda e: (e[0].start, -e[0].end)):
            if span.start < cursor:
                continue  # nested inside a link already being rewritten, which carries it along
            edits.append((span, text))
            cursor = span.end
        for span, text in edits:
            if not MoveNoteUseCase._reads_back(note, span, text, moves, old_paths_to_new, before, after):
                blocked.append(
                    f"{note.rel_path}: '{span.target_raw}' rewritten as '{text}' would no longer open the same file; "
                    f"choose a destination name links can carry"
                )
        return edits

    @staticmethod
    def _reads_back(
        note: Note,
        span: LinkSpan,
        text: str,
        moves: dict[str, str],
        old_paths_to_new: dict[str, str],
        before: LinkTargetIndex,
        after: LinkTargetIndex,
    ) -> bool:
        """Whether the rewritten link still parses as one link of its kind, naming what it should."""
        probe = _reparse(span, text)
        if probe is None:
            return False
        if span.kind == LINK_KIND_WIKI:
            was = before.resolve(span.target)
            return was in moves and after.resolve(probe.target) == moves[was]
        if note.cid in moves:
            old_cid = note.cid
            new_cid = moves[old_cid]
            expected = MoveNoteUseCase._expected_relative_target(old_cid, span.target, old_paths_to_new)
            return resolve_relative_path(new_cid, probe.target) == expected and (
                is_raw_path(probe.target) == is_raw_path(span.target)
            )
        old_target = resolve_markdown_target(note.cid, span.target_raw)
        return (
            old_target in moves
            and probe.target.lower().endswith(".md")
            and not is_raw_path(probe.target)
            and resolve_markdown_target(note.cid, probe.target_raw) == moves[old_target]
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


def _truncate_sections(
    full_text: str, body: str, budget_tokens: int | None, render: Callable[[str], str]
) -> tuple[str, bool]:
    """Return (content, was_truncated) honoring a token budget via section splitting (§5.4).

    `full_text` is what an unbudgeted call returns and `body` the part of it
    that sections split; `render` puts a truncated body back into its frame
    (a note's frontmatter, or nothing for a raw source).
    """
    if budget_tokens is None or budget_tokens <= 0:
        return full_text, False

    char_budget = budget_tokens * 4
    if len(full_text) <= char_budget:
        return full_text, False

    sections = split_markdown_sections(body)
    total_sections = max(1, len(sections))
    overhead = len(full_text) - len(body)
    accumulated: list[str] = []
    current_len = overhead
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
        remaining = max(0, char_budget - overhead - len(marker))
        truncated_body = body[:remaining].rstrip()
    return render(truncated_body + marker), True


def _truncate_to_budget(note: Note, budget_tokens: int | None) -> tuple[str, bool]:
    """A note's serialized text within `budget_tokens` (§5.4)."""
    return _truncate_sections(
        note.serialize(), note.body, budget_tokens, lambda body: serialize_frontmatter(note.frontmatter, body)
    )


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

    def _load(self, cid: str) -> Note:
        note = self.storage.get_note(cid)
        if note is None:
            raise FileNotFoundError(f"Note not found: {cid_to_path(cid)}")
        return note

    def _resolve_target_and_anchor(
        self, raw_target: str, cids: Sequence[str], index: LinkTargetIndex
    ) -> tuple[Note, str | None]:
        """The note `raw_target` names and its `#anchor`, by the same rules as the write tools (§5.1).

        A filename that literally contains '#' (`wiki/C#-Basics.md`) is tried as a
        whole path before any '#' is read as an anchor. Raises FileNotFoundError,
        AmbiguousNoteError, or FrontmatterParseError (naming the file), exactly as
        update, verify and move do for the same name.
        """
        unwrapped = _strip_dot_slash(strip_link_wrapper(raw_target))
        if "#" in unwrapped and (literal := _match_listed_path(cids, unwrapped, raw_target.strip())):
            return self._load(literal), None
        clean, anchor = split_link_target_and_anchor(unwrapped)
        try:
            cid = resolve_note_cid(self.storage, clean, index, cids=cids)
        except FileNotFoundError:
            if unwrapped.count("#") < 2:
                raise
            head, _, tail = unwrapped.rpartition("#")  # 'wiki/C#-Basics#Intro'
            cid = resolve_note_cid(self.storage, head, index, cids=cids)
            anchor = tail.strip() or None
        return self._load(cid), anchor

    @staticmethod
    def _apply_section_anchor(note: Note, anchor: str | None) -> tuple[Note, str | None, str | None]:
        """`(note_to_return, matched_heading, section_error)` for an optional `#anchor` (§5.1).

        A heading that no longer exists is not a failure: the note resolved, as
        lint's ANCHOR_MISSING warning recognizes (§6.3), so the whole note comes
        back with `section_error` naming the headings it does have.
        """
        if anchor is None:
            return note, None, None
        extracted = extract_markdown_section(note.body, anchor)
        if extracted is None:
            headings = extract_headings(note.body)
            available = ", ".join(f"'{h}'" for h in headings) if headings else "none"
            return note, None, f"Section '#{anchor}' not found in '{note.cid}' (available headings: {available})"
        matched_section, section_text = extracted
        return replace(note, body=section_text, raw_text=None), matched_section, None

    def _grounded(
        self,
        raw_target: str,
        note: Note,
        anchor: str | None,
        budget_tokens: int | None,
        domains: dict[str, Any],
    ) -> dict[str, Any]:
        note_for_budget, matched_section, section_error = self._apply_section_anchor(note, anchor)
        domain_name = path_to_layer(note.rel_path)
        domain_def = domains.get(domain_name)
        if self.cache is not None:
            links, backlinks, sources = self.cache.get_note_neighborhood(note.cid)
        else:
            links, backlinks, sources = [], [], []
        content_to_return, truncated = _truncate_to_budget(note_for_budget, budget_tokens)
        item: dict[str, Any] = {
            "target": raw_target,
            "kind": "note",
            "cid": note.cid,
            "domain": domain_name,
            "title": note.title or Path(note.rel_path).stem,
            "type": note.type,
            "status": note.status,
            "trust_tier": note.trust_tier,
            "section": matched_section,
            "directives": domain_def.directives_markdown if domain_def is not None else "",
            "content": content_to_return,
            "links": links,
            "backlinks": backlinks,
            "sources": sources,
            "truncated": truncated,
        }
        if section_error is not None:
            item["section_error"] = section_error
        return item

    def _resolve_source(self, unwrapped: str, shown: str, sources: Sequence[str]) -> tuple[str, str | None]:
        """The raw/ file `unwrapped` names and its `#anchor` (§5.1).

        The whole name is tried first, so a filename containing '#' still
        resolves; then the text before the last '#'. Each try is exact, then
        unique ignoring letter case. Only files storage lists as evidence can
        match, which is what keeps a crafted path from reading anything else.
        """

        def match(path: str) -> str | None:
            if path in sources:
                return path
            folded = sorted(s for s in sources if s.casefold() == path.casefold())
            if len(folded) > 1:
                raise AmbiguousNoteError(shown, folded)
            return folded[0] if folded else None

        if found := match(unwrapped):
            return found, None
        if "#" in unwrapped:
            head, _, tail = unwrapped.rpartition("#")
            if found := match(head):
                return found, tail.strip() or None
        raise FileNotFoundError(f"Source not found: {unwrapped}")

    def _grounded_source(self, raw_target: str, path: str, anchor: str | None, budget_tokens: int | None) -> dict[str, Any]:
        """A raw/ file as evidence: its text and the notes citing it, never a trust tier (§5.1)."""
        cited_by = self.cache.get_source_citers(path) if self.cache is not None else []
        item: dict[str, Any] = {"target": raw_target, "kind": "raw", "path": path, "section": None}
        text_exts = tuple(self.vault.config["raw_text_extensions"]) if self.vault is not None else None
        if text_exts is not None and not is_raw_text_path(path, text_exts):
            # The engine has no reader for this format; decoding it would hand
            # the caller bytes dressed up as source text.
            return item | {"content": None, "filename_only": True, "truncated": False, "cited_by": cited_by}
        text = self.storage.read_source(path)
        if text is None:
            raise FileNotFoundError(f"Source not found: {path}")
        if anchor is not None:
            extracted = extract_markdown_section(text, anchor)
            if extracted is None:
                headings = extract_headings(text)
                available = ", ".join(f"'{h}'" for h in headings) if headings else "none"
                item["section_error"] = f"Section '#{anchor}' not found in '{path}' (available headings: {available})"
            else:
                item["section"], text = extracted
        content, truncated = _truncate_sections(text, text, budget_tokens, lambda body: body)
        return item | {"content": content, "filename_only": False, "truncated": truncated, "cited_by": cited_by}

    def execute(
        self,
        targets: Sequence[str],
        budget_tokens: int | None = None,
    ) -> list[dict[str, Any]]:
        """One entry per target, in order: the grounded note or source, or `{"target", "error"}` (§5.1).

        A target that cannot be grounded is reported in its place, classified
        exactly as any other failure (§5.4), so one stale link does not cost the
        caller the rest of the batch. If nothing at all could be grounded the
        call fails with the first target's error, which makes a single-target
        call behave like update, verify and move. A target under `raw/` names a
        source, never a note: no note can live there (§2.1).
        """
        if isinstance(targets, str) or not targets:
            raise ValueError("targets must be a non-empty list of note names or raw/ paths")
        domains = self.vault.discover_domains() if self.vault is not None else {}
        # Names resolve against what storage lists, not the search index, so a
        # note whose frontmatter is broken is named in its error rather than the
        # name silently resolving to another note with the same stem.
        listed = list(self.storage.list_note_cids())
        index = LinkTargetIndex(listed)
        sources: list[str] | None = None

        results: list[dict[str, Any]] = []
        failures: list[Exception] = []
        for raw_target in targets:
            try:
                unwrapped = _strip_dot_slash(strip_link_wrapper(raw_target))
                if unwrapped.casefold().startswith(f"{DIR_RAW}/"):
                    if sources is None:
                        sources = list(self.storage.list_source_paths())
                    path, anchor = self._resolve_source(unwrapped, raw_target.strip(), sources)
                    results.append(self._grounded_source(raw_target, path, anchor, budget_tokens))
                    continue
                note, anchor = self._resolve_target_and_anchor(raw_target, listed, index)
            except Exception as exc:  # noqa: BLE001 -- classified per target, §5.4
                info = classify(exc)
                failures.append(exc)
                results.append(
                    {"target": raw_target, "error": {"code": info.code, "message": info.message, "retryable": info.retryable}}
                )
                continue
            results.append(self._grounded(raw_target, note, anchor, budget_tokens, domains))

        if failures and len(failures) == len(results):
            raise failures[0]
        return results


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
    *,
    edits: list[dict[str, Any]] | None = None,
    expected_hash: str | None = None,
    actor: str = "agent:unknown",
    storage: NoteStoragePort | None = None,
    ledger: LedgerPort | None = None,
    cache: IndexCachePort | None = None,
) -> Path:
    """Non-destructively patch note frontmatter and apply ordered body `edits` (§5.1)."""
    active_storage, active_ledger = _resolve_adapters(vault, storage, ledger)
    uc = UpdateNoteUseCase(active_storage, active_ledger)
    note = uc.execute(
        cid_or_path=cid_or_path,
        frontmatter_patch=frontmatter_patch,
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
    if _strip_dot_slash(strip_link_wrapper(target)).casefold().startswith(f"{DIR_RAW}/"):
        raise ValueError("raw/ sources are not graph nodes; vault_ground a raw/ path lists the notes citing it")
    active_storage, _ = _resolve_adapters(vault, storage)
    listed = list(active_storage.list_note_cids())
    cid = resolve_note_cid(active_storage, target, LinkTargetIndex(listed), cids=listed)
    data = get_note_graph(cache.get_connection(), cid, limit=limit)
    if data is None:
        # Listed but not indexed: its frontmatter does not parse (§4.4).
        load_existing_note(active_storage, cid)
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
        _refresh(cache)
    return plan.to_dict(applied=apply)

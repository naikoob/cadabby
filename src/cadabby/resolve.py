"""Shared vault-relative name resolution, path/location validation, and layout-port binding.

Conforms strictly to Cadabby Technical Specification §2.1, §2.2, §3.4, §5.1, §7.5, §9.1.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from cadabby.adapters.memory_storage import InMemoryVaultLayout
from cadabby.constants import ACTOR_PATTERN, DIR_RAW, FILE_AGENTS
from cadabby.domain import Note
from cadabby.errors import AlreadyExistsError, AmbiguousNoteError, InvalidArgumentError, NotFoundError
from cadabby.graph import LinkTargetIndex, normalize_link_target
from cadabby.okf import is_valid_actor
from cadabby.ports import NoteStoragePort, VaultLayoutPort
from cadabby.vault import cid_to_path, normalize_rel, path_to_cid, path_to_layer


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
        raise InvalidArgumentError(absolute_error)
    stripped = _strip_dot_slash(raw_input)
    if stripped.startswith("/") or Path(stripped).is_absolute():
        raise InvalidArgumentError(absolute_error)
    parsed = _vault_name_parts(raw_input)
    if parsed is None:
        raise InvalidArgumentError(f"Path traversal detected in '{raw_input}': must not contain '.' or '..'")
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
        raise InvalidArgumentError(f"Invalid actor format {actor!r}. Must match {ACTOR_PATTERN}")


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
        raise InvalidArgumentError("Note CID or path must be a non-empty string")
    shown = cid_or_path.strip()
    parsed = _vault_name_parts(cid_or_path)
    if parsed is None:
        raise NotFoundError(f"Note not found: {shown} (names must be vault-relative)")
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
    raise NotFoundError(f"Note not found: {shown}")


def load_existing_note(
    storage: NoteStoragePort,
    cid_or_path: str,
    resolver: LinkTargetIndex | None = None,
) -> Note:
    """The note `cid_or_path` names, resolved by `resolve_note_cid`; NotFoundError if it is gone."""
    cid = resolve_note_cid(storage, cid_or_path, resolver)
    note = storage.get_note(cid)
    if note is None:
        raise NotFoundError(f"Note not found: {cid_to_path(cid)}")
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


def _default_layout(storage: NoteStoragePort, vault: VaultLayoutPort | None) -> VaultLayoutPort:
    """Resolve the active `VaultLayoutPort`: explicit `vault`, else `storage`'s layout, else an in-memory default (§9.1)."""
    if vault is not None:
        return vault
    layout = getattr(storage, "layout", None)
    if isinstance(layout, VaultLayoutPort):
        return layout
    return InMemoryVaultLayout()


def _check_new_location(
    storage: NoteStoragePort,
    vault: VaultLayoutPort,
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
    why = vault.note_path_violation(rel_path, for_write=True) or _name_violation(rel_path)
    if why:
        raise InvalidArgumentError(f"Cannot {verb} at '{rel_path}': {why}")

    domain = path_to_layer(rel_path)
    domain_def = vault.discover_domains().get(domain)
    if domain_def and domain_def.manifest_error:
        raise InvalidArgumentError(
            f"Cannot {verb} into domain '{domain}': its {FILE_AGENTS} is invalid "
            f"({domain_def.manifest_error}). Fix the manifest first"
        )
    if domain_def and domain_def.allowed_types is not None and type_ not in domain_def.allowed_types:
        raise InvalidArgumentError(
            f"Invalid note type '{type_}'. In domain '{domain}', allowed types are {domain_def.allowed_types}"
        )

    cid = path_to_cid(rel_path)
    excluded = replacing_group | ({replacing} if replacing is not None else set())
    others = [c for c in storage.list_note_cids() if c not in excluded]
    if cid in others:
        raise AlreadyExistsError(f"Note already exists at {rel_path}")
    clash = next((c for c in others if c.casefold() == cid.casefold()), None)
    if clash is not None:
        raise AlreadyExistsError(
            f"Note already exists at {cid_to_path(clash)} (differs from '{rel_path}' only by letter case)"
        )
    # The listing walks real folders only; the storage also sees files reached
    # through a symlinked folder, which a write would otherwise overwrite.
    own_spelling = any(cid.casefold() == r.casefold() for r in excluded)
    if not own_spelling and storage.note_exists(cid):
        raise AlreadyExistsError(f"Note already exists at {rel_path}")


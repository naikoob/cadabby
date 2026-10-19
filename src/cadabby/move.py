"""Note and folder move planning, link rewriting, write ordering, and transactional rollback.

Conforms strictly to Cadabby Technical Specification §3.4, §5.1, §8, §9.1.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import quote

from cadabby.constants import DIR_WIKI
from cadabby.domain import Note
from cadabby.errors import AlreadyExistsError, AmbiguousNoteError, InvalidArgumentError, NotFoundError
from cadabby.frontmatter import FrontmatterParseError, split_frontmatter
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
from cadabby.resolve import (
    _check_new_location,
    _default_layout,
    _parse_relative_target,
    _require_actor,
    _strip_dot_slash,
    _vault_name_parts,
    load_existing_note,
    resolve_note_cid,
    with_md_suffix,
)
from cadabby.ports import LedgerPort, NoteStoragePort, VaultLayoutPort
from cadabby.vault import cid_to_path, normalize_rel, path_to_cid


def _stable_wiki_target(cid: str) -> str:
    """The wikilink text that opens `cid` and that no note added later can take over (§5.1).

    A `wiki/` note's bare stem and any full CID are exact names, matched before
    any suffix or stem; a shorter name for a domain or `archive/` note opens it
    only while no higher-ranked note shares that name (§4.4). Wiki short names
    never contain '/' and CIDs always do, so the two forms cannot collide.
    """
    return cid.removeprefix(f"{DIR_WIKI}/") if cid.startswith(f"{DIR_WIKI}/") else cid


def _is_stable_wiki_text(text: str, cid: str) -> bool:
    """Whether wikilink `text` names `cid` in a form no note added later can take over (§4.4)."""
    return normalize_rel(text).removesuffix(".md") in (cid, _stable_wiki_target(cid))


def _fragile_warnings(fragile: Sequence[tuple[str, str, str]]) -> list[str]:
    """One warning per moved note whose untouched links now open it only by a name others could take (§5.1).

    Rewriting them would unbind each citing note's attestations, so they are
    left byte-identical and named instead; the user decides.
    """
    by_target: dict[str, list[tuple[str, str]]] = {}
    for rel_path, text, cid in fragile:
        by_target.setdefault(cid, []).append((rel_path, text))
    out = []
    for cid, links in sorted(by_target.items()):
        sample = ", ".join(f"{rel}: [[{text}]]" for rel, text in links[:3])
        more = f" (+{len(links) - 3} more)" if len(links) > 3 else ""
        name = cid.rsplit("/", 1)[-1]
        out.append(
            f"{len(links)} link(s) still open {cid}, but only while no other note is named '{name}': "
            f"{sample}{more}; [[{_stable_wiki_target(cid)}]] would not depend on that"
        )
    return out


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
            raise InvalidArgumentError(
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
    # Refusals no approval can lift: retargeted links, unreadable notes, ...
    blocked: list[str] = field(default_factory=list)
    # human-reviewed referrers not yet named in approve_stale: their CIDs, and why.
    needs_approval: list[str] = field(default_factory=list)
    unapproved: list[str] = field(default_factory=list)
    newly_resolved: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def refusals(self) -> list[str]:
        """Every reason this plan cannot be applied as it stands."""
        return [*self.blocked, *self.unapproved]

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
        """A fingerprint of what this plan would write and refuse, over the exact bytes it read.

        `unapproved` is left out. approve_stale records the user's consent to a
        rewrite the dry run already showed, tier change and all -- that note's
        rewrite and source hash are in `rewrites` either way -- so the token of
        a dry run blocked only for approval is the token its approved apply
        passes back (§5.1).
        """

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
            "blocked": self.refusals,
            "needs_approval": list(self.needs_approval),
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

    def __init__(self, storage: NoteStoragePort, ledger: LedgerPort, vault: VaultLayoutPort | None = None) -> None:
        self.storage = storage
        self.ledger = ledger
        self.vault = _default_layout(storage, vault)

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
                raise NotFoundError(f"Note not found: {cid_or_path.strip()} (names must be vault-relative)")
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

        fragile: list[tuple[str, str, str]] = []
        for cid in cids:
            moving_item = moved_by_cid.get(cid)
            note = moving_item.source if moving_item is not None else self._read(cid, plan, moves, before, after)
            if note is None:
                continue
            edits = self._link_edits(note, moves, before, after, plan.newly_resolved, plan.blocked, fragile)
            if not edits:
                continue
            body = _apply_spans(note.body, edits)
            if moving_item is not None:
                moving_item.destination = replace(moving_item.destination, body=body)
                moving_item.links = len(edits)
            else:
                plan.rewrites.append(_Rewrite(original=note, updated=replace(note, body=body), links=len(edits)))
            if note.trust_tier == "human-reviewed" and cid not in approved:
                plan.needs_approval.append(cid)
                plan.unapproved.append(
                    f"{note.rel_path} is human-reviewed; rewriting its links would make it stale-verified. "
                    f"Ask the user, then pass it in approve_stale"
                )
        plan.warnings.extend(_fragile_warnings(fragile))
        return plan

    def apply(self, plan: MovePlan, actor: str) -> None:
        """Write the plan destination-first, so a crash leaves a duplicate, never a lost note."""
        _require_actor(actor)
        if plan.refusals:
            raise InvalidArgumentError("Move refused: " + "; ".join(plan.refusals))
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
                    raise AlreadyExistsError(f"Note already exists at {destination.rel_path}")
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
            raise NotFoundError(f"Note not found: {cid_or_path.strip()} (no notes under '{prefix}/')")
        return None

    def _leftover_folder_files(self, src_prefix: str, folder_cids: Sequence[str]) -> list[str]:
        moving_rels = {cid_to_path(c).casefold() for c in folder_cids}
        return [
            rel
            for rel in self.vault.list_folder_files(src_prefix)
            if rel.casefold() not in moving_rels
        ]

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
            raise InvalidArgumentError(f"Cannot move folder '{src_prefix}/' to a single file '{to}'")
        dst_prefix = "/".join(raw_parts)
        if dst_prefix == src_prefix:
            raise InvalidArgumentError(f"'{src_prefix}/' is already at '{dst_prefix}/'")

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
                raise NotFoundError(f"Note not found: {cid_to_path(old_cid)}")
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
            is_folder = any(c.startswith(folder + "/") for c in cids) or self.vault.has_folder(folder)
            if is_folder and not raw.lower().endswith(".md"):
                raise InvalidArgumentError(
                    f"'{to}' is an existing folder: end it with '/' to move the note into it, "
                    f"or add '.md' to name a file"
                )
        rel = with_md_suffix("/".join(raw_parts))
        if path_to_cid(rel) == source.cid:
            raise InvalidArgumentError(f"'{source.rel_path}' is already at '{rel}'")
        _check_new_location(self.storage, self.vault, rel, source.type, verb="move", replacing=source.cid)
        return rel

    def _approvals(self, items: Sequence[str], moves: dict[str, str], resolver: LinkTargetIndex) -> set[str]:
        """The CIDs the user agreed may go stale. Moved notes or folders may be named by where they are going."""
        approved: set[str] = set()
        new_to_old = {v: k for k, v in moves.items()}
        for item in items:
            if not isinstance(item, str) or not item.strip():
                raise InvalidArgumentError("approve_stale entries must be non-empty note names")
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
        fragile: list[tuple[str, str, str]] | None = None,
    ) -> list[tuple[LinkSpan, str]]:
        """The target rewrites a move makes in `note`, each checked to read back as the same link.

        A link that still opens the moved note is left byte-identical (§5.1),
        but if the move leaves it resolvable only by a name another note could
        take (§4.4), `(rel_path, text, new_cid)` is appended to `fragile`.
        """
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
                        text = _stable_wiki_target(expected_cid)
                        if span.embed and is_attachment_target(text):
                            text += ".md"  # `![[v1.5]]` reads as an attachment, `![[v1.5.md]]` as the note
                        raw_edits.append((span, text))
                    elif (
                        fragile is not None
                        and _is_stable_wiki_text(span.target, was)
                        and not _is_stable_wiki_text(span.target, expected_cid)
                    ):
                        fragile.append((note.rel_path, span.target, expected_cid))
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


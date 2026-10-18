"""Multi-target note and raw/ source grounding with section extraction and token budgeting.

Conforms strictly to Cadabby Technical Specification §2.1, §2.4, §5.1, §5.4, §9.1.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from cadabby.cache import is_raw_text_path
from cadabby.constants import DIR_RAW
from cadabby.domain import Note
from cadabby.errors import AmbiguousNoteError, InvalidArgumentError, NotFoundError, classify
from cadabby.frontmatter import serialize_frontmatter
from cadabby.graph import LinkTargetIndex, split_link_target_and_anchor, strip_link_wrapper
from cadabby.markdown import extract_headings, extract_markdown_section, split_markdown_sections
from cadabby.resolve import (
    _default_layout,
    _match_listed_path,
    _strip_dot_slash,
    resolve_note_cid,
)
from cadabby.ports import IndexCachePort, NoteStoragePort, VaultLayoutPort
from cadabby.vault import cid_to_path, path_to_layer

# The most of a raw/ file vault_ground reads; more is reported as truncated.
RAW_GROUND_MAX_BYTES = 1024 * 1024


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
        vault: VaultLayoutPort | None = None,
    ) -> None:
        self.storage = storage
        self.cache = cache
        self.vault = _default_layout(storage, vault)

    def _load(self, cid: str) -> Note:
        note = self.storage.get_note(cid)
        if note is None:
            raise NotFoundError(f"Note not found: {cid_to_path(cid)}")
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
        raise NotFoundError(f"Source not found: {unwrapped}")

    def _grounded_source(self, raw_target: str, path: str, anchor: str | None, budget_tokens: int | None) -> dict[str, Any]:
        """A raw/ file as evidence: its text and the notes citing it, never a trust tier (§5.1)."""
        cited_by = self.cache.get_source_citers(path) if self.cache is not None else []
        item: dict[str, Any] = {"target": raw_target, "kind": "raw", "path": path, "section": None}
        if not is_raw_text_path(path, tuple(self.vault.raw_text_extensions)):
            # The engine has no reader for this format; decoding it would hand
            # the caller bytes dressed up as source text.
            return item | {"content": None, "filename_only": True, "truncated": False, "cited_by": cited_by}
        # Read no more than can be returned: raw dumps (CSVs, logs) run to
        # hundreds of megabytes, and a budget of 50 tokens read the whole file.
        cap = RAW_GROUND_MAX_BYTES if budget_tokens is None else min(RAW_GROUND_MAX_BYTES, budget_tokens * 16)
        text = self.storage.read_source(path, max_bytes=cap + 1)
        if text is None:
            raise NotFoundError(f"Source not found: {path}")
        cut_at_read = len(text.encode("utf-8")) > cap
        if cut_at_read:
            text = text.encode("utf-8")[:cap].decode("utf-8", errors="ignore")
        if anchor is not None:
            extracted = extract_markdown_section(text, anchor)
            if extracted is None:
                headings = extract_headings(text)
                available = ", ".join(f"'{h}'" for h in headings) if headings else "none"
                item["section_error"] = f"Section '#{anchor}' not found in '{path}' (available headings: {available})"
            else:
                item["section"], text = extracted
        content, truncated = _truncate_sections(text, text, budget_tokens, lambda body: body)
        return item | {
            "content": content,
            "filename_only": False,
            "truncated": truncated or cut_at_read,
            "cited_by": cited_by,
        }

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
            raise InvalidArgumentError("targets must be a non-empty list of note names or raw/ paths")
        domains = self.vault.discover_domains()
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
                # Exactly `raw/`: a domain folder named `Raw/` holds notes (§2.1).
                if unwrapped.startswith(f"{DIR_RAW}/"):
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


"""Wikilink and Markdown note-link extraction, target resolution, and graph edge parsing.

Conforms to Cadabby Technical Specification §4.2, §4.4, §6.3.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote

from cadabby.constants import DIR_ARCHIVE, DIR_RAW, DIR_WIKI
from cadabby.markdown import code_spans, iter_fence_states
from cadabby.paths import cid_to_path, normalize_rel, path_to_cid

# Regex matching [[Target#Anchor|Alias]] (including table-escaped \| aliases)
# and note transclusions ![[Target#Anchor]], ignoring multiline brackets and
# code spans. Group 1 is the embed marker; attachment embeds are filtered below.
# No group may contain `[`: an unclosed `[[` then ends at the next one instead
# of rescanning the rest of the line from every opener (quadratic on `[[`*n).
RE_WIKILINK = re.compile(r"(!?)\[\[([^\[\]|#\n]+?)(?:#([^\[\]|\n]+?))?(?:\\?\|([^\[\]\n]+))?\]\]")

# A final path segment ending in `.ext`, where ext is not `md`. An embed naming
# such a file (`![[diagram.png]]`, `![[flow.canvas]]`) is an attachment, not a
# note transclusion, and is never a graph edge (§4.4).
RE_ATTACHMENT_SUFFIX = re.compile(r"\.(?!md$)[A-Za-z0-9]+$", re.IGNORECASE)


def is_attachment_target(target_stem: str) -> bool:
    """Whether an embed target names a non-note file by its extension."""
    return bool(RE_ATTACHMENT_SUFFIX.search(target_stem.rsplit("/", 1)[-1]))

# Inline Markdown link, not an image embed: [label](target "optional title").
# The angle-bracket form [label](<My Note.md>) is what Obsidian writes for
# paths containing spaces, so group 1 takes it and group 2 the bare form.
# Link text may nest one level of brackets ([a [b] c](x)); neither it nor the
# target may run into another link's opener, which kept every pattern linear.
_MD_LABEL = r"\[(?:[^\[\]\n]|\[[^\[\]\n]*\])*\]"
_MD_TARGET = r'\(\s*(?:<([^<>\n]+)>|([^()\s<]+))(?:\s+"[^"\n]*")?\s*\)'
RE_MD_LINK = re.compile(r"(?<!!)" + _MD_LABEL + _MD_TARGET)
RE_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")

# Paths a note names that are never graph edges, but still go stale when the
# note moves folders: image embeds `![alt](path)` and reference definitions
# `[id]: path` (footnotes `[^1]:` excluded). Only `vault_move_note` reads them.
RE_MD_IMAGE = re.compile(r"!" + _MD_LABEL + _MD_TARGET)
RE_MD_REFDEF = re.compile(r"^[ ]{0,3}\[(?!\^)[^\]\n]+\]:[ \t]*(?:<([^>\n]+)>|(\S+))", re.MULTILINE)
RE_BLOCK_MATH = re.compile(r"(?<!\\)\$\$.*?(?<!\\)\$\$", re.DOTALL)
RE_INLINE_MATH = re.compile(r"(?<!\\)\$(?!\s)(?:[^$\n]*?(?<!\s))?\$(?![0-9$])")

LINK_KIND_WIKI = "wiki"
LINK_KIND_MARKDOWN = "markdown"
LINK_KIND_IMAGE = "image"
LINK_KIND_REFDEF = "refdef"


@dataclass
class ExtractedLink:
    """Represents a single wikilink or Markdown note-link reference in a markdown note."""

    target_raw: str  # Exactly as written in the link target, e.g. "Note#Heading" or "../wiki/Note.md#Heading"
    target_stem: str  # Note stem or decoded path part, e.g. "Note" or "../wiki/Note.md"
    anchor: str | None = None  # Heading anchor, e.g. "Heading"
    alias: str | None = None  # Link display alias, e.g. "alias"
    occurrences: int = 1
    kind: str = LINK_KIND_WIKI  # "wiki" for [[...]], "markdown" for [label](path.md)


def _blank_preserving_newlines(match: re.Match[str]) -> str:
    return re.sub(r"[^\n]", " ", match.group(0))


def strip_code_spans(text: str) -> str:
    """Blank fenced code, inline code, and LaTeX math so none yields links.

    Length-preserving: every blanked character becomes a space, so an offset
    into the result is an offset into `text`. `iter_link_spans` depends on
    that to report where each link target sits in the original body.
    """
    out = []
    for line, in_fence in iter_fence_states(text.split("\n")):
        if in_fence:
            out.append(" " * len(line))
            continue
        chars = list(line)
        for open_, _, _, close in code_spans(line):
            chars[open_:close] = " " * (close - open_)
        out.append("".join(chars))
    cleaned = RE_BLOCK_MATH.sub(_blank_preserving_newlines, "\n".join(out))
    return RE_INLINE_MATH.sub(_blank_preserving_newlines, cleaned)


@dataclass(frozen=True, slots=True)
class LinkSpan:
    """One link occurrence, with where its target text sits in the body.

    `start:end` covers only the target (the wikilink stem, or the Markdown
    path before any `#`), so replacing that slice keeps embed markers,
    anchors, aliases, escaped table pipes and angle brackets byte-identical.
    """

    kind: str  # LINK_KIND_WIKI or LINK_KIND_MARKDOWN
    start: int
    end: int
    target: str  # wikilink stem as resolved; Markdown path decoded
    target_raw: str  # wikilink "stem#anchor"; Markdown target exactly as written
    anchor: str | None = None
    alias: str | None = None
    embed: bool = False
    angle: bool = False  # Markdown written as [label](<path>)


def _iter_wiki_spans(clean: str) -> Iterator[LinkSpan]:
    for match in RE_WIKILINK.finditer(clean):
        is_embed, raw_target, raw_anchor_group, raw_alias = match.groups()
        alias = (raw_alias.strip() or None) if raw_alias else None
        start, end = match.span(2)
        # A table-escaped alias pipe (`[[x\|a]]`) leaves its backslash on the
        # stem when there is no anchor to absorb it.
        stem_text = raw_target.rstrip("\\") if (raw_alias is not None and not raw_anchor_group) else raw_target
        target_stem = stem_text.strip()
        if target_stem:
            end -= len(raw_target) - len(stem_text)
            start += len(stem_text) - len(stem_text.lstrip())
            end -= len(stem_text) - len(stem_text.rstrip())
        else:
            # `[[   ]]` stays a link -- gate 3 reports it dead -- with a
            # zero-width span, since there is no target text to point at.
            end = start
        if is_embed and is_attachment_target(target_stem):
            continue
        raw_anchor = (
            raw_anchor_group.rstrip("\\") if (raw_alias is not None and raw_anchor_group) else raw_anchor_group
        )
        anchor = (raw_anchor.strip() or None) if raw_anchor else None
        yield LinkSpan(
            kind=LINK_KIND_WIKI,
            start=start,
            end=end,
            target=target_stem,
            target_raw=f"{target_stem}#{anchor}" if anchor else target_stem,
            anchor=anchor,
            alias=alias,
            embed=bool(is_embed),
        )


def _path_span(kind: str, match: re.Match[str]) -> LinkSpan | None:
    """A relative path written as `<path>` (group 1) or bare (group 2); None for URLs, `/x` and `#x`."""
    group = 1 if match.group(1) is not None else 2
    written = match.group(group)
    target = written.strip()
    if not target or target.startswith(("/", "#")) or RE_URL_SCHEME.match(target):
        return None
    start = match.start(group) + (len(written) - len(written.lstrip()))
    path_part, _, anchor = target.partition("#")
    return LinkSpan(
        kind=kind,
        start=start,
        end=start + len(path_part),
        target=unquote(path_part),
        target_raw=target,
        anchor=unquote(anchor).strip() or None,
        angle=group == 1,
    )


def _iter_markdown_spans(clean: str) -> Iterator[LinkSpan]:
    """Relative inline Markdown links. URLs, vault-absolute paths and `#`-only jumps are skipped."""
    for match in RE_MD_LINK.finditer(clean):
        if (span := _path_span(LINK_KIND_MARKDOWN, match)) is not None:
            yield span


def iter_link_spans(markdown_text: str) -> Iterator[LinkSpan]:
    """Every wikilink and relative Markdown link outside code, with its target span (§4.4).

    The single parser behind the graph extractors, gate 4's citation check and
    `vault_move_note`, so a move rewrites exactly the links the index sees.
    """
    clean = strip_code_spans(markdown_text)
    yield from _iter_wiki_spans(clean)
    yield from _iter_markdown_spans(clean)


def iter_relative_path_spans(markdown_text: str) -> Iterator[LinkSpan]:
    """Relative image embeds and reference definitions outside code (§5.1).

    Neither is a graph edge, so nothing but `vault_move_note` reads them: a
    moved note's `![fig](../raw/x.png)` must still name the same file from its
    new folder, and a `[ref]: ../wiki/x.md` must follow the note it names.
    """
    clean = strip_code_spans(markdown_text)
    for pattern, kind in ((RE_MD_IMAGE, LINK_KIND_IMAGE), (RE_MD_REFDEF, LINK_KIND_REFDEF)):
        for match in pattern.finditer(clean):
            if (span := _path_span(kind, match)) is not None:
                yield span


def is_raw_path(decoded_path: str) -> bool:
    """Whether a relative link path points into `raw/` (a citation, never an edge)."""
    return DIR_RAW in PurePosixPath(decoded_path).parts


def extract_wikilinks(markdown_text: str) -> list[ExtractedLink]:
    """Extract all distinct wikilinks and their occurrences from markdown text."""
    links_by_target: dict[str, ExtractedLink] = {}
    for span in _iter_wiki_spans(strip_code_spans(markdown_text)):
        if span.target_raw in links_by_target:
            links_by_target[span.target_raw].occurrences += 1
        else:
            links_by_target[span.target_raw] = ExtractedLink(
                target_raw=span.target_raw, target_stem=span.target, anchor=span.anchor, alias=span.alias
            )
    return list(links_by_target.values())


def iter_markdown_link_targets(markdown_text: str) -> Iterator[str]:
    """Yield relative inline Markdown link targets outside code, exactly as written.

    URLs (any scheme), vault-absolute paths and same-note `#heading` jumps are
    skipped: none of them is a relative path a note could be addressed by.
    Shared by the graph extractor and gate 4's raw-citation check (§6.3).
    """
    for span in _iter_markdown_spans(strip_code_spans(markdown_text)):
        yield span.target_raw


def extract_markdown_note_links(markdown_text: str) -> list[ExtractedLink]:
    """Extract relative Markdown links that may name a note (§4.4).

    Only `.md` targets qualify: without the extension a link cannot be told
    apart from one to a folder or a non-note file, and both Obsidian's
    Markdown link format and GitHub write it. Paths with a `raw` segment are
    citations, owned by gate 4's `RAW_LINK_BROKEN`, and never graph edges.
    """
    links_by_target: dict[str, ExtractedLink] = {}
    for span in _iter_markdown_spans(strip_code_spans(markdown_text)):
        if not span.target.lower().endswith(".md") or is_raw_path(span.target):
            continue
        if span.target_raw in links_by_target:
            links_by_target[span.target_raw].occurrences += 1
            continue
        links_by_target[span.target_raw] = ExtractedLink(
            target_raw=span.target_raw,
            target_stem=span.target,
            anchor=span.anchor,
            kind=LINK_KIND_MARKDOWN,
        )
    return list(links_by_target.values())


def resolve_relative_path(source_cid: str, path_part: str) -> str | None:
    """The vault-relative path a relative link names from its source note's folder.

    `path_part` is already decoded and carries no `#anchor`. Returns None when
    the path climbs out of the vault or is absolute.
    """
    if path_part.startswith("/"):
        return None
    source_dir = posixpath.dirname(cid_to_path(source_cid))
    joined = posixpath.normpath(posixpath.join(source_dir, path_part))
    if joined in (".", "..") or joined.startswith(("../", "/")):
        return None
    return joined


def resolve_markdown_target(source_cid: str, target_raw: str) -> str | None:
    """Return the CID a relative Markdown link names from its source note's folder.

    Resolution is by exact path only, never by stem: a stem fallback would
    silently absorb the one-`../`-too-many mistakes the dead-link check exists
    to catch. Returns None when the path climbs out of the vault.
    """
    joined = resolve_relative_path(source_cid, unquote(target_raw.split("#", 1)[0]))
    return None if joined is None else path_to_cid(joined)


def relative_note_path(source_cid: str, target_cid: str) -> str:
    """The relative Markdown path from source_cid's folder to target_cid's file."""
    source_dir = posixpath.dirname(cid_to_path(source_cid)) or "."
    return posixpath.relpath(cid_to_path(target_cid), source_dir)


class LinkTargetIndex:
    """Precomputed index for O(1) resolution of wikilink targets to canonical CIDs."""

    def __init__(self, known_cids: Sequence[str]) -> None:
        self._exact_map: dict[str, str] = {}
        self._suffix_map: dict[str, str] = {}
        self._stem_map: dict[str, str] = {}
        self.known_cids: frozenset[str] = frozenset(known_cids)

        # Prioritize wiki/ notes first so canonical knowledge takes precedence
        # for bare stem resolution (e.g. [[Architecture]]), while active domains
        # precede archive/ so retired notes never shadow live ones ("archive/"
        # sorts alphabetically before "customers/" or "projects/").
        wiki_prefix = f"{DIR_WIKI}/"
        archive_prefix = f"{DIR_ARCHIVE}/"
        sorted_cids = sorted(
            known_cids,
            key=lambda c: (0 if c.startswith(wiki_prefix) else (2 if c.startswith(archive_prefix) else 1), c),
        )

        for cid in sorted_cids:
            if cid not in self._exact_map:
                self._exact_map[cid] = cid
            if cid.startswith(wiki_prefix):
                short = cid.removeprefix(wiki_prefix)
                if short not in self._exact_map:
                    self._exact_map[short] = cid

            parts = cid.split("/")
            for i in range(1, len(parts) - 1):
                suffix = "/".join(parts[i:])
                if suffix not in self._suffix_map:
                    self._suffix_map[suffix] = cid

            stem = parts[-1]
            if stem not in self._stem_map:
                self._stem_map[stem] = cid

    def resolve(self, target_stem: str) -> str | None:
        """Resolve a target stem against the precomputed index in O(1) time."""
        clean = normalize_rel(target_stem).removesuffix(".md")

        # 1. Exact CID or wiki-relative match
        if clean in self._exact_map:
            return self._exact_map[clean]

        # 2. Path suffix match
        if clean in self._suffix_map:
            return self._suffix_map[clean]

        # 3. Bare stem only (§4.4). A target with a path segment names a
        #    location; falling back to its stem would hide exactly the typo
        #    gate 3 exists to catch (`[[customers/acmee/README]]`).
        return None if "/" in clean else self._stem_map.get(clean)

    def candidates(self, target_stem: str) -> list[str]:
        """Every note `target_stem` could mean, where `resolve()` picks one (§5.1).

        An exact CID or `wiki/`-relative name is canonical (§4.4) and never
        ambiguous; otherwise every note whose path ends with the target, or
        whose stem equals it, is a candidate. Active domain notes precede
        `archive/` notes so retiring a note does not make the remaining active
        note ambiguous. Write tools refuse more than one.
        """
        clean = normalize_rel(target_stem).removesuffix(".md")
        if clean in self._exact_map:
            return [self._exact_map[clean]]
        if "/" in clean:
            matches = sorted(c for c in self.known_cids if c.endswith("/" + clean))
        else:
            matches = sorted(c for c in self.known_cids if c.rsplit("/", 1)[-1] == clean)
        active = [c for c in matches if not c.startswith(f"{DIR_ARCHIVE}/")]
        return active or matches

    def suggest(self, target_path: str) -> str | None:
        """Best-guess CID for a missed path, by its final stem (hints only, never resolution)."""
        stem = normalize_rel(target_path).split("/")[-1].removesuffix(".md")
        return self._stem_map.get(stem)


def strip_link_wrapper(text: str) -> str:
    """Strip surrounding `[[ ]]` (or `![[ ]]`) and any `|alias` while keeping `#` intact."""
    clean = text.strip()
    if clean.startswith("![[") and clean.endswith("]]"):
        clean = clean[3:-2].strip()
    elif clean.startswith("[[") and clean.endswith("]]"):
        clean = clean[2:-2].strip()
    has_alias = "|" in clean
    without_alias = clean.split("|", 1)[0]
    if has_alias:
        without_alias = without_alias.rstrip("\\")
    return without_alias.strip()


def split_link_target_and_anchor(text: str) -> tuple[str, str | None]:
    """Split a CID, path, stem, or wikilink into `(target_stem, anchor)` (§5.1)."""
    unwrapped = strip_link_wrapper(text)
    if "#" not in unwrapped:
        return unwrapped, None
    target, anchor = unwrapped.split("#", 1)
    return target.strip(), (anchor.strip() or None)


def normalize_link_target(text: str) -> str:
    """Reduce what a caller typed to a resolvable target: strip `[[ ]]`, `|alias` and `#anchor`.

    Shared by `vault_ground` and `cadabby graph`, so both accept the same
    spellings and resolve them through the same `LinkTargetIndex`.
    """
    return split_link_target_and_anchor(text)[0]

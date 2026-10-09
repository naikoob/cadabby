"""Wikilink and Markdown note-link extraction, target resolution, and graph edge parsing.

Conforms to Cadabby Technical Specification §4.2, §4.4, §6.3.
"""

from __future__ import annotations

import posixpath
import re
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote

from cadabby.constants import DIR_RAW
from cadabby.domain import iter_fence_states
from cadabby.vault import cid_to_path, normalize_rel, path_to_cid

# Regex matching [[Target#Anchor|Alias]] (including table-escaped \| aliases)
# and note transclusions ![[Target#Anchor]], ignoring multiline brackets and
# code spans. Group 1 is the embed marker; attachment embeds are filtered below.
RE_WIKILINK = re.compile(r"(!?)\[\[([^\]|#\n]+?)(?:#([^\]|\n]+?))?(?:\\?\|([^\]\n]+))?\]\]")
RE_INLINE_CODE = re.compile(r"(`+)(?:(?!\1)[^\n])+\1")

# A final path segment ending in `.ext`, where ext is not `md`. An embed naming
# such a file (`![[diagram.png]]`, `![[flow.canvas]]`) is an attachment, not a
# note transclusion, and is never a graph edge (§4.4).
RE_ATTACHMENT_SUFFIX = re.compile(r"\.(?!md$)[A-Za-z0-9]+$", re.IGNORECASE)


def _is_attachment_target(target_stem: str) -> bool:
    """Whether an embed target names a non-note file by its extension."""
    return bool(RE_ATTACHMENT_SUFFIX.search(target_stem.rsplit("/", 1)[-1]))

# Inline Markdown link, not an image embed: [label](target "optional title").
# The angle-bracket form [label](<My Note.md>) is what Obsidian writes for
# paths containing spaces, so group 1 takes it and group 2 the bare form.
RE_MD_LINK = re.compile(r'(?<!!)\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|([^)\s]+))(?:\s+"[^"\n]*")?\s*\)')
RE_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")

LINK_KIND_WIKI = "wiki"
LINK_KIND_MARKDOWN = "markdown"


@dataclass
class ExtractedLink:
    """Represents a single wikilink or Markdown note-link reference in a markdown note."""

    target_raw: str  # Exactly as written in the link target, e.g. "Note#Heading" or "../wiki/Note.md#Heading"
    target_stem: str  # Note stem or decoded path part, e.g. "Note" or "../wiki/Note.md"
    anchor: str | None = None  # Heading anchor, e.g. "Heading"
    alias: str | None = None  # Link display alias, e.g. "alias"
    occurrences: int = 1
    kind: str = LINK_KIND_WIKI  # "wiki" for [[...]], "markdown" for [label](path.md)


def strip_code_spans(text: str) -> str:
    """Blank fenced code and inline code so neither yields links or headings.

    Fenced lines become empty rather than vanishing, so line positions survive.
    """
    unfenced = "\n".join("" if in_fence else line for line, in_fence in iter_fence_states(text.split("\n")))
    return RE_INLINE_CODE.sub(" ", unfenced)


def extract_wikilinks(markdown_text: str) -> list[ExtractedLink]:
    """Extract all distinct wikilinks and their occurrences from markdown text."""
    clean_text = strip_code_spans(markdown_text)
    links_by_target: dict[str, ExtractedLink] = {}

    for match in RE_WIKILINK.finditer(clean_text):
        is_embed, raw_target, raw_anchor_group, raw_alias = match.groups()
        alias = raw_alias.strip() if raw_alias else None
        raw_stem = (
            raw_target.rstrip("\\")
            if (alias is not None and not raw_anchor_group)
            else raw_target
        )
        target_stem = raw_stem.strip()
        if is_embed and _is_attachment_target(target_stem):
            continue
        raw_anchor = (
            raw_anchor_group.rstrip("\\")
            if (alias is not None and raw_anchor_group)
            else raw_anchor_group
        )
        anchor = raw_anchor.strip() if raw_anchor else None

        # Format target_raw: "Target#Anchor" or "Target"
        target_raw = f"{target_stem}#{anchor}" if anchor else target_stem

        if target_raw in links_by_target:
            links_by_target[target_raw].occurrences += 1
        else:
            links_by_target[target_raw] = ExtractedLink(
                target_raw=target_raw,
                target_stem=target_stem,
                anchor=anchor,
                alias=alias,
                occurrences=1,
            )

    return list(links_by_target.values())


def iter_markdown_link_targets(markdown_text: str) -> Iterator[str]:
    """Yield relative inline Markdown link targets outside code, exactly as written.

    URLs (any scheme), vault-absolute paths and same-note `#heading` jumps are
    skipped: none of them is a relative path a note could be addressed by.
    Shared by the graph extractor and gate 4's raw-citation check (§6.3).
    """
    for match in RE_MD_LINK.finditer(strip_code_spans(markdown_text)):
        target = (match.group(1) or match.group(2) or "").strip()
        if not target or target.startswith(("/", "#")) or RE_URL_SCHEME.match(target):
            continue
        yield target


def extract_markdown_note_links(markdown_text: str) -> list[ExtractedLink]:
    """Extract relative Markdown links that may name a note (§4.4).

    Only `.md` targets qualify: without the extension a link cannot be told
    apart from one to a folder or a non-note file, and both Obsidian's
    Markdown link format and GitHub write it. Paths with a `raw` segment are
    citations, owned by gate 4's `RAW_LINK_BROKEN`, and never graph edges.
    """
    links_by_target: dict[str, ExtractedLink] = {}
    for target in iter_markdown_link_targets(markdown_text):
        path_part, _, anchor = target.partition("#")
        decoded = unquote(path_part)
        if not decoded.lower().endswith(".md") or DIR_RAW in PurePosixPath(decoded).parts:
            continue
        if target in links_by_target:
            links_by_target[target].occurrences += 1
            continue
        links_by_target[target] = ExtractedLink(
            target_raw=target,
            target_stem=decoded,
            anchor=unquote(anchor).strip() or None,
            kind=LINK_KIND_MARKDOWN,
        )
    return list(links_by_target.values())


def resolve_markdown_target(source_cid: str, target_raw: str) -> str | None:
    """Return the CID a relative Markdown link names from its source note's folder.

    Resolution is by exact path only, never by stem: a stem fallback would
    silently absorb the one-`../`-too-many mistakes the dead-link check exists
    to catch. Returns None when the path climbs out of the vault.
    """
    source_dir = posixpath.dirname(cid_to_path(source_cid))
    path_part = unquote(target_raw.split("#", 1)[0])
    joined = posixpath.normpath(posixpath.join(source_dir, path_part))
    if joined in (".", "..") or joined.startswith("../"):
        return None
    return path_to_cid(joined)


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
        # for bare stem resolution (e.g. [[Architecture]]), while suffix matches
        # resolve unambiguously to specific domain notes.
        sorted_cids = sorted(known_cids, key=lambda c: (0 if c.startswith("wiki/") else 1, c))

        for cid in sorted_cids:
            if cid not in self._exact_map:
                self._exact_map[cid] = cid
            if cid.startswith("wiki/"):
                short = cid[5:]
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

    def suggest(self, target_path: str) -> str | None:
        """Best-guess CID for a missed path, by its final stem (hints only, never resolution)."""
        stem = normalize_rel(target_path).split("/")[-1].removesuffix(".md")
        return self._stem_map.get(stem)


def normalize_link_target(text: str) -> str:
    """Reduce what a caller typed to a resolvable target: strip `[[ ]]`, `|alias` and `#anchor`.

    Shared by `vault_ground` and `cadabby graph`, so both accept the same
    spellings and resolve them through the same `LinkTargetIndex`.
    """
    clean = text.strip()
    if clean.startswith("[[") and clean.endswith("]]"):
        clean = clean[2:-2].strip()
    return clean.split("|")[0].split("#")[0].strip()


def get_note_graph(conn: sqlite3.Connection, target_cid: str) -> dict[str, Any] | None:
    """Compute 1-hop and 2-hop neighborhoods, co-citations, and bibliographic coupling (§5.3)."""
    # 1. Check note existence
    cur = conn.execute(
        "SELECT cid, title, type, status, trust_tier, rel_path FROM notes WHERE cid = ?;",
        (target_cid,),
    )
    row = cur.fetchone()
    if not row:
        return None

    note_info = {
        "cid": row["cid"],
        "title": row["title"],
        "type": row["type"],
        "status": row["status"],
        "trust_tier": row["trust_tier"],
        "rel_path": row["rel_path"],
    }

    # 2. 1-hop forward links
    cur = conn.execute(
        "SELECT target_raw, target_cid, anchor, kind FROM links WHERE source_cid = ? ORDER BY target_cid ASC;",
        (target_cid,),
    )
    forward_links = [
        {"target_raw": r["target_raw"], "target_cid": r["target_cid"], "anchor": r["anchor"], "kind": r["kind"]}
        for r in cur.fetchall()
    ]
    forward_cids = {r["target_cid"] for r in forward_links if r["target_cid"]}

    # 3. 1-hop backlinks
    cur = conn.execute(
        "SELECT source_cid, target_raw, anchor, kind FROM links WHERE target_cid = ? ORDER BY source_cid ASC;",
        (target_cid,),
    )
    backlinks = [
        {"source_cid": r["source_cid"], "target_raw": r["target_raw"], "anchor": r["anchor"], "kind": r["kind"]}
        for r in cur.fetchall()
    ]
    backlink_cids = {r["source_cid"] for r in backlinks if r["source_cid"]}

    # 4. 2-hop forward links (targets of 1-hop forward targets)
    two_hop_forward: list[dict[str, Any]] = []
    if forward_cids:
        placeholders = ",".join("?" for _ in forward_cids)
        cur = conn.execute(
            f"""
            SELECT DISTINCT source_cid, target_cid
            FROM links
            WHERE source_cid IN ({placeholders})
              AND target_cid IS NOT NULL
              AND target_cid != ?
              AND target_cid NOT IN ({placeholders})
            ORDER BY target_cid ASC;
            """,
            (*forward_cids, target_cid, *forward_cids),
        )
        two_hop_forward = [{"via": r["source_cid"], "target_cid": r["target_cid"]} for r in cur.fetchall()]

    # 5. 2-hop backlinks (sources linking to 1-hop backlinks)
    two_hop_backlinks: list[dict[str, Any]] = []
    if backlink_cids:
        placeholders = ",".join("?" for _ in backlink_cids)
        cur = conn.execute(
            f"""
            SELECT DISTINCT source_cid, target_cid
            FROM links
            WHERE target_cid IN ({placeholders})
              AND source_cid != ?
              AND source_cid NOT IN ({placeholders})
            ORDER BY source_cid ASC;
            """,
            (*backlink_cids, target_cid, *backlink_cids),
        )
        two_hop_backlinks = [{"source_cid": r["source_cid"], "via": r["target_cid"]} for r in cur.fetchall()]

    # 6. Co-citations (notes cited alongside target_cid by common sources)
    cur = conn.execute(
        """
        SELECT l2.target_cid, COUNT(DISTINCT l1.source_cid) AS co_citations
        FROM links l1
        JOIN links l2 ON l1.source_cid = l2.source_cid
        WHERE l1.target_cid = ?
          AND l2.target_cid != ?
          AND l2.target_cid IS NOT NULL
        GROUP BY l2.target_cid
        ORDER BY co_citations DESC, l2.target_cid ASC;
        """,
        (target_cid, target_cid),
    )
    co_citations = [{"cid": r["target_cid"], "count": r["co_citations"]} for r in cur.fetchall()]

    # 7. Bibliographic coupling (notes that cite common targets as target_cid)
    cur = conn.execute(
        """
        SELECT l2.source_cid, COUNT(DISTINCT l1.target_cid) AS shared_targets
        FROM links l1
        JOIN links l2 ON l1.target_cid = l2.target_cid
        WHERE l1.source_cid = ?
          AND l2.source_cid != ?
          AND l1.target_cid IS NOT NULL
        GROUP BY l2.source_cid
        ORDER BY shared_targets DESC, l2.source_cid ASC;
        """,
        (target_cid, target_cid),
    )
    bibliographic_coupling = [{"cid": r["source_cid"], "count": r["shared_targets"]} for r in cur.fetchall()]

    return {
        "note": note_info,
        "forward_links": forward_links,
        "backlinks": backlinks,
        "two_hop_forward": two_hop_forward,
        "two_hop_backlinks": two_hop_backlinks,
        "co_citations": co_citations,
        "bibliographic_coupling": bibliographic_coupling,
    }



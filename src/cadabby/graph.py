"""Wikilink extraction, target resolution, and graph edge parsing.

Conforms to Cadabby Technical Specification §4.2, §6.3.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

# Regex matching [[Target#Anchor|Alias]] ignoring backtick code spans
RE_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#([^\]|]+))?(?:\|([^\]]+))?\]\]")
RE_CODE_BLOCK = re.compile(r"```[\s\S]*?```|`[^`\n]+`")


@dataclass
class ExtractedLink:
    """Represents a single wikilink reference in a markdown note."""

    target_raw: str  # Exactly as written in the link target, e.g. "Note#Heading"
    target_stem: str  # Note stem or path part, e.g. "Note"
    anchor: str | None = None  # Heading anchor, e.g. "Heading"
    alias: str | None = None  # Link display alias, e.g. "alias"
    occurrences: int = 1


def strip_code_spans(text: str) -> str:
    """Replace fenced code blocks and inline code with spaces to prevent false link matching."""
    return RE_CODE_BLOCK.sub(" ", text)


def extract_wikilinks(markdown_text: str) -> list[ExtractedLink]:
    """Extract all distinct wikilinks and their occurrences from markdown text."""
    clean_text = strip_code_spans(markdown_text)
    links_by_target: dict[str, ExtractedLink] = {}

    for match in RE_WIKILINK.finditer(clean_text):
        target_stem = match.group(1).strip()
        anchor = match.group(2).strip() if match.group(2) else None
        alias = match.group(3).strip() if match.group(3) else None

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


class LinkTargetIndex:
    """Precomputed index for O(1) resolution of wikilink targets to canonical CIDs."""

    def __init__(self, known_cids: Sequence[str]):
        self._exact_map: dict[str, str] = {}
        self._suffix_map: dict[str, str] = {}
        self._stem_map: dict[str, str] = {}

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
            for i in range(1, len(parts)):
                suffix = "/".join(parts[i:])
                if suffix not in self._suffix_map:
                    self._suffix_map[suffix] = cid

            stem = parts[-1]
            if stem not in self._stem_map:
                self._stem_map[stem] = cid

    def resolve(self, target_stem: str) -> str | None:
        """Resolve a target stem against the precomputed index in O(1) time."""
        clean = target_stem.replace("\\", "/").strip("/")
        clean = clean.removesuffix(".md")

        # 1. Exact CID or wiki-relative match
        if clean in self._exact_map:
            return self._exact_map[clean]

        # 2. Path suffix match
        if clean in self._suffix_map:
            return self._suffix_map[clean]

        # 3. Note stem match
        stem = clean.split("/")[-1]
        return self._stem_map.get(stem)


def resolve_link_target(target_stem: str, known_cids: Sequence[str]) -> str | None:
    """Resolve a target stem or relative path against known vault CIDs.

    Matches:
    1. Exact CID match (e.g. 'wiki/concepts/Note' -> 'wiki/concepts/Note').
    2. Path suffix match (e.g. 'concepts/Note' -> 'wiki/concepts/Note').
    3. Note stem match (e.g. 'Note' -> 'wiki/concepts/Note').
    Returns matching CID or None if unresolved (broken link).
    """
    return LinkTargetIndex(known_cids).resolve(target_stem)


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
        "SELECT target_raw, target_cid, anchor FROM links WHERE source_cid = ? ORDER BY target_cid ASC;",
        (target_cid,),
    )
    forward_links = [
        {"target_raw": r["target_raw"], "target_cid": r["target_cid"], "anchor": r["anchor"]}
        for r in cur.fetchall()
    ]
    forward_cids = {r["target_cid"] for r in forward_links if r["target_cid"]}

    # 3. 1-hop backlinks
    cur = conn.execute(
        "SELECT source_cid, target_raw, anchor FROM links WHERE target_cid = ? ORDER BY source_cid ASC;",
        (target_cid,),
    )
    backlinks = [
        {"source_cid": r["source_cid"], "target_raw": r["target_raw"], "anchor": r["anchor"]}
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



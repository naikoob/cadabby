"""Wikilink extraction, target resolution, and graph edge parsing.

Conforms to Cadabby Technical Specification §4.2, §6.3.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

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

        for cid in known_cids:
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
        if clean.endswith(".md"):
            clean = clean[:-3]

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


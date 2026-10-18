"""Markdown text scanning: code fences, inline code spans, ATX headings, and `##` sections.

Pure functions of text with no Cadabby imports. One model of each construct is
shared by link extraction (graph.py), section grounding (ground.py), lint's
anchor checks, and the Note aggregate's section edits, so a heading or fence
cannot mean one thing to a reader and another to a writer (§4.3, §5.1, §6.3).
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Iterable, Iterator
from urllib.parse import unquote

_BACKTICK_RUN_RE = re.compile(r"`+")
_ATX_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(\S.*?)\r?\n?$")


def iter_fence_states(lines: Iterable[str]) -> Iterator[tuple[str, bool]]:
    """Yield each line with whether it belongs to a fenced code block (delimiters included).

    The one code-fence model (CommonMark): a fence opens on a line starting
    with 3+ backticks or tildes and closes on a line of only that character,
    at least as long. Section splitting, link extraction and lint's heading
    scan all consume it, so a link inside a four-backtick fence is not an edge
    while the heading after it still is. An unclosed fence runs to the end.
    """
    fence = ""
    for line in lines:
        stripped = line.strip()
        if not fence:
            if stripped.startswith(("```", "~~~")):
                run = len(stripped) - len(stripped.lstrip(stripped[0]))
                if run >= 3:
                    fence = stripped[0] * run
                    yield line, True
                    continue
            yield line, False
        else:
            if stripped.startswith(fence) and not stripped.strip(fence[0]):
                fence = ""
            yield line, True


def code_spans(line: str) -> list[tuple[int, int, int, int]]:
    """Inline code spans on one line: `(open, content_start, content_end, close)` offsets.

    CommonMark's rule, in linear time: a backtick run opens a span that the
    next run of exactly the same length closes; a run with no partner is
    literal. The regex this replaces, `(`+)(?:(?!\\1)[^\\n])+\\1`, backtracked
    cubically: one 1 KB line of backticks took 17 s, and every scan of the
    vault parses every note (§4.3).
    """
    runs = [(m.start(), m.end()) for m in _BACKTICK_RUN_RE.finditer(line)]
    by_len: dict[int, list[int]] = {}
    for i, (start, end) in enumerate(runs):
        by_len.setdefault(end - start, []).append(i)
    spans: list[tuple[int, int, int, int]] = []
    i = 0
    while i < len(runs):
        start, end = runs[i]
        same = by_len[end - start]
        k = bisect_right(same, i)
        if k == len(same):
            i += 1  # no closing run of this length: literal backticks
            continue
        j = same[k]
        spans.append((start, end, runs[j][0], runs[j][1]))
        i = j + 1
    return spans


def unwrap_inline_code(text: str) -> str:
    """`text` with each inline code span replaced by its content (per line)."""
    out_lines = []
    for line in text.split("\n"):
        pieces, pos = [], 0
        for open_, c_start, c_end, close in code_spans(line):
            pieces += [line[pos:open_], line[c_start:c_end]]
            pos = close
        out_lines.append("".join(pieces) + line[pos:])
    return "\n".join(out_lines)


def _clean_heading_text(raw: str) -> str:
    """Strip optional closing ATX `#` sequence and unwrap inline code backticks.

    String operations, not `\\s+#+\\s*$`: that pattern was quadratic in a run
    of spaces. A closing sequence needs whitespace before it (`# C#` keeps
    its `#`), and a heading of only `#`s is left alone.
    """
    text = raw.strip()
    trimmed = text.rstrip("#")
    if trimmed != text and trimmed and trimmed[-1] in " \t":
        text = trimmed.rstrip()
    return unwrap_inline_code(text)


def extract_headings(body: str) -> list[str]:
    """Extract markdown ATX headings outside fenced code blocks, unwrapping inline code backticks."""
    unfenced = "\n".join(
        "" if in_fence else line for line, in_fence in iter_fence_states(body.split("\n"))
    )
    # `[ \t]`, not `\s`: with MULTILINE, `\s+` crosses the newline after a bare
    # `#`, promoting the next line to an invented heading.
    # Greedy, then trimmed in Python: a lazy body before `[ \t\r]*$` retried the
    # trailing-whitespace test at every character, quadratic in long lines.
    raw_headings = re.findall(r"^#{1,6}[ \t]+(\S[^\n]*)$", unfenced, flags=re.MULTILINE)
    return [_clean_heading_text(h.rstrip(" \t\r")) for h in raw_headings]


def heading_slug(text: str) -> str:
    """GitHub-style heading anchor: lowercase, punctuation dropped, spaces to hyphens."""
    unwrapped = unwrap_inline_code(text.strip())
    return re.sub(r"[^\w\- ]", "", unwrapped.lower()).replace(" ", "-")


def anchor_matches_heading(anchor: str, heading: str) -> bool:
    """Whether a link or grounding anchor names `heading` (Obsidian or GitHub-slug form)."""
    a = unwrap_inline_code(unquote(anchor).strip())
    if not a:
        return False
    a_norm = re.sub(r"[\s-]+", " ", a.lower())
    h_clean = heading.strip()
    if a.lower() == h_clean.lower() or a_norm == re.sub(r"[\s-]+", " ", h_clean.lower()):
        return True
    a_slug = heading_slug(a)
    return bool(a_slug and a_slug == heading_slug(h_clean))


def anchor_matches(anchor: str, headings: Iterable[str]) -> bool:
    """Whether a link anchor names one of the headings.

    Accepts the heading text as written (Obsidian), its space/hyphen-normalized
    form, and the GitHub slug, so `#b-tree-indexes-v2` finds `## B-Tree Indexes (v2)`.
    """
    return any(anchor_matches_heading(anchor, h) for h in headings)


def extract_markdown_section(body: str, anchor: str) -> tuple[str, str] | None:
    """Extract the ATX heading section matching `anchor` (including nested subsections).

    Scans lines outside fenced code blocks for the first heading at level k (1..6)
    matching `anchor`, and collects all lines up to the next non-fenced heading of
    level <= k. Returns `(matched_heading_title, section_text)` or None.
    """
    target_level: int | None = None
    matched_heading = ""
    collected: list[str] = []

    for line, in_fence in iter_fence_states(re.findall(r"[^\n]*\n|[^\n]+\Z", body)):
        if not in_fence and (m := _ATX_HEADING_RE.match(line)):
            level = len(m.group(1))
            heading_text = _clean_heading_text(m.group(2))
            if target_level is None:
                if anchor_matches_heading(anchor, heading_text):
                    target_level = level
                    matched_heading = heading_text
                    collected.append(line)
                continue
            if level <= target_level:
                break
        if target_level is not None:
            collected.append(line)

    if target_level is None:
        return None
    return matched_heading, "".join(collected)


def split_markdown_sections(body: str) -> list[tuple[str, str]]:
    """Split a body into `## ` sections, ignoring headings inside fenced code.

    Returns:
        List of (heading, full_section_text) tuples.
        The preamble before the first ## heading has heading="".
    """
    sections: list[tuple[str, str]] = []
    current_heading = ""
    current_lines: list[str] = []

    # Line endings are kept so the sections rejoin to the exact body; only
    # "\n" splits, matching the body hash (§3.3).
    # A level-2 heading by the same ATX rule ground and lint use: `##\tSetup`
    # and `## Intro ##` are sections too. With a looser `"## "` prefix test,
    # replace_section could not find a heading ground had just returned, and
    # appended a duplicate while reporting success.
    for line, in_fence in iter_fence_states(re.findall(r"[^\n]*\n|[^\n]+\Z", body)):
        m = None if in_fence else _ATX_HEADING_RE.match(line)
        if m and len(m.group(1)) == 2:
            if current_lines or current_heading:
                sections.append((current_heading, "".join(current_lines)))
                current_lines = []
            current_heading = _clean_heading_text(m.group(2))
        current_lines.append(line)

    if current_lines or current_heading:
        sections.append((current_heading, "".join(current_lines)))

    return sections


def section_heading_key(heading: str) -> str:
    """The form a requested `##` heading is matched in: as cleaned as the body's.

    Exact match after cleaning, never slug or case folding: a write that
    fuzzily picked a section could overwrite the wrong one.
    """
    return _clean_heading_text(heading.lstrip("#"))


def replace_markdown_section(body: str, heading: str, new_content: str) -> str:
    """Replace an existing ## heading section or append if not present, code-fence safe.

    The existing heading line is kept byte for byte (`## Intro ##` stays as
    written); only the content under it changes.
    """
    written = heading.lstrip("#").strip()
    key = section_heading_key(heading)
    sections = split_markdown_sections(body)
    found = False

    new_sections = []
    for h, content in sections:
        if h == key and not found:
            heading_line = content.splitlines(keepends=True)[0]
            if not heading_line.endswith("\n"):
                heading_line += "\n"
            new_sections.append(f"{heading_line}\n{new_content.strip()}\n\n")
            found = True
        else:
            new_sections.append(content)

    if not found:
        return body.rstrip() + f"\n\n## {written}\n\n{new_content.strip()}\n"

    return "".join(new_sections)

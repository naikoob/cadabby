"""Domain model and pure business entities for Cadabby vaults.

Conforms to Cadabby Technical Specification §3.
Free from filesystem I/O, SQLite dependencies, and network protocols.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from urllib.parse import unquote

from cadabby.constants import ACTOR_PATTERN
from cadabby.errors import HumanAttestationRefusedError, InvalidArgumentError
from cadabby.frontmatter import FrontmatterParseError, parse_frontmatter, serialize_frontmatter
from cadabby.okf import compute_body_hash, derive_trust_tier, is_valid_actor, utc_now_iso

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


# Written by the engine alone: `verified` only by vault_verify_note / cadabby
# verify, `generated` only at scaffold. A patch able to set them could mint a
# `human:*` attestation that vault_verify_note refuses, or erase the audit trail.
ENGINE_OWNED_FIELDS: tuple[str, ...] = ("verified", "generated")
_STRING_FIELDS: tuple[str, ...] = ("title", "description", "type", "status")


def validate_frontmatter_patch(patch: dict[str, Any] | None) -> None:
    """Refuse a patch that touches engine-owned fields or gives a core field a non-string value (§3.4, §5.1)."""
    if patch is None:
        return
    if not isinstance(patch, dict):
        raise InvalidArgumentError("patch_frontmatter must be an object of field: value pairs")
    owned = [k for k in ENGINE_OWNED_FIELDS if k in patch]
    if owned:
        entries = patch.get("verified")
        entries = entries if isinstance(entries, list) else [entries]
        if any(isinstance(e, dict) and str(e.get("by", "")).startswith("human:") for e in entries):
            raise HumanAttestationRefusedError(
                "A human:* attestation cannot be written by patching frontmatter; the person runs cadabby verify --human."
            )
        raise InvalidArgumentError(
            f"{', '.join(owned)} {'is' if len(owned) == 1 else 'are'} engine-owned: attest with vault_verify_note "
            "(or cadabby verify), and never remove attestation history"
        )
    for key in _STRING_FIELDS:
        if patch.get(key) is not None and not isinstance(patch[key], str):
            raise InvalidArgumentError(f"'{key}' must be a string")


@dataclass
class Note:
    """Core domain aggregate representing a typed knowledge note."""

    cid: str
    rel_path: str
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    source_hash: str | None = field(default=None, repr=False, compare=False)
    # The text this note was parsed from, if any. While frontmatter and body
    # still equal what it parses to, serialize() returns it verbatim, so a
    # rollback or a pure rename keeps comments, CRLF and quoting (§5.1, §8).
    raw_text: str | None = field(default=None, repr=False, compare=False)

    @property
    def title(self) -> str:
        return str(self.frontmatter.get("title", ""))

    @property
    def type(self) -> str:
        # Missing reads as missing (""), not an invented default: `vault_ground`
        # must show the same gap lint reports as FIELD_MISSING (§3.1).
        return str(self.frontmatter.get("type", ""))

    @property
    def description(self) -> str:
        return str(self.frontmatter.get("description", ""))

    @property
    def status(self) -> str:
        return str(self.frontmatter.get("status", ""))

    @property
    def tags(self) -> list[str]:
        val = self.frontmatter.get("tags")
        return list(val) if isinstance(val, list) else []

    @property
    def sources(self) -> list[str]:
        val = self.frontmatter.get("sources")
        return list(val) if isinstance(val, list) else []

    @property
    def verified(self) -> list[dict[str, Any]]:
        val = self.frontmatter.get("verified")
        return list(val) if isinstance(val, list) else []

    @property
    def generated(self) -> dict[str, Any]:
        val = self.frontmatter.get("generated")
        return dict(val) if isinstance(val, dict) else {}

    @property
    def body_hash(self) -> str:
        """Derive normative SHA-256 body hash (§3.3)."""
        return compute_body_hash(self.body)

    @property
    def trust_tier(self) -> str:
        """Derive dynamic trust tier from verified records and body hash (§3.4)."""
        return derive_trust_tier(self.verified, self.body_hash)

    def patch_frontmatter(self, patch: dict[str, Any]) -> None:
        """Apply frontmatter updates (None value removes key).

        The engine-owned-field guard is the aggregate's own invariant, not a
        caller's courtesy: no path through `patch_frontmatter` can write
        `verified` or `generated` (§3.4).
        """
        validate_frontmatter_patch(patch)
        for k, v in patch.items():
            if v is None:
                self.frontmatter.pop(k, None)
            else:
                self.frontmatter[k] = v

    def replace_section(self, heading: str, content: str) -> None:
        """Replace section under ## heading or append if not present (code-fence safe)."""
        self.body = replace_markdown_section(self.body, heading, content)

    def append_section(self, heading: str, content: str) -> None:
        """Append a section under ## heading to the end of the body."""
        clean_heading = heading.lstrip("#").strip()
        self.body = self.body.rstrip() + f"\n\n## {clean_heading}\n\n{content.strip()}\n"

    def replace_text(self, old: str, new: str) -> None:
        """Replace one exact, unique occurrence of `old` in the body (§5.1).

        Reaches anywhere in the body, including the H1 and preamble above the
        first `##` heading that section operations cannot address. Frontmatter
        is never reachable: it is a separate field, and `verified`/`generated`
        are engine-owned. Zero or multiple matches raise rather than guess.
        """
        if not old:
            raise InvalidArgumentError("replace_text: 'old' must be a non-empty string")
        count = self.body.count(old)
        if count != 1:
            raise InvalidArgumentError(
                f"replace_text: 'old' must match exactly once in the body of {self.rel_path} "
                f"(matched {count} times); include more surrounding text to make it unique"
            )
        self.body = self.body.replace(old, new, 1)

    def add_attestation(
        self,
        actor: str,
        method: str = "manual-review",
        at_iso: str | None = None,
    ) -> tuple[dict[str, Any], str, bool]:
        """Add an attestation bound to the current body hash.

        Returns:
            (attestation_dict, derived_trust_tier, already_verified)
        """
        if not is_valid_actor(actor):
            raise InvalidArgumentError(f"Invalid actor format '{actor}'. Must match {ACTOR_PATTERN}")

        current_hash = self.body_hash
        verified_list = self.frontmatter.get("verified")
        if not isinstance(verified_list, list):
            verified_list = []
            self.frontmatter["verified"] = verified_list

        # Deduplicate exact matching attestation
        for entry in verified_list:
            if isinstance(entry, dict) and entry.get("by") == actor and entry.get("of") == current_hash:
                return (entry, self.trust_tier, True)

        now_str = at_iso or utc_now_iso()
        attestation = {
            "by": actor,
            "at": now_str,
            "method": method,
            "of": current_hash,
        }
        verified_list.append(attestation)
        return (attestation, self.trust_tier, False)

    def serialize(self) -> str:
        """Serialize frontmatter and body into OKF markdown text.

        An unchanged note returns the exact text it was read from; anything
        else goes through the §3.2 writer.
        """
        if self.raw_text is not None:
            try:
                fm, body = parse_frontmatter(self.raw_text)
            except FrontmatterParseError:
                fm, body = None, None
            if fm == self.frontmatter and body == self.body:
                return self.raw_text
        return serialize_frontmatter(self.frontmatter, self.body, newline=self.newline)

    @property
    def newline(self) -> str:
        """The line ending the note was read with, judged by its first line (§8: minimal diffs)."""
        if self.raw_text is not None:
            first_end = self.raw_text.find("\n")
            if first_end > 0 and self.raw_text[first_end - 1] == "\r":
                return "\r\n"
        return "\n"

    @classmethod
    def new(
        cls,
        *,
        cid: str,
        rel_path: str,
        type_: str,
        title: str,
        description: str,
        actor: str,
        tags: list[str] | None = None,
        sources: list[str] | None = None,
        body: str = "",
        at_iso: str | None = None,
    ) -> Note:
        """A freshly scaffolded note: draft, unverified, attributed to `actor` (§3.1, §3.4)."""
        frontmatter: dict[str, Any] = {
            "type": type_,
            "title": title,
            "description": description,
            "status": "draft",
            "tags": tags or [],
            "sources": sources or [],
            "verified": [],
            "generated": {"by": actor, "at": at_iso or utc_now_iso()},
        }
        return cls(cid=cid, rel_path=rel_path, frontmatter=frontmatter, body=body.strip() or f"# {title}\n\n")

    @classmethod
    def from_raw(cls, cid: str, rel_path: str, raw_text: str, source_hash: str | None = None) -> Note:
        """Construct Note entity by parsing raw OKF markdown.

        `source_hash` is the storage adapter's concurrency token for the
        content it read; the adapter owns what "unchanged" means (§8).
        """
        fm, body = parse_frontmatter(raw_text)
        return cls(cid=cid, rel_path=rel_path, frontmatter=fm, body=body, source_hash=source_hash, raw_text=raw_text)


@dataclass
class VerificationResult:
    cid: str
    actor: str
    of: str
    trust_tier: str
    already_verified: bool


@dataclass
class DomainDefinition:
    """Represents a self-describing, self-governing cognitive domain."""

    name: str
    path: Path
    description: str = ""
    allowed_types: list[str] | None = None  # None = open/permissive
    require_sources: bool = False
    directives_markdown: str = ""
    # Why `{domain}/AGENTS.md` could not be honored, if it could not (§2.2).
    # Set, the domain is treated as open but lint reports it and scaffold refuses.
    manifest_error: str | None = None

    @classmethod
    def permissive(cls, name: str, path: Path, description: str | None = None) -> DomainDefinition:
        """An open domain with no manifest: any type, no source requirement (§2.2)."""
        return cls(name=name, path=path, description=description or f"{name.capitalize()} domain")


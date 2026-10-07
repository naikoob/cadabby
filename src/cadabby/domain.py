"""Domain model and pure business entities for Cadabby vaults.

Conforms to Cadabby Technical Specification §3.
Free from filesystem I/O, SQLite dependencies, and network protocols.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cadabby.frontmatter import parse_frontmatter, serialize_frontmatter
from cadabby.okf import compute_body_hash, derive_trust_tier, is_valid_actor


def split_markdown_sections(body: str) -> list[tuple[str, str]]:
    """Split markdown body into sections delineated by top-level headings (## ),
    strictly ignoring any heading lines inside fenced code blocks (``` or ~~~).

    Returns:
        List of (heading, full_section_text) tuples.
        The preamble before the first ## heading has heading="".
    """
    lines = body.splitlines(keepends=True)
    sections: list[tuple[str, str]] = []

    current_heading = ""
    current_lines: list[str] = []

    in_fence = False
    fence_char = ""
    fence_len = 0

    for line in lines:
        stripped = line.strip()
        # Check code fence start/end
        if not in_fence:
            if stripped.startswith(("```", "~~~")):
                f_char = stripped[0]
                f_len = len(stripped) - len(stripped.lstrip(f_char))
                if f_len >= 3:
                    in_fence = True
                    fence_char = f_char
                    fence_len = f_len
        else:
            if stripped.startswith(fence_char * fence_len):
                in_fence = False
                fence_char = ""
                fence_len = 0

        # Heading detection outside code blocks
        if not in_fence and line.startswith("## "):
            if current_lines or current_heading:
                sections.append((current_heading, "".join(current_lines)))
                current_lines = []
            current_heading = line[3:].strip()
            current_lines.append(line)
        else:
            current_lines.append(line)

    if current_lines or current_heading:
        sections.append((current_heading, "".join(current_lines)))

    return sections


def replace_markdown_section(body: str, heading: str, new_content: str) -> str:
    """Replace an existing ## heading section or append if not present, code-fence safe."""
    clean_heading = heading.lstrip("#").strip()
    sections = split_markdown_sections(body)
    found = False
    new_section_text = f"## {clean_heading}\n\n{new_content.strip()}\n\n"

    new_sections = []
    for h, content in sections:
        if h == clean_heading and not found:
            new_sections.append(new_section_text)
            found = True
        else:
            new_sections.append(content)

    if not found:
        return body.rstrip() + f"\n\n## {clean_heading}\n\n{new_content.strip()}\n"

    return "".join(new_sections)


@dataclass
class Note:
    """Core domain aggregate representing a typed knowledge note."""

    cid: str
    rel_path: str
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    source_hash: str | None = field(default=None, repr=False, compare=False)

    @property
    def title(self) -> str:
        return str(self.frontmatter.get("title", ""))

    @property
    def type(self) -> str:
        return str(self.frontmatter.get("type", "concept"))

    @property
    def description(self) -> str:
        return str(self.frontmatter.get("description", ""))

    @property
    def status(self) -> str:
        return str(self.frontmatter.get("status", "draft"))

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
        """Apply frontmatter updates (None value removes key)."""
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
            raise ValueError("replace_text: 'old' must be a non-empty string")
        count = self.body.count(old)
        if count != 1:
            raise ValueError(
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
            raise ValueError(f"Invalid actor format '{actor}'. Must match ^(human|agent|process):[A-Za-z0-9._\\-/]+$")

        current_hash = self.body_hash
        verified_list = self.frontmatter.get("verified")
        if not isinstance(verified_list, list):
            verified_list = []
            self.frontmatter["verified"] = verified_list

        # Deduplicate exact matching attestation
        for entry in verified_list:
            if isinstance(entry, dict) and entry.get("by") == actor and entry.get("of") == current_hash:
                return (entry, self.trust_tier, True)

        now_str = at_iso or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        attestation = {
            "by": actor,
            "at": now_str,
            "method": method,
            "of": current_hash,
        }
        verified_list.append(attestation)
        return (attestation, self.trust_tier, False)

    def serialize(self) -> str:
        """Serialize frontmatter and body into canonical OKF markdown text."""
        return serialize_frontmatter(self.frontmatter, self.body)

    @classmethod
    def from_raw(cls, cid: str, rel_path: str, raw_text: str) -> Note:
        """Construct Note entity by parsing raw OKF markdown."""
        fm, body = parse_frontmatter(raw_text)
        source_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        return cls(cid=cid, rel_path=rel_path, frontmatter=fm, body=body, source_hash=source_hash)


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


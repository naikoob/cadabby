"""Domain model and pure business entities for Cadabby vaults.

Conforms to Cadabby Technical Specification §3.
Free from filesystem I/O, SQLite dependencies, and network protocols.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from cadabby.frontmatter import parse_frontmatter, serialize_frontmatter
from cadabby.okf import compute_body_hash, derive_trust_tier, is_valid_actor


@dataclass
class Note:
    """Core domain aggregate representing a typed knowledge note."""

    cid: str
    rel_path: str
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""

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
        """Replace section under ## heading or append if not present."""
        pattern = rf"(^##\s+{re.escape(heading)}\s*$\n?)([\s\S]*?)(?=^##\s|\Z)"
        replacement = f"## {heading}\n\n{content.strip()}\n\n"
        if re.search(pattern, self.body, flags=re.MULTILINE):
            self.body = re.sub(pattern, replacement, self.body, flags=re.MULTILINE)
        else:
            self.body = self.body.rstrip() + f"\n\n## {heading}\n\n{content.strip()}\n"

    def append_section(self, heading: str, content: str) -> None:
        """Append a section under ## heading to the end of the body."""
        self.body = self.body.rstrip() + f"\n\n## {heading}\n\n{content.strip()}\n"

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

        now_str = at_iso or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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
        return cls(cid=cid, rel_path=rel_path, frontmatter=fm, body=body)


@dataclass
class VerificationResult:
    cid: str
    actor: str
    of: str
    trust_tier: str
    already_verified: bool

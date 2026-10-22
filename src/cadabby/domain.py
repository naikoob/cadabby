"""Domain model and pure business entities for Cadabby vaults.

Conforms to Cadabby Technical Specification §3.
Free from filesystem I/O, SQLite dependencies, and network protocols.
Markdown text scanning (fences, headings, sections) lives in `markdown.py`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cadabby.constants import ACTOR_PATTERN, NOTE_STATUSES, SCHEMA_KEY_ORDER
from cadabby.errors import HumanAttestationRefusedError, InvalidArgumentError, VerificationRefusedError
from cadabby.frontmatter import FrontmatterParseError, parse_frontmatter, serialize_frontmatter
from cadabby.markdown import replace_markdown_section, unverified_supplement_lines
from cadabby.okf import (
    canonicalize_aliases,
    compute_body_hash,
    derive_trust_tier,
    is_actor_human,
    is_valid_actor,
    utc_now_iso,
)


# Written by the engine alone: `verified` only by vault_verify_note / cadabby
# verify, `generated` only at scaffold. A patch able to set them could mint a
# `human:*` attestation that vault_verify_note refuses, or erase the audit trail.
ENGINE_OWNED_FIELDS: tuple[str, ...] = ("verified", "generated")
_STRING_FIELDS: tuple[str, ...] = ("title", "description", "type", "status")
_SCHEMA_KEYS: frozenset[str] = frozenset(SCHEMA_KEY_ORDER)
# Schema keys `Note.new` takes as arguments. Inside scaffold's `frontmatter` they
# used to be overwritten silently -- or, for `aliases`, kept uncanonicalized.
_ARGUMENT_KEYS: frozenset[str] = _SCHEMA_KEYS - frozenset(ENGINE_OWNED_FIELDS)


def _coerce_custom_numbers(val: Any) -> Any:
    """Coerce finite JSON int/float scalars in custom frontmatter fields to strings (§3.2, §5.1)."""
    if isinstance(val, bool) or val is None or isinstance(val, str):
        return val
    if isinstance(val, int) or (isinstance(val, float) and math.isfinite(val)):
        return str(val)
    if isinstance(val, list):
        return [_coerce_custom_numbers(item) for item in val]
    if isinstance(val, dict):
        return {k: _coerce_custom_numbers(v) for k, v in val.items()}
    return val


def validate_frontmatter_patch(patch: dict[str, Any] | None) -> None:
    """Refuse a patch that touches engine-owned fields or gives a core field an invalid value (§3.4, §5.1)."""
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
    if "cid" in patch:
        raise InvalidArgumentError("'cid' is derived from the note path and cannot be set in frontmatter")
    for key in _STRING_FIELDS:
        if patch.get(key) is not None:
            if not isinstance(patch[key], str):
                raise InvalidArgumentError(f"'{key}' must be a string")
            if not patch[key].strip():
                raise InvalidArgumentError(f"'{key}' must not be empty")
    if patch.get("status") is not None and patch["status"] not in NOTE_STATUSES:
        raise InvalidArgumentError(f"Invalid status {patch['status']!r}. Must be one of {NOTE_STATUSES}")
    if patch.get("aliases") is not None:
        canonicalize_aliases(patch["aliases"])


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
    def aliases(self) -> list[str]:
        val = self.frontmatter.get("aliases")
        return list(val) if isinstance(val, list) else []

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
                self.frontmatter[k] = v if k in _SCHEMA_KEYS else _coerce_custom_numbers(v)

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

    def machine_attestation_refusal(self, method: str) -> str | None:
        """Why an agent or process may not attest this note, or None (§3.4).

        Two contradictions the engine can see: the body fences text as
        unattested (an Unverified Supplement callout, under any method, since
        the MCP default `automated-check` would otherwise walk past it), and
        `source-crosscheck` claimed for a note that cites no source. Other
        methods on an unsourced note (a CI test of a runbook) are legitimate.
        `cadabby verify --human` shows the same reason as a warning.
        """
        if lines := unverified_supplement_lines(self.body):
            where = ", ".join(str(n) for n in lines)
            return f"the body has {len(lines)} Unverified Supplement callout(s) (body line {where})"
        if method == "source-crosscheck" and not self.frontmatter.get("sources"):
            return "method 'source-crosscheck' claims a check against sources, but `sources:` is empty"
        return None

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
        # Before the dedupe below, so the answer does not depend on whether a
        # matching entry already exists. Humans may vouch for anything (§3.5).
        if not is_actor_human(actor) and (why := self.machine_attestation_refusal(method)):
            raise VerificationRefusedError(
                f"Refusing {actor} on {self.cid}: {why}; a machine attestation would contradict the note. "
                f"Leave it unverified, ground the claim in raw/ first, or ask a person to run "
                f"`cadabby verify {self.cid} --human`."
            )

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
        status: str = "draft",
        aliases: list[str] | None = None,
        tags: list[str] | None = None,
        sources: list[str] | None = None,
        extra_frontmatter: dict[str, Any] | None = None,
        body: str = "",
        at_iso: str | None = None,
    ) -> Note:
        """A freshly scaffolded note: unverified, attributed to `actor` (§3.1, §3.4)."""
        if not isinstance(status, str) or status not in NOTE_STATUSES:
            raise InvalidArgumentError(f"Invalid status {status!r}. Must be one of {NOTE_STATUSES}")
        clean_aliases = canonicalize_aliases(aliases) if aliases is not None else []
        validate_frontmatter_patch(extra_frontmatter)
        if extra_frontmatter and (taken := sorted(_ARGUMENT_KEYS & extra_frontmatter.keys())):
            one = len(taken) == 1
            raise InvalidArgumentError(
                f"{', '.join(repr(k) for k in taken)} {'has its' if one else 'have their'} own scaffold "
                f"argument; pass {'it' if one else 'them'} there, not inside 'frontmatter'"
            )
        # Only custom keys remain, so every value takes the custom-field coercion.
        frontmatter: dict[str, Any] = {
            k: _coerce_custom_numbers(v) for k, v in (extra_frontmatter or {}).items() if v is not None
        }
        frontmatter.update({
            "type": type_,
            "title": title,
            "description": description,
            "status": status,
            "tags": tags or [],
            "sources": sources or [],
            "verified": [],
            "generated": {"by": actor, "at": at_iso or utc_now_iso()},
        })
        if clean_aliases:
            frontmatter["aliases"] = clean_aliases
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


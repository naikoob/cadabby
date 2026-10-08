"""Open Knowledge Format (OKF v0.2) schema validation, body hashing, and trust tier derivation.

Conforms to Cadabby Technical Specification §3.1, §3.3, §3.4.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

from cadabby.constants import (
    ACTOR_PATTERN,
    BODY_HASH_PREFIX,
    RFC3339_TIMESTAMP_PATTERN,
)

RE_ACTOR = re.compile(ACTOR_PATTERN)
RE_TIMESTAMP = re.compile(RFC3339_TIMESTAMP_PATTERN)

# Canonical tag grammar (§3.1): lowercase kebab-case segments joined by '/'.
# Segments are unicode letters/digits so non-English tags conform. Whitespace is
# excluded by construction: the cache stores tags space-joined (§4.2), so a tag
# containing a space makes the `--tag` facet match notes that do not carry it.
_TAG_SEGMENT = r"[^\W_]+(?:-[^\W_]+)*"
RE_TAG = re.compile(rf"^{_TAG_SEGMENT}(?:/{_TAG_SEGMENT})*$")


def utc_now_iso() -> str:
    """The current UTC time in the one timestamp form OKF writes (§3.1): `YYYY-MM-DDTHH:MM:SSZ`."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def split_lines(text: str) -> list[str]:
    """Split on line endings only (§3.3): `\r\n`, `\r` and `\n`.

    `str.splitlines()` also breaks on U+2028, U+0085, form feed and vertical
    tab, so the parser saw line breaks the file does not have and the body
    hash disagreed with any implementation that follows the spec.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def compute_body_hash(body: str) -> str:
    """Compute normative body hash conforming strictly to §3.3.

    1. Decode UTF-8 (assumed in Python str).
    2. Normalize line endings to \\n.
    3. Strip trailing whitespace from each line.
    4. Strip leading and trailing blank lines.
    5. Encode UTF-8, sha256 lowercase hex prefixed with 'sha256:'.
    """
    lines = [line.rstrip() for line in split_lines(body)]

    start = 0
    while start < len(lines) and not lines[start]:
        start += 1

    end = len(lines)
    while end > start and not lines[end - 1]:
        end -= 1

    normalized = "\n".join(lines[start:end])
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"{BODY_HASH_PREFIX}{digest}"


def is_valid_actor(actor: Any) -> bool:
    """Validate actor string against ^(human|agent|process):[A-Za-z0-9._\\-/]+$."""
    if not isinstance(actor, str):
        return False
    return bool(RE_ACTOR.match(actor))


def is_actor_human(actor: str) -> bool:
    """Return True if actor represents a human identity."""
    return actor.startswith("human:")


def is_actor_machine(actor: str) -> bool:
    """Return True if actor represents an autonomous agent or automated process."""
    return actor.startswith(("agent:", "process:"))


def tag_has_whitespace(tag: Any) -> bool:
    """Return True if the tag contains whitespace, which breaks `--tag` filtering."""
    return isinstance(tag, str) and any(ch.isspace() for ch in tag)


def is_canonical_tag(tag: Any) -> bool:
    """Return True if the tag is in canonical lowercase kebab-case form (§3.1)."""
    if not isinstance(tag, str):
        return False
    return tag == tag.lower() and bool(RE_TAG.match(tag))


def canonicalize_tags(tags: Any) -> list[str]:
    """Lowercase and de-duplicate tags, preserving first-seen order (§3.2).

    Raises:
        ValueError: if a tag contains whitespace. Lowercasing cannot repair that,
            and writing it would corrupt the `--tag` facet (§4.2).
    """
    if not isinstance(tags, list):
        return []

    canonical: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        if not isinstance(tag, str):
            raise ValueError(f"Invalid tag {tag!r}. Tags must be strings")
        if tag_has_whitespace(tag):
            raise ValueError(
                f"Invalid tag '{tag}'. Tags must not contain whitespace; "
                "use kebab-case (e.g. 'machine-learning')"
            )
        normalized = tag.lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            canonical.append(normalized)
    return canonical


def is_valid_timestamp(ts: Any) -> bool:
    """Return True if ts is a real RFC 3339 UTC instant.

    Shape and calendar are both checked. The pattern alone admits
    '2026-13-45T99:99:99Z', which has the right digits and is not a date; lint
    promises RFC 3339 conformance (§3.1), so the parse has to agree.
    """
    if not isinstance(ts, str) or not RE_TIMESTAMP.match(ts):
        return False
    try:
        datetime.fromisoformat(ts)
    except ValueError:
        return False
    return True


def derive_trust_tier(verified_list: list[Any] | None, current_body_hash: str) -> str:
    """Derive the epistemic trust tier from valid verification entries (§3.4).

    An entry is valid when it is a mapping with a well-formed `by:` actor and
    an `of:` equal to `current_body_hash`. Valid human beats valid machine;
    a non-empty list with no valid entry is `stale-verified` -- including
    records too broken to ever have bound, which stay visible as debt rather
    than reading as "never verified". Absent or empty is `unverified`.
    """
    if not verified_list or not isinstance(verified_list, list):
        return "unverified"

    valid_actors = [
        item["by"]
        for item in verified_list
        if isinstance(item, dict)
        and isinstance(item.get("by"), str)
        and is_valid_actor(item["by"])
        and item.get("of") == current_body_hash
    ]
    if any(is_actor_human(a) for a in valid_actors):
        return "human-reviewed"
    if any(is_actor_machine(a) for a in valid_actors):
        return "machine-confirmed"
    return "stale-verified"

"""Open Knowledge Format (OKF v0.2) schema validation, body hashing, and trust tier derivation.

Conforms to Cadabby Technical Specification §3.1, §3.3, §3.4.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
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


def compute_body_hash(body: str) -> str:
    """Compute normative body hash conforming strictly to §3.3.

    1. Decode UTF-8 (assumed in Python str).
    2. Normalize line endings to \\n.
    3. Strip trailing whitespace from each line.
    4. Strip leading and trailing blank lines.
    5. Encode UTF-8, sha256 lowercase hex prefixed with 'sha256:'.
    """
    lines = [line.rstrip() for line in body.splitlines()]

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
    """Derive epistemic trust tier from valid verification entries conforming to §3.4.

    A verification entry is valid when its 'of:' field is present and equals current_body_hash.
    Tiers:
    - 'human-reviewed': At least one valid entry with by: human:*
    - 'machine-confirmed': No valid human entry, but at least one valid entry with by: agent:* or process:*
    - 'stale-verified': at least one attestation record exists, but none is valid —
      including records broken enough to never have bound (no 'of:', bad actor).
      Preserving them as debt is the point; silently ignoring them would read as
      'never verified' and hide the breakage.
    - 'unverified': verified is absent, empty, or contains no attestation records
    """
    if not verified_list or not isinstance(verified_list, list):
        return "unverified"

    valid_human = False
    valid_machine = False
    has_any_attestation = False

    for item in verified_list:
        if not isinstance(item, dict):
            continue
        has_any_attestation = True

        actor = item.get("by")
        target_hash = item.get("of")

        if not isinstance(actor, str) or not is_valid_actor(actor):
            continue

        # Check content binding
        if target_hash and target_hash == current_body_hash:
            if is_actor_human(actor):
                valid_human = True
            elif is_actor_machine(actor):
                valid_machine = True

    if valid_human:
        return "human-reviewed"
    if valid_machine:
        return "machine-confirmed"
    if has_any_attestation:
        return "stale-verified"
    return "unverified"

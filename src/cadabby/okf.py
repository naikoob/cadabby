"""Open Knowledge Format (OKF v0.2) schema validation, body hashing, and trust tier derivation.

Conforms to Cadabby Technical Specification §3.1, §3.3, §3.4.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from cadabby.constants import (
    ACTOR_PATTERN,
    BODY_HASH_PREFIX,
    RFC3339_TIMESTAMP_PATTERN,
)

RE_ACTOR = re.compile(ACTOR_PATTERN)
RE_TIMESTAMP = re.compile(RFC3339_TIMESTAMP_PATTERN)


def compute_body_hash(body: str) -> str:
    """Compute normative body hash conforming strictly to §3.3.

    1. Decode UTF-8 (assumed in Python str).
    2. Normalize line endings to \\n.
    3. Strip trailing whitespace from each line.
    4. Strip leading and trailing blank lines.
    5. Encode UTF-8, sha256 lowercase hex prefixed with 'sha256:'.
    """
    lines = [line.rstrip() for line in body.splitlines()]

    # Strip leading blank lines
    while lines and not lines[0]:
        lines.pop(0)

    # Strip trailing blank lines
    while lines and not lines[-1]:
        lines.pop()

    normalized = "\n".join(lines)
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
    return actor.startswith("agent:") or actor.startswith("process:")


def is_valid_timestamp(ts: Any) -> bool:
    """Return True if timestamp matches RFC 3339 UTC format."""
    if not isinstance(ts, str):
        return False
    return bool(RE_TIMESTAMP.match(ts))


def derive_trust_tier(verified_list: list[Any] | None, current_body_hash: str) -> str:
    """Derive epistemic trust tier from valid verification entries conforming to §3.4.

    A verification entry is valid when its 'of:' field is present and equals current_body_hash.
    Tiers:
    - 'human-reviewed': At least one valid entry with by: human:*
    - 'machine-confirmed': No valid human entry, but at least one valid entry with by: agent:* or process:*
    - 'stale-verified': verified is non-empty, but no entry is valid
    - 'unverified': verified is absent, empty, or carries no valid structure
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

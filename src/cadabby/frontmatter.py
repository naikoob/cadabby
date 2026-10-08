"""Restricted YAML subset parser and canonicalizing writer for OKF frontmatter.

Normative implementation conforming to Cadabby Technical Specification §3.2.
Zero external dependencies; fails loudly on anything outside the accepted grammar.
"""

from __future__ import annotations

import re
from typing import Any

from cadabby.constants import BODY_HASH_PREFIX, SCHEMA_KEY_ORDER
from cadabby.errors import CadabbyError
from cadabby.errors import FRONTMATTER_UNPARSEABLE as _CODE_UNPARSEABLE
from cadabby.errors import FRONTMATTER_UNSERIALIZABLE as _CODE_UNSERIALIZABLE
from cadabby.okf import RE_TIMESTAMP, canonicalize_tags, split_lines

RE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
RE_BARE_SAFE = re.compile(r"^[A-Za-z0-9._\-/]+$")


class FrontmatterParseError(CadabbyError, ValueError):
    """Raised when frontmatter violates the restricted YAML subset.

    ValueError stays in the bases because `vault.py` catches
    `(FrontmatterParseError, ValueError)` and `cache.py` relies on the
    builtin arm elsewhere; removing it would reroute live handlers.
    """

    code = _CODE_UNPARSEABLE

    def __init__(self, message: str, line_number: int) -> None:
        self.message = message
        self.line_number = line_number
        super().__init__(f"Line {line_number}: {message}")


def split_comment(line: str) -> tuple[str, str | None]:
    """Split a line into content and trailing comment, respecting quotes."""
    in_single = False
    in_double = False
    escaped = False

    for i, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_double:
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            continue
        if ch == "#" and not in_single and not in_double and (i == 0 or line[i - 1].isspace()):
            return line[:i], line[i:]

    return line, None


def parse_scalar(raw: str, line_no: int) -> str | bool | None:
    """Parse a scalar conforming to the restricted YAML subset."""
    val = raw.strip()
    if not val:
        return ""

    # Double-quoted scalar
    if val.startswith('"'):
        if len(val) < 2 or not val.endswith('"'):
            raise FrontmatterParseError("Unclosed double-quoted string", line_no)
        inner = val[1:-1]
        result = []
        i = 0
        while i < len(inner):
            ch = inner[i]
            if ch == "\\":
                if i + 1 >= len(inner):
                    raise FrontmatterParseError("Dangling backslash in escape sequence", line_no)
                esc = inner[i + 1]
                if esc == "\\":
                    result.append("\\")
                elif esc == '"':
                    result.append('"')
                elif esc == "n":
                    result.append("\n")
                elif esc == "t":
                    result.append("\t")
                else:
                    raise FrontmatterParseError(f"Unrecognized escape sequence '\\{esc}'", line_no)
                i += 2
            elif ch == '"':
                raise FrontmatterParseError("Unescaped double quote inside double-quoted string", line_no)
            else:
                result.append(ch)
                i += 1
        return "".join(result)

    # Single-quoted scalar
    if val.startswith("'"):
        if len(val) < 2 or not val.endswith("'"):
            raise FrontmatterParseError("Unclosed single-quoted string", line_no)
        inner = val[1:-1]
        # In YAML, '' represents an escaped single quote
        return inner.replace("''", "'")

    # Bare scalars. The forbidden-feature checks apply here and only here: a
    # quoted string is literal text, so `"Notes on [RFC]"` -- which the writer
    # emits -- must parse back rather than trip the flow-style check (C30).
    if val.startswith(("&", "*")):
        raise FrontmatterParseError("Anchors and aliases are not permitted", line_no)
    if val.startswith("!"):
        raise FrontmatterParseError("YAML tags are not permitted", line_no)
    if val.startswith(("|", ">")):
        raise FrontmatterParseError("Multi-line scalars (| or >) are not permitted", line_no)
    if any(c in val for c in ("[", "]", "{", "}")):
        raise FrontmatterParseError("Flow style indicators are not permitted", line_no)

    # Strictly typed for true/false/null (§3.2), otherwise strings. `~` is a
    # string: the spec names exactly three non-string literals.
    if val == "true":
        return True
    if val == "false":
        return False
    if val == "null":
        return None

    return val


def _split_mapping(text: str) -> tuple[str, str] | None:
    """Split `key: value` on YAML's key indicator, or return None if absent.

    The indicator is a colon followed by a space or the end of the line, so
    `https://example.com` and `agent:foo` are scalars, not one-key mappings.
    Splitting on any colon turned a list of URLs into `[{'https': ...}]`.
    """
    key, sep, rest = text.partition(":")
    if not sep or (rest and not rest.startswith(" ")):
        return None
    return key.strip(), rest.strip()


def split_frontmatter(content: str) -> tuple[str | None, str]:
    """Split markdown text into raw frontmatter and body.

    Returns:
        (raw_frontmatter_text, body_text), or (None, content) when the text
        does not open with a frontmatter fence.
    """
    if not content.startswith("---\n") and not content.startswith("---\r\n") and content != "---":
        return None, content

    lines = split_lines(content)
    closing_index = next((idx for idx in range(1, len(lines)) if lines[idx].strip() == "---"), -1)
    if closing_index == -1:
        raise FrontmatterParseError("Unterminated frontmatter block: missing closing '---'", 1)

    raw_fm = "\n".join(lines[1:closing_index])
    body = "\n".join(lines[closing_index + 1 :])
    return raw_fm, body


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse markdown frontmatter and body strictly conforming to §3.2.

    Returns:
        (parsed_dict, body_text)
    Raises:
        FrontmatterParseError on any syntax violation.
    """
    raw_fm, body = split_frontmatter(content)
    if raw_fm is None:
        return {}, content

    data: dict[str, Any] = {}
    lines = split_lines(raw_fm)

    # Parser state tracking
    current_key: str | None = None
    current_container_type: str | None = None  # "scalar_seq", "dict_seq", "flat_map"
    current_seq: list[Any] | None = None
    current_map: dict[str, Any] | None = None
    current_seq_item_map: dict[str, Any] | None = None

    for line_idx, line in enumerate(lines, start=2):  # line 1 was opening '---'
        if "\t" in line:
            raise FrontmatterParseError("Tab characters are not permitted in frontmatter", line_idx)

        content_part, _ = split_comment(line)
        stripped = content_part.strip()

        # Skip empty or comment-only lines
        if not stripped:
            continue

        indent = len(content_part) - len(content_part.lstrip(" "))

        if indent > 4:
            raise FrontmatterParseError("Excessive indentation or unsupported nesting depth (> 4 spaces)", line_idx)

        # Level 0: Top-level mapping keys
        if indent == 0:
            current_key = None
            current_container_type = None
            current_seq = None
            current_map = None
            current_seq_item_map = None

            mapping = _split_mapping(stripped)
            if mapping is None:
                raise FrontmatterParseError("Expected 'key: value' or 'key:' mapping at indent 0", line_idx)

            k, v_val = mapping
            if not RE_KEY.match(k):
                raise FrontmatterParseError(f"Invalid key name '{k}'", line_idx)
            if k in data:
                raise FrontmatterParseError(f"Duplicate top-level key '{k}'", line_idx)

            if not v_val:
                # Key begins a container (sequence or nested mapping)
                current_key = k
            else:
                data[k] = parse_scalar(v_val, line_idx)
            continue

        # Indent 2: Sub-items under a top-level key
        if indent == 2:
            if current_key is None:
                raise FrontmatterParseError("Unexpected indentation without preceding parent key", line_idx)

            # Sequence item: '- ...'
            if stripped.startswith("-"):
                seq_val = stripped[1:].strip()
                if seq_val.startswith("-"):
                    raise FrontmatterParseError("Sequences of sequences are not permitted", line_idx)

                is_quoted_scalar = seq_val.startswith(('"', "'"))
                item_mapping = None if is_quoted_scalar else _split_mapping(seq_val)
                is_mapping_item = item_mapping is not None

                # Initialize list container if not already started
                if current_container_type is None:
                    current_container_type = "dict_seq" if is_mapping_item else "scalar_seq"
                    current_seq = []
                    data[current_key] = current_seq

                if is_mapping_item:
                    # Sequence of flat mappings: '- key: value'
                    if current_container_type == "scalar_seq":
                        raise FrontmatterParseError("Cannot mix scalar and mapping items in sequence", line_idx)
                    current_container_type = "dict_seq"
                    assert item_mapping is not None
                    sub_k, sub_v = item_mapping
                    if not RE_KEY.match(sub_k):
                        raise FrontmatterParseError(f"Invalid mapping key '{sub_k}' in sequence", line_idx)
                    current_seq_item_map = {sub_k: parse_scalar(sub_v, line_idx)}
                    assert current_seq is not None
                    current_seq.append(current_seq_item_map)
                else:
                    # Sequence of scalars: '- scalar'
                    if current_container_type == "dict_seq":
                        raise FrontmatterParseError("Cannot mix scalar and mapping items in sequence", line_idx)
                    current_container_type = "scalar_seq"
                    current_seq_item_map = None
                    assert current_seq is not None
                    current_seq.append(parse_scalar(seq_val, line_idx))
                continue

            # Nested flat mapping under top-level key: 'key: value'
            nested = _split_mapping(stripped)
            if nested is None:
                raise FrontmatterParseError("Expected 'key: value' mapping at indent 2", line_idx)

            if current_container_type is None:
                current_container_type = "flat_map"
                current_map = {}
                data[current_key] = current_map

            if current_container_type != "flat_map":
                raise FrontmatterParseError("Cannot mix sequence items with mapping items under the same key", line_idx)

            sub_k, sub_v_val = nested
            if not RE_KEY.match(sub_k):
                raise FrontmatterParseError(f"Invalid key name '{sub_k}' at indent 2", line_idx)
            assert current_map is not None
            if sub_k in current_map:
                raise FrontmatterParseError(f"Duplicate nested key '{sub_k}'", line_idx)

            if not sub_v_val:
                raise FrontmatterParseError("Mappings nested more than two levels are not permitted", line_idx)
            current_map[sub_k] = parse_scalar(sub_v_val, line_idx)
            continue

        # Indent 4: Sibling keys for a sequence item mapping
        if indent == 4:
            if current_container_type != "dict_seq" or current_seq_item_map is None:
                raise FrontmatterParseError("Indent 4 is only valid for sibling keys of sequence mappings", line_idx)

            sibling = _split_mapping(stripped)
            if sibling is None:
                raise FrontmatterParseError("Expected 'key: value' at indent 4", line_idx)

            sub_k, sub_v_val = sibling
            if not RE_KEY.match(sub_k):
                raise FrontmatterParseError(f"Invalid key name '{sub_k}' at indent 4", line_idx)
            if sub_k in current_seq_item_map:
                raise FrontmatterParseError(f"Duplicate sibling key '{sub_k}' in sequence mapping", line_idx)

            if not sub_v_val:
                raise FrontmatterParseError("Mappings nested more than two levels are not permitted", line_idx)
            current_seq_item_map[sub_k] = parse_scalar(sub_v_val, line_idx)
            continue

        # Any other indentation
        raise FrontmatterParseError(f"Invalid indentation ({indent} spaces)", line_idx)

    return data, body


class FrontmatterSerializeError(CadabbyError, ValueError):
    """Raised when a value cannot be written inside the restricted YAML subset."""

    code = _CODE_UNSERIALIZABLE


def format_scalar(val: Any) -> str:
    """Format a scalar into its canonical restricted YAML representation.

    Raises:
        FrontmatterSerializeError: if val is a collection. The subset has no flow
            style, so there is no legal rendering; falling through to str() would
            emit a Python repr that this module's own parser rejects (§3.2).
    """
    if isinstance(val, (dict, list, tuple, set)):
        raise FrontmatterSerializeError(
            f"Cannot serialize {type(val).__name__} as a scalar. The restricted "
            "YAML subset nests only one level deep (§3.2)"
        )
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "true" if val else "false"
    if not isinstance(val, str):
        # Every scalar in the subset is a string except true/false/null, so
        # str() would change the type on the next read (3 -> "3") or emit a
        # repr the parser rejects. Refuse and let the caller quote it (§3.2).
        raise FrontmatterSerializeError(
            f"Cannot serialize {type(val).__name__} {val!r}: the restricted YAML subset "
            "holds only strings, true, false and null (§3.2). Pass it as a string"
        )

    val_str = val.replace("\r\n", "\n").replace("\r", "\n")

    # Empty string
    if not val_str:
        return '""'

    # RFC 3339 timestamps must be single-quoted per §3.1/§3.2
    if RE_TIMESTAMP.match(val_str):
        return f"'{val_str}'"

    # Hashes and sha256 references
    if val_str.startswith(BODY_HASH_PREFIX):
        return f"'{val_str}'"

    # Keywords that would be ambiguously parsed as booleans or null
    if val_str in ("true", "false", "null", "~", "yes", "no", "on", "off"):
        return f'"{val_str}"'

    # Safe bare scalars (alphanumeric, dots, underscores, dashes, slashes)
    if RE_BARE_SAFE.match(val_str) and not val_str.startswith("-"):
        return val_str

    # Everything else double-quoted with escaped characters
    escaped = (
        val_str.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _scalar_at(val: Any, where: str) -> str:
    """format_scalar, with the offending key named in the error message."""
    try:
        return format_scalar(val)
    except FrontmatterSerializeError as exc:
        raise FrontmatterSerializeError(f"'{where}': {exc}") from None


def _check_key(key: Any, where: str) -> str:
    """Validate that a mapping key conforms to RE_KEY."""
    if not isinstance(key, str) or not RE_KEY.match(key):
        raise FrontmatterSerializeError(f"'{where}': Invalid key name '{key}'")
    return key


def serialize_frontmatter(data: dict[str, Any], body: str = "") -> str:
    """Canonicalizing frontmatter serializer conforming to §3.2.

    Emits keys in schema order, followed by unknown keys alphabetically.
    Guarantees two-space sequence indent and single-quoted timestamps.

    The writer is as strict as the parser: a value outside the subset raises
    rather than being coerced. Emitting a Python repr would destroy the value
    and leave a file the parser rejects, silently dropping the note from search.

    Raises:
        FrontmatterSerializeError: on a value the restricted subset cannot express.
        ValueError: on a tag containing whitespace (§3.1).
    """
    if not data and not body:
        return ""

    for k in data:
        _check_key(k, str(k))

    # Canonicalize tags: lowercase, de-duplicate, reject whitespace (§3.1).
    # Copied rather than mutated — callers hold the original dict.
    if isinstance(data.get("tags"), list):
        data = {**data, "tags": canonicalize_tags(data["tags"])}

    # Sort keys: SCHEMA_KEY_ORDER first, then extra keys alphabetically
    ordered_keys = [k for k in SCHEMA_KEY_ORDER if k in data]
    extra_keys = sorted(k for k in data if k not in SCHEMA_KEY_ORDER)
    all_keys = ordered_keys + extra_keys

    lines: list[str] = ["---"]

    for k in all_keys:
        val = data[k]

        # Sequence
        if isinstance(val, list):
            if not val:
                continue
            has_dict = any(isinstance(item, dict) for item in val)
            has_non_dict = any(not isinstance(item, dict) for item in val)
            if has_dict and has_non_dict:
                raise FrontmatterSerializeError(f"'{k}': Cannot mix scalar and mapping items in sequence")
            lines.append(f"{k}:")
            for item in val:
                if isinstance(item, dict):
                    # Flat mapping sequence item
                    # First key on '- ' line, remaining keys on indent 4
                    sub_keys = list(item.keys())
                    if not sub_keys:
                        raise FrontmatterSerializeError(f"'{k}[]': Cannot serialize an empty mapping item")
                    first_k = _check_key(sub_keys[0], f"{k}[].{sub_keys[0]}")
                    lines.append(f"  - {first_k}: {_scalar_at(item[first_k], f'{k}[].{first_k}')}")
                    for other_k in sub_keys[1:]:
                        _check_key(other_k, f"{k}[].{other_k}")
                        lines.append(f"    {other_k}: {_scalar_at(item[other_k], f'{k}[].{other_k}')}")
                else:
                    lines.append(f"  - {_scalar_at(item, f'{k}[]')}")
            continue

        # Nested mapping (e.g. 'generated:')
        if isinstance(val, dict):
            if not val:
                continue
            lines.append(f"{k}:")
            for sub_k, sub_v in val.items():
                _check_key(sub_k, f"{k}.{sub_k}")
                lines.append(f"  {sub_k}: {_scalar_at(sub_v, f'{k}.{sub_k}')}")
            continue

        # Top-level scalar
        lines.append(f"{k}: {_scalar_at(val, k)}")

    lines.append("---")
    fm_text = "\n".join(lines) + "\n"

    if body:
        if not body.startswith("\n"):
            return fm_text + body
        return fm_text + body.lstrip("\n")
    return fm_text

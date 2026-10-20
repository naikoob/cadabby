"""Model Context Protocol (MCP 2024-11-05) JSON-RPC 2.0 stdio server.

Conforms strictly to Cadabby Technical Specification §5.1, §5.2.
"""

from __future__ import annotations

import copy
import json
import re
import sys
from collections.abc import Iterator
from types import TracebackType
from typing import Any, Self, TypedDict

from cadabby import __version__
from cadabby.cache import VaultCache
from cadabby.constants import DEFAULT_LIMIT, DIR_WIKI, NOTE_STATUSES
from cadabby.errors import (
    HUMAN_ATTESTATION_REFUSED,
    INVALID_ARGUMENT,
    UNKNOWN_TOOL,
    InvalidArgumentError,
    classify,
)
from cadabby.okf import actor_name
from cadabby.ops import (
    graph_view,
    ground_notes,
    move_note,
    scaffold_note_result,
    search_notes,
    update_note_result,
    vault_status,
    verify_note,
)
from cadabby.vault import Vault


def _tool_ok(payload: Any) -> dict[str, Any]:
    """Render a successful tool result.

    The counterpart to _tool_error. Every tool answers with a JSON document
    (§5.1): messages are for people and are never parsed (§5.4), so a result
    an agent must act on -- the new `source_hash`, `already_verified` -- is a
    field, not a sentence. A str payload is still returned verbatim.
    """
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _tool_error(tool: str, code: str, message: str, *, retryable: bool) -> dict[str, Any]:
    """Render a tool failure as structured JSON rather than prose (§5.4).

    The payload goes in the text content because MCP has no typed error
    channel for tool results -- `isError` is a single boolean. Emitting JSON
    there gives the caller something to branch on without parsing English,
    which is what the previous `f"Error executing {name}: {e}"` forced.
    """
    payload = {"error": {"code": code, "message": message, "retryable": retryable, "tool": tool}}
    return {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
        "isError": True,
    }


# Upper bounds on what one call may ask for (§5.1). Unbounded, `limit: 1e30`
# reached SQLite as an overflow (INTERNAL) and a target list had no ceiling.
MAX_LIMIT = 1000
MAX_TARGETS = 50
MAX_BUDGET_TOKENS = 1_000_000


class ToolSpec(TypedDict):
    """One MCP tool declaration, exactly as `tools/list` serves it (§5.1)."""

    name: str
    description: str
    inputSchema: dict[str, Any]  # JSON Schema: open-ended by nature


_TOOL_SPECS: list[ToolSpec] = [
    {
        "name": "vault_search",
        "description": (
            "Keyword (FTS5 BM25) or filter query over vault notes, ranked by trust tier, status, and domain. "
            "Matching is lexical, not semantic: use words the note would contain and try synonyms before "
            "concluding a topic is absent. Omit 'query' with at least one filter to list matching notes by "
            "epistemic rank. Unfiltered searches exclude raw/; pass domain='raw' to search unprocessed sources "
            "too. Every call rescans the vault first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "default": "",
                    "description": "Search terms; omit or pass '' when filtering",
                },
                "domain": {
                    "type": "string",
                    "description": "Cognitive domain ('wiki', 'customers', …) or 'raw' for primary sources",
                },
                "type": {"type": "string", "description": "Filter by note type"},
                "status": {"type": "string", "description": "Filter by note status"},
                "trust": {"type": "string", "description": "Filter by trust tier (human-reviewed, machine-confirmed, …)"},
                "tag": {"type": "string", "description": "Filter by note tag"},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT, "description": "Max results"},
            },
        },
    },
    {
        "name": "vault_ground",
        "description": (
            "Retrieve note markdown (full note or a '#heading' section), domain directives, and 1-hop "
            "links/backlinks/sources with neighbor metadata (title, type, status, trust_tier, description). "
            "A raw/<path> target returns that source's text (or filename_only: true for binaries like PDF) "
            "and its cited_by notes, with no trust tier: read sources this way, not with file tools. "
            "Returns one entry per target (kind 'note' or 'raw', or {target, error: {code, message, retryable}}); "
            "fails only if none resolve. A missing '#heading' returns the full text with 'section_error'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "targets": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 1,
                    "maxItems": MAX_TARGETS,
                    "description": (
                        "Notes (CIDs, stems, relative paths, or wikilinks) and raw sources ('raw/paper.md'); "
                        "append '#heading' to ground only that section"
                    ),
                },
                "budget_tokens": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_BUDGET_TOKENS,
                    "description": (
                        "Optional cap on returned text to protect context; never derive it from a requested "
                        "output length. Truncates at section boundaries."
                    ),
                },
            },
            "required": ["targets"],
        },
    },
    {
        "name": "vault_status",
        "description": (
            "Vault health check: note counts by tier, type, and domain, verification debt, raw file counts, "
            "broken links, finding_counts from the six lint gates, and 'queues' (first 25 with totals: "
            "unprocessed_raw, unfiled wiki notes, and verification_debt split into agent_payable and "
            "human_owed — never agent-verify human_owed; give the user `cadabby verify <cid> --human`). "
            "Pass lint=true for the full findings list. Findings never set isError; check finding_counts.error."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "lint": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include every lint finding (code, severity, rel_path, line, message)",
                },
            },
        },
    },
    {
        "name": "vault_graph",
        "description": (
            "Link structure without note text. view='neighborhood' (default) returns a note's 1- and 2-hop "
            "links and backlinks, co-citations, and bibliographic coupling with neighbor metadata (title, "
            "type, status, trust_tier) for cross-linking and MOC filing. view='mocs' returns, for every MOC "
            "(type: moc in any domain but archive/), "
            "outgoing/incoming link counts and largest link overlaps (ratio = shared / smaller MOC) for splits "
            "and merges."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "view": {
                    "type": "string",
                    "enum": ["neighborhood", "mocs"],
                    "default": "neighborhood",
                    "description": "'neighborhood' around one note, or 'mocs' across the vault",
                },
                "target": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Note (CID, path, stem, or wikilink); required for 'neighborhood', refused for 'mocs'",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT, "description": "Max entries per list"},
            },
        },
    },
    {
        "name": "vault_scaffold_note",
        "description": "Create a note with valid OKF frontmatter and generated attribution. Refuses to overwrite.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "minLength": 1, "description": "Note title"},
                "type": {"type": "string", "minLength": 1, "description": "Note type ('concept', 'moc', or domain type)"},
                "description": {"type": "string", "minLength": 1, "description": "One-line summary"},
                "domain": {"type": "string", "default": "wiki", "description": "Cognitive domain (default: 'wiki')"},
                "path": {
                    "type": "string",
                    "description": "Relative path within domain or vault (wiki/ is flat; raw/ and dot folders refused)",
                },
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Kebab-case tags"},
                "aliases": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "description": "Search synonyms and acronyms indexed into FTS5",
                },
                "sources": {"type": "array", "items": {"type": "string"}, "description": "Cited raw/ paths"},
                "status": {
                    "type": "string",
                    "enum": list(NOTE_STATUSES),
                    "default": "draft",
                    "description": "Initial lifecycle status (default: 'draft')",
                },
                "frontmatter": {
                    "type": "object",
                    "description": (
                        "Optional custom frontmatter fields (max 1 nesting level; numeric scalars are stored "
                        "as strings; fields with their own argument, like 'tags' or 'status', and engine-owned "
                        "'generated' and 'verified' are refused)"
                    ),
                },
                "body": {"type": "string", "description": "Initial markdown body"},
                "template": {
                    "type": "string",
                    "description": (
                        "Obsidian template name to seed body ('{{title}}' substituted; template frontmatter "
                        "discarded; mutually exclusive with 'body')"
                    ),
                },
            },
            "required": ["title", "type", "description"],
        },
    },
    {
        "name": "vault_update_note",
        "description": (
            "Atomic frontmatter patches and body edits. In 'edits', replace_section and append_section "
            "address '##' headings; replace_text matches exact unique text anywhere in the body (never "
            "frontmatter). Batch related edits into one call. Any body edit turns a verified note "
            "stale-verified, so check trust_tier first. Refuses unparseable files."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cid": {"type": "string", "description": "Note CID, stem, or relative path"},
                "patch_frontmatter": {
                    "type": "object",
                    "description": (
                        "Frontmatter fields to patch (max 1 nesting level; numeric scalars are stored "
                        "as strings; engine-owned 'generated' and 'verified' are refused)"
                    ),
                },
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {"type": "string", "enum": ["replace_text", "replace_section", "append_section"]},
                            "old": {"type": "string", "description": "replace_text: exact unique match"},
                            "new": {"type": "string", "description": "replace_text: replacement text"},
                            "heading": {"type": "string", "description": "section ops: '##' heading text"},
                            "body": {"type": "string", "description": "section ops: section body"},
                        },
                        "required": ["op"],
                    },
                    "description": "Ordered body edits applied atomically; if any fails, nothing is written",
                },
                "expected_hash": {
                    "type": "string",
                    "description": (
                        "The source_hash a previous scaffold or update returned; the write is refused "
                        "(VAULT_CONFLICT) if the file changed since"
                    ),
                },
            },
            "required": ["cid"],
        },
    },
    {
        "name": "vault_verify_note",
        "description": (
            "Appends a verification entry stamped by: agent:<client_id>, at: now, and of: the current body "
            "hash. Writing human:* over MCP is refused; a human signs with `cadabby verify <cid> --human`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cid": {"type": "string", "description": "Note CID, stem, or relative path"},
                "method": {
                    "type": "string",
                    "default": "automated-check",
                    "description": "Verification method label (e.g. 'source-crosscheck')",
                },
            },
            "required": ["cid"],
        },
    },
    {
        "name": "vault_move_note",
        "description": (
            "Move or rename a note or folder subtree and repoint every link to it. Dry-run unless apply=true: "
            "returns each referrer whose links change, its trust tier before/after, and a plan_token. "
            "Show the user that plan and apply only once they agree, passing plan_token with apply=true. "
            "Refused if the move retargets another link or stales a human-reviewed note not in approve_stale. "
            "Never move note files with shell commands: nothing else repoints links or logs the move."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cid": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Note or folder prefix to move (CID, path, stem, wikilink, or 'customers/northwind/')",
                },
                "to": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Destination path (bare name renames in place; trailing '/' moves into folder)",
                },
                "apply": {"type": "boolean", "default": False, "description": "Write the move; omit for dry-run"},
                "approve_stale": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "description": "Human-reviewed notes the user approved going stale-verified on link rewrite",
                },
                "plan_token": {
                    "type": "string",
                    "description": "Dry-run plan_token; with apply=true, refuses (VAULT_CONFLICT) if plan changed",
                },
            },
            "required": ["cid", "to"],
        },
    },
]



def _closed(spec: ToolSpec) -> ToolSpec:
    """A tool declaration whose schema refuses unknown keys (§5.1).

    `qurey` or `patch` silently dropped turns a typo into a call that
    "succeeds" doing something else. Built as a copy rather than by mutating
    the literal at import time.
    """
    closed = copy.deepcopy(spec)
    closed["inputSchema"]["additionalProperties"] = False
    return closed


# The one tool registry: what tools/list serves, and what every argument is
# validated against. A tuple, so no caller can append a ninth tool to it.
TOOLS: tuple[ToolSpec, ...] = tuple(_closed(spec) for spec in _TOOL_SPECS)

_REQUIRED_ARGS: dict[str, tuple[str, ...]] = {t["name"]: tuple(t["inputSchema"].get("required", ())) for t in TOOLS}
_PROPERTIES: dict[str, dict[str, Any]] = {t["name"]: t["inputSchema"].get("properties", {}) for t in TOOLS}
# Tools removed from the surface, mapped to what replaces them. Only ever read
# to word an UNKNOWN_TOOL error: an agent still following a 0.3.x vault's
# AGENTS.md can correct itself, and the surface stays at eight tools (§5.1).
_RETIRED_TOOLS: dict[str, str] = {
    "vault_lint": "vault_status with lint: true",
}
# Names agents guess that were never tools, mapped to the tool they mean. Kept
# apart from _RETIRED_TOOLS so the error never claims a history that did not
# happen: these get "did you mean", not "retired in 0.4.0".
_LIKELY_GUESSES: dict[str, str] = {
    "vault_get_graph": "vault_graph",
    "vault_read_note": "vault_ground",
    "vault_list_notes": "vault_search",
}
# Arguments removed from a tool, worded the same way: never aliases, only the
# INVALID_ARGUMENT message, checked before the required-argument check so a
# caller passing the old name learns the new one rather than "missing targets".
_RETIRED_ARGUMENTS: dict[str, dict[str, str]] = {
    "vault_ground": {"cids": "renamed to 'targets' in 0.5.0"},
    "vault_update_note": {
        "append_section": 'retired in 0.5.0; pass edits: [{"op": "append_section", "heading": ..., "body": ...}]',
        "replace_section": 'retired in 0.5.0; pass edits: [{"op": "replace_section", "heading": ..., "body": ...}]',
    },
}
_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _required_args(tool: str) -> tuple[str, ...]:
    """Required fields a tool declares. Empty for tools that declare none."""
    return _REQUIRED_ARGS.get(tool, ())


def _schema_violation(name: str, value: Any, schema: dict[str, Any]) -> str | None:
    """Why `value` does not satisfy the subset of JSON Schema the tool declarations use, or None.

    Checked once at the boundary for every tool, from the same inputSchema
    clients read, so a mistyped argument is INVALID_ARGUMENT before any engine
    code runs -- never an INTERNAL from deep inside, never written to disk.
    """
    expected = schema.get("type")
    if expected is not None:
        # bool is an int subclass in Python, and never a JSON integer.
        if not isinstance(value, _JSON_TYPES[expected]) or (expected in ("integer", "number") and isinstance(value, bool)):
            return f"'{name}' must be {'an' if expected[0] in 'aeiou' else 'a'} {expected}"
    if "enum" in schema and value not in schema["enum"]:
        return f"'{name}' must be one of {schema['enum']}"
    if "minimum" in schema and value < schema["minimum"]:
        return f"'{name}' must be at least {schema['minimum']}"
    if "maximum" in schema and value > schema["maximum"]:
        return f"'{name}' must be at most {schema['maximum']}"
    if "minLength" in schema and len(value) < schema["minLength"]:
        return f"'{name}' must not be empty"
    if "minItems" in schema and len(value) < schema["minItems"]:
        return f"'{name}' must have at least {schema['minItems']} item(s)"
    if "maxItems" in schema and len(value) > schema["maxItems"]:
        return f"'{name}' must have at most {schema['maxItems']} item(s)"
    if expected == "array" and "items" in schema:
        for i, item in enumerate(value):
            if why := _schema_violation(f"{name}[{i}]", item, schema["items"]):
                return why
    if expected == "object" and "properties" in schema:
        for key in schema.get("required", ()):
            if key not in value:
                return f"'{name}.{key}' is required"
        for key, sub in schema["properties"].items():
            if value.get(key) is not None and (why := _schema_violation(f"{name}.{key}", value[key], sub)):
                return why
    return None


def _bool_arg(args: dict[str, Any], key: str, *, default: bool = False) -> bool:
    """An optional boolean argument; its type was already checked against the schema."""
    val = args.get(key)
    return default if val is None else bool(val)


class McpServer:
    """JSON-RPC 2.0 stdio MCP Server implementation."""

    def __init__(self, vault: Vault) -> None:
        self.vault = vault
        self.cache = VaultCache(self.vault)
        self.client_id = "agent:unknown"

    def close(self) -> None:
        """Close persistent cache connection."""
        if hasattr(self, "cache") and self.cache is not None:
            self.cache.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    def __del__(self) -> None:
        # Finalizers must not raise; stray stderr tracebacks confuse MCP harnesses.
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass

    def handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """Negotiate capabilities and identify client."""
        client_info = params.get("clientInfo") if isinstance(params, dict) else None
        client_name = client_info.get("name") if isinstance(client_info, dict) else None
        # The actor grammar is ASCII (§3.4). `\w` admitted letters like 'é',
        # minting an actor that verify then refused on every call.
        safe_name = actor_name(str(client_name or "unknown")) or "client"
        self.client_id = f"agent:{safe_name}"

        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {},
                "resources": {},
            },
            "serverInfo": {
                "name": "cadabby",
                "version": __version__,
            },
        }

    def handle_tools_list(self) -> dict[str, Any]:
        # A copy: the registry is shared by every request and must not be edited through a reply.
        return {"tools": copy.deepcopy(list(TOOLS))}

    def _tool_search(self, args: dict[str, Any]) -> dict[str, Any]:
        res = search_notes(
            self.vault,
            self.cache,
            query=args.get("query") or "",
            type_=args.get("type"),
            status=args.get("status"),
            trust=args.get("trust"),
            tag=args.get("tag"),
            domain=args.get("domain"),
            limit=args.get("limit"),
        )
        return _tool_ok([r.to_dict() for r in res])

    def _tool_ground(self, args: dict[str, Any]) -> dict[str, Any]:
        grounded = ground_notes(
            self.vault,
            args["targets"],
            budget_tokens=args.get("budget_tokens"),
            cache=self.cache,
        )
        return _tool_ok(grounded)

    def _tool_scaffold_note(self, args: dict[str, Any]) -> dict[str, Any]:
        result = scaffold_note_result(
            vault=self.vault,
            title=args["title"],
            type_=args["type"],
            description=args["description"],
            tags=args.get("tags"),
            sources=args.get("sources"),
            body=args.get("body") or "",
            actor=self.client_id,
            domain=args.get("domain") or DIR_WIKI,
            path=args.get("path"),
            template=args.get("template"),
            status=args.get("status") or "draft",
            aliases=args.get("aliases"),
            frontmatter=args.get("frontmatter"),
            cache=self.cache,
        )
        return _tool_ok(result.to_dict())

    def _tool_update_note(self, args: dict[str, Any]) -> dict[str, Any]:
        result = update_note_result(
            vault=self.vault,
            cid_or_path=args["cid"],
            frontmatter_patch=args.get("patch_frontmatter"),
            expected_hash=args.get("expected_hash"),
            actor=self.client_id,
            edits=args.get("edits"),
            cache=self.cache,
        )
        return _tool_ok(result.to_dict())

    def _tool_verify_note(self, args: dict[str, Any]) -> dict[str, Any]:
        res = verify_note(
            vault=self.vault,
            cid_or_path=args["cid"],
            actor=self.client_id,
            method=args.get("method") or "automated-check",
            is_human_authorized=False,
            cache=self.cache,
        )
        return _tool_ok(res)

    def _tool_status(self, args: dict[str, Any]) -> dict[str, Any]:
        return _tool_ok(vault_status(self.vault, self.cache, lint=_bool_arg(args, "lint", default=False)))

    def _tool_graph(self, args: dict[str, Any]) -> dict[str, Any]:
        return _tool_ok(
            graph_view(
                self.vault,
                self.cache,
                view=args.get("view") or "neighborhood",
                target=args.get("target"),
                limit=args.get("limit"),
            )
        )

    def _tool_move_note(self, args: dict[str, Any]) -> dict[str, Any]:
        return _tool_ok(
            move_note(
                self.vault,
                args["cid"],
                args["to"],
                apply=_bool_arg(args, "apply", default=False),
                approve_stale=args.get("approve_stale") or [],
                plan_token=args.get("plan_token"),
                actor=self.client_id,
                cache=self.cache,
            )
        )

    _TOOL_HANDLERS = {
        "vault_search": _tool_search,
        "vault_ground": _tool_ground,
        "vault_scaffold_note": _tool_scaffold_note,
        "vault_update_note": _tool_update_note,
        "vault_move_note": _tool_move_note,
        "vault_verify_note": _tool_verify_note,
        "vault_status": _tool_status,
        "vault_graph": _tool_graph,
    }

    def handle_tools_call(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Route tool invocation to underlying engine functions."""
        if args is not None and not isinstance(args, dict):
            return _tool_error(name, INVALID_ARGUMENT, "'arguments' must be a JSON object", retryable=False)
        args = args or {}

        # Checked here rather than left to the `args["targets"]` lookups below,
        # which raise KeyError and classify as INTERNAL -- telling a caller
        # who omitted a required field that the server has a bug. The
        # requirement is already declared in each tool's inputSchema, so
        # reading it back is the only way the two cannot drift. A JSON null
        # means "not given" (§5.1), for required arguments too.
        retired = _RETIRED_ARGUMENTS.get(name, {})
        # A JSON null means "not given" (§5.1), for retired names too.
        if stale := sorted(k for k in args if k in retired and args[k] is not None):
            return _tool_error(
                name,
                INVALID_ARGUMENT,
                "; ".join(f"'{k}' was {retired[k]}" for k in stale),
                retryable=False,
            )

        missing = [k for k in _required_args(name) if args.get(k) is None]
        if missing:
            return _tool_error(
                name,
                INVALID_ARGUMENT,
                f"Missing required argument(s): {', '.join(missing)}",
                retryable=False,
            )

        handler = self._TOOL_HANDLERS.get(name)
        if handler is None:
            message = f"Unknown tool: {name}"
            if replacement := _RETIRED_TOOLS.get(name):
                message += f" -- retired in 0.4.0; call {replacement} instead"
            elif guess := _LIKELY_GUESSES.get(name):
                message += f"; did you mean {guess}?"
            return _tool_error(name, UNKNOWN_TOOL, message, retryable=False)

        # Defense in depth: the server stamps its own client id regardless, but
        # an explicit human:* request is refused as such (§3.5), ahead of the
        # unknown-argument check that would otherwise report it as a typo.
        actor_param = args.get("actor") or args.get("by")
        if actor_param and str(actor_param).startswith("human:"):
            return _tool_error(
                name,
                HUMAN_ATTESTATION_REFUSED,
                "Verification by human:* cannot be performed over MCP.",
                retryable=False,
            )
        # A JSON null means "not given" (§5.1), so an unknown key sent as null is ignored.
        unknown = sorted(k for k in args if k not in _PROPERTIES[name] and args[k] is not None)
        if unknown:
            return _tool_error(
                name,
                INVALID_ARGUMENT,
                f"Unknown argument(s): {', '.join(unknown)}. Accepted: {', '.join(sorted(_PROPERTIES[name]))}",
                retryable=False,
            )

        # Types come from the same declaration. A JSON null means "not given".
        for key, value in args.items():
            schema = _PROPERTIES[name].get(key)
            if schema is not None and value is not None and (why := _schema_violation(key, value, schema)):
                return _tool_error(name, INVALID_ARGUMENT, why, retryable=False)

        try:
            return handler(self, args)
        except Exception as e:  # noqa: BLE001
            info = classify(e)
            return _tool_error(name, info.code, info.message, retryable=info.retryable)

    def handle_resources_list(self) -> dict[str, Any]:
        """List exposed MCP resources representing cognitive domains and directives."""
        resources: list[dict[str, Any]] = [
            {
                "uri": "vault://domains",
                "name": "Discovered Cognitive Domains",
                "description": "JSON inventory of all cognitive domains and governance rules",
                "mimeType": "application/json",
            }
        ]
        domains = self.vault.discover_domains()
        for name, d in domains.items():
            if d.directives_markdown:
                resources.append(
                    {
                        "uri": f"domain://{name}/directives",
                        "name": f"{name.capitalize()} Domain Directives",
                        "description": f"Domain directives and agent constraints for '{name}'",
                        "mimeType": "text/markdown",
                    }
                )
        return {"resources": resources}

    def handle_resources_read(self, uri: str) -> dict[str, Any]:
        """Read content of an exposed MCP resource."""
        domains = self.vault.discover_domains()
        if uri == "vault://domains":
            data = {
                name: {
                    "name": d.name,
                    "description": d.description,
                    "allowed_types": d.allowed_types,
                    "require_sources": d.require_sources,
                    "has_directives": bool(d.directives_markdown),
                }
                for name, d in domains.items()
            }
            return {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "application/json",
                        "text": json.dumps(data, indent=2),
                    }
                ]
            }

        if uri.startswith("domain://") and uri.endswith("/directives"):
            domain_name = uri[len("domain://") : -len("/directives")].strip("/")
            domain_def = domains.get(domain_name)
            if domain_def is None:
                raise InvalidArgumentError(f"Unknown domain '{domain_name}' (see vault://domains)")
            if domain_def is not None and domain_def.directives_markdown:
                return {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "text/markdown",
                            "text": domain_def.directives_markdown,
                        }
                    ]
                }
            return {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "text/markdown",
                        "text": f"# {domain_name.capitalize()} Directives\n\nNo custom directives configured.",
                    }
                ]
            }

        raise InvalidArgumentError(f"Unknown resource URI: {uri}")

    def dispatch_request(self, req: dict[str, Any]) -> dict[str, Any] | None:
        """Route a parsed JSON-RPC 2.0 request object and return its response (or None for notifications)."""
        # A notification is a request with no `id` member. `"id": null` is a
        # request the client will wait on, so it is answered, not dropped.
        if "id" not in req:
            return None
        req_id = req["id"]
        if isinstance(req_id, bool) or not isinstance(req_id, (str, int)):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request: 'id' must be a string or an integer"}}

        method = req.get("method")
        params = req.get("params")
        if not isinstance(params, dict):
            params = {}

        resp: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}

        if not isinstance(method, str):
            resp["error"] = {"code": -32600, "message": "Invalid Request: 'method' must be a string"}
            return resp

        if method == "initialize":
            try:
                resp["result"] = self.handle_initialize(params)
            except Exception as e:  # noqa: BLE001
                resp["error"] = {"code": -32603, "message": f"Internal error during initialize: {e!s}"}
        elif method == "tools/list":
            try:
                resp["result"] = self.handle_tools_list()
            except Exception as e:  # noqa: BLE001
                resp["error"] = {"code": -32603, "message": f"Internal error during tools/list: {e!s}"}
        elif method == "tools/call" and not isinstance(params.get("name"), str):
            resp["error"] = {"code": -32602, "message": "Invalid params: 'name' must be a string"}
        elif method == "tools/call":
            try:
                resp["result"] = self.handle_tools_call(params["name"], params.get("arguments"))
            except Exception as e:  # noqa: BLE001
                resp["error"] = {"code": -32603, "message": f"Internal error during tools/call: {e!s}"}
        elif method == "resources/list":
            try:
                resp["result"] = self.handle_resources_list()
            except Exception as e:  # noqa: BLE001
                resp["error"] = {"code": -32603, "message": f"Internal error during resources/list: {e!s}"}
        elif method == "resources/read" and not isinstance(params.get("uri"), str):
            resp["error"] = {"code": -32602, "message": "Invalid params: 'uri' must be a string"}
        elif method == "resources/read":
            try:
                resp["result"] = self.handle_resources_read(params.get("uri", ""))
            except Exception as e:  # noqa: BLE001
                resp["error"] = {"code": -32602, "message": f"Resource error: {e!s}"}
        elif method == "ping":
            resp["result"] = {}
        else:
            resp["error"] = {"code": -32601, "message": f"Method not found: {method}"}

        return resp


# A header line (`Name: value`), as opposed to a JSON message, which opens with `{` or `[`.
_RE_HEADER = re.compile(r"[A-Za-z][A-Za-z0-9-]*:")
_FRAMING_NDJSON = "ndjson"
_FRAMING_CONTENT_LENGTH = "content-length"

# One JSON-RPC message, in either framing (§5.2). Far above any real tool call;
# its job is to keep a hostile or corrupt length from allocating without bound.
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
_DISCARD_CHUNK = 64 * 1024


def _discard(n: int) -> bool:
    """Read and drop `n` bytes of stdin in bounded chunks; False if stdin ends first."""
    while n > 0:
        chunk = sys.stdin.buffer.read(min(n, _DISCARD_CHUNK))
        if not chunk:
            return False
        n -= len(chunk)
    return True


def _drain_line() -> bool:
    """Drop the rest of an over-long line through its newline; False if stdin ends first."""
    while True:
        chunk = sys.stdin.buffer.readline(_DISCARD_CHUNK)
        if not chunk:
            return False
        if chunk.endswith(b"\n"):
            return True


def _write_framed(resp: dict[str, Any], framing: str) -> None:
    """Emit a JSON-RPC response in the framing the request arrived in."""
    out_bytes = json.dumps(resp).encode("utf-8")
    if framing == _FRAMING_CONTENT_LENGTH:
        header = f"Content-Length: {len(out_bytes)}\r\n\r\n".encode("ascii")
        sys.stdout.buffer.write(header + out_bytes)
    else:
        sys.stdout.buffer.write(out_bytes + b"\n")
    sys.stdout.buffer.flush()


def _write_framed_error(code: int, message: str, framing: str) -> None:
    """Emit an id-less error so a client awaiting a reply fails fast."""
    _write_framed({"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}}, framing)


def _is_notification_only_batch(payload: Any) -> bool:
    """Return True when payload is a non-empty JSON-RPC batch containing only notifications."""
    return (
        isinstance(payload, list)
        and bool(payload)
        and all(isinstance(item, dict) and "id" not in item for item in payload)
    )


def _iter_framed_messages() -> Iterator[tuple[dict[str, Any], str]]:
    """Yield `(req_dict, framing)` pairs from `sys.stdin.buffer`, emitting framing errors inline."""
    too_large = f"Invalid Request: message exceeds {MAX_MESSAGE_BYTES} bytes"
    while True:
        raw_line = sys.stdin.buffer.readline(MAX_MESSAGE_BYTES + 1)
        if not raw_line:
            break
        if len(raw_line) > MAX_MESSAGE_BYTES and not raw_line.endswith(b"\n"):
            # An NDJSON line past the cap: drain it to its newline, then refuse it.
            if not _drain_line():
                break
            _write_framed_error(-32600, too_large, _FRAMING_NDJSON)
            continue

        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue

        if _RE_HEADER.match(line):
            # An LSP-style header block. Content-Length is required but need
            # not come first: `Content-Type` is just as legal a first header.
            framing = _FRAMING_CONTENT_LENGTH
            headers = [line]
            while True:
                hdr_bytes = sys.stdin.buffer.readline(_DISCARD_CHUNK)
                if hdr_bytes in (b"\r\n", b"\n", b""):
                    break
                headers.append(hdr_bytes.decode("utf-8", errors="replace").strip())
            if not hdr_bytes:
                break
            length = -1
            for header in headers:
                key, _, value = header.partition(":")
                if key.strip().lower() == "content-length":
                    try:
                        length = int(value.strip())
                    except ValueError:
                        length = -1
            if length < 0:
                _write_framed_error(-32700, "Parse error: missing or malformed Content-Length header", framing)
                break
            if length > MAX_MESSAGE_BYTES:
                # Reading it whole would allocate whatever the header claims.
                # Answer first (the body may never fully arrive), then read it
                # away in bounded chunks and keep the session alive.
                _write_framed_error(-32600, too_large, framing)
                if not _discard(length):
                    break
                continue
            payload_bytes = sys.stdin.buffer.read(length)
            if len(payload_bytes) < length:
                break
            try:
                req = json.loads(payload_bytes.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                _write_framed_error(-32700, "Parse error: request body is not valid JSON", framing)
                continue
        else:
            framing = _FRAMING_NDJSON
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                _write_framed_error(-32700, "Parse error: request body is not valid JSON", framing)
                continue

        if not isinstance(req, dict):
            if _is_notification_only_batch(req):
                continue
            _write_framed_error(
                -32600,
                "Invalid Request: batch and non-object payloads are not supported",
                framing,
            )
            continue

        yield req, framing


def run_mcp_server(vault: Vault) -> int:
    """Run stdio JSON-RPC MCP server loop.

    Newline-delimited JSON is the normative MCP stdio framing (§5.2).
    `Content-Length` framing is accepted for LSP-style clients, and every
    reply mirrors the framing of the message it answers, so neither kind of
    client ever receives bytes it cannot parse.
    """
    with McpServer(vault) as server:
        for req, framing in _iter_framed_messages():
            resp = server.dispatch_request(req)
            if resp is not None:
                _write_framed(resp, framing)
    return 0


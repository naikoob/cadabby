"""Model Context Protocol (MCP 2024-11-05) JSON-RPC 2.0 stdio server.

Conforms strictly to Cadabby Technical Specification §5.1, §5.2.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator
from types import TracebackType
from typing import Any, Self

from cadabby import __version__
from cadabby.cache import VaultCache
from cadabby.constants import DIR_WIKI
from cadabby.errors import INVALID_ARGUMENT, UNKNOWN_TOOL, HumanAttestationRefusedError, classify
from cadabby.lint import status_report
from cadabby.ops import ground_notes, move_note, scaffold_note, update_note, verify_note
from cadabby.vault import Vault, path_to_cid


def _tool_ok(payload: Any) -> dict[str, Any]:
    """Render a successful tool result.

    The counterpart to _tool_error. A str payload is returned verbatim --
    several tools answer with a human-readable confirmation line rather than
    a document -- and anything else is serialized as JSON.
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


TOOLS = [
    {
        "name": "vault_search",
        "description": (
            "Keyword search (SQLite FTS5 BM25) or metadata filter query over vault notes, ranked up by trust tier, "
            "note status, and domain. Matching is lexical, not semantic: query with words the note would contain, "
            "and try synonyms before concluding a topic is absent. Omit 'query' (or pass '') with at least one "
            "filter (domain, type, status, trust, tag) to list matching notes by epistemic rank without a keyword. "
            "Results exclude raw/ unless domain='raw'; search raw/ too before telling the user the vault has nothing. "
            "Every call rescans the vault first, so files edited outside this session are already visible."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "default": "",
                    "description": "Search query terms; omit or pass '' when filtering by domain, type, status, trust, or tag",
                },
                "domain": {
                    "type": "string",
                    "description": (
                        "Filter by cognitive domain (e.g. 'wiki', 'customers', 'projects'). "
                        "Use 'raw' to search the full text of unprocessed sources in raw/, "
                        "which are excluded from unfiltered results."
                    ),
                },
                "type": {"type": "string", "description": "Filter by note type"},
                "status": {"type": "string", "description": "Filter by note status"},
                "trust": {"type": "string", "description": "Filter by trust tier (human-reviewed, machine-confirmed, etc.)"},
                "tag": {"type": "string", "description": "Filter by note tag"},
                "limit": {"type": "integer", "minimum": 1, "default": 20, "description": "Max results to return"},
            },
        },
    },
    {
        "name": "vault_ground",
        "description": (
            "Retrieve note markdown content (full note or a specific '#heading' section), domain directives, "
            "and 1-hop link/backlink/source graph with neighbor metadata (title, type, status, trust_tier, "
            "description) for specified CIDs, note stems, or wikilinks."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "List of note CIDs, bare stems (e.g. 'raft-consensus'), relative paths, or wikilinks "
                        "to retrieve; append '#heading' (or '[[Note#Heading]]') to ground only that section "
                        "and its subsections"
                    ),
                },
                "budget_tokens": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Optional cap on how much note text is returned, to protect your context. It is "
                        "unrelated to how long your answer should be: never derive it from a requested "
                        "output length. Truncates cleanly at section boundaries."
                    ),
                },
            },
            "required": ["cids"],
        },
    },
    {
        "name": "vault_status",
        "description": (
            "Vault health check: note counts by tier and type, verification debt, unprocessed and "
            "filename-only raw files, broken-link count, and finding_counts from the six lint gates. "
            "Pass lint=true for the full findings list to repair. Findings describe the vault, not a "
            "failed call: read finding_counts.error, never the call's error flag. No need to call this "
            "to refresh before other tools; each one rescans the vault itself."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "lint": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include every finding (code, severity, path, message), not just the counts",
                },
            },
        },
    },
    {
        "name": "vault_scaffold_note",
        "description": "Create a note in the vault with valid OKF frontmatter and generated attribution. Refuses to overwrite.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Title of the note"},
                "type": {"type": "string", "description": "Note type (e.g. 'concept', 'entity', or domain-specific type)"},
                "description": {"type": "string", "description": "One-line descriptive summary"},
                "domain": {"type": "string", "default": "wiki", "description": "Cognitive domain (default: 'wiki')"},
                "path": {
                    "type": "string",
                    "description": (
                        "Optional relative path within the domain or vault. wiki/ is flat, so no subfolders "
                        "there; nested folders belong in a cognitive domain. Paths under raw/ or a dot folder "
                        "are refused."
                    ),
                },
                "tags": {"type": "array", "items": {"type": "string"}, "description": "List of tags"},
                "sources": {"type": "array", "items": {"type": "string"}, "description": "Paths to raw sources"},
                "body": {"type": "string", "description": "Initial markdown body text"},
                "template": {
                    "type": "string",
                    "description": (
                        "Name of an Obsidian template whose body seeds the note, so an agent "
                        "and a human writing in Obsidian produce the same shape. '{{title}}' is "
                        "substituted; the template's own frontmatter is discarded because OKF "
                        "frontmatter is engine-owned. Mutually exclusive with 'body'."
                    ),
                },
            },
            "required": ["title", "type", "description"],
        },
    },
    {
        "name": "vault_update_note",
        "description": (
            "Non-destructive frontmatter patches and body edits. Section operations address '##' "
            "headings only; use edits with replace_text for anything else (a single line, the H1, "
            "text above the first '##'). Batch related changes into one call: edits apply atomically "
            "in one write with one log entry. Any body edit makes a verified note stale-verified, so "
            "check trust_tier first. Never truncates a file it could not fully parse."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cid": {"type": "string", "description": "Note CID or relative path"},
                "patch_frontmatter": {
                    "type": "object",
                    "description": (
                        "Dictionary of frontmatter fields to patch. Values may nest at most "
                        "one level (a mapping of scalars, or a list of scalars/flat mappings); "
                        "deeper structures are rejected."
                    ),
                },
                "append_section": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "[heading, section_body] to append to note as a '##' section",
                },
                "replace_section": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "[heading, new_section_body] to replace the '##' section with that heading",
                },
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {"type": "string", "enum": ["replace_text", "replace_section", "append_section"]},
                            "old": {"type": "string", "description": "replace_text: exact text, must match once"},
                            "new": {"type": "string", "description": "replace_text: replacement text"},
                            "heading": {"type": "string", "description": "section ops: '##' heading text"},
                            "body": {"type": "string", "description": "section ops: section body"},
                        },
                        "required": ["op"],
                    },
                    "description": (
                        "Ordered body edits applied atomically: if any fails (e.g. replace_text "
                        "'old' matches zero or several times), nothing is written. replace_text "
                        "reaches the whole body but never the frontmatter."
                    ),
                },
                "expected_hash": {"type": "string", "description": "Expected file hash for concurrency safety"},
            },
            "required": ["cid"],
        },
    },
    {
        "name": "vault_verify_note",
        "description": (
            "Appends a verification entry stamped by: agent:<client_id>, at: now, and of: the current body "
            "hash. Writing human:* over MCP is refused unconditionally; a human signs with "
            "`cadabby verify <cid> --human` in their own terminal."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cid": {"type": "string", "description": "Note CID or relative path"},
                "method": {
                    "type": "string",
                    "default": "automated-check",
                    "description": "Short label for how you checked the claims (e.g. 'source-crosscheck')",
                },
            },
            "required": ["cid"],
        },
    },
    {
        "name": "vault_move_note",
        "description": (
            "Move or rename a note or folder subtree and repoint every link to it. Dry-run unless apply=true: "
            "the result lists each note whose links change, its trust tier before and after, and a plan_token. "
            "Show the user that plan and apply only once they agree, passing the plan_token with apply=true so "
            "nothing is written if the vault changed since. Only links that would break or open a different "
            "note are rewritten. Refused when the move would retarget someone else's link, or would stale a "
            "human-reviewed note not named in approve_stale; report any warnings too. Never move note files "
            "with shell commands: nothing else repoints links or logs the move."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cid": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Note or folder prefix to move: CID, relative path, stem, wikilink, or folder path "
                        "(e.g. 'customers/northwind/' or 'cs244b/')"
                    ),
                },
                "to": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Destination vault-relative path (e.g. 'customers/acme/sizing' or 'archive/cs244b/'). "
                        "A bare name renames a note in place; a trailing '/' moves a note into that folder"
                    ),
                },
                "apply": {"type": "boolean", "default": False, "description": "Write the move; omit for a dry-run"},
                "approve_stale": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "description": "Human-reviewed notes the user agreed may go stale-verified when their links are rewritten",
                },
                "plan_token": {
                    "type": "string",
                    "description": "The dry-run's plan_token; with apply=true, refuses (VAULT_CONFLICT) if the plan changed",
                },
            },
            "required": ["cid", "to"],
        },
    },
]

_REQUIRED_ARGS: dict[str, tuple[str, ...]] = {
    t["name"]: tuple(t["inputSchema"].get("required", ())) for t in TOOLS  # type: ignore[index]
}
_PROPERTIES: dict[str, dict[str, Any]] = {
    t["name"]: t["inputSchema"].get("properties", {}) for t in TOOLS  # type: ignore[index,union-attr]
}
# Tools removed from the surface, mapped to what replaces them. Only ever read
# to word an UNKNOWN_TOOL error: an agent still following a 0.3.x vault's
# AGENTS.md can correct itself, and the surface stays at seven tools (§5.1).
_RETIRED_TOOLS: dict[str, str] = {
    "vault_lint": "vault_status with lint: true",
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
    if "minLength" in schema and len(value) < schema["minLength"]:
        return f"'{name}' must not be empty"
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


def _section_arg(args: dict[str, Any], key: str) -> tuple[str, str] | None:
    """Parse an optional [heading, body] array parameter for vault_update_note."""
    s = args.get(key)
    if s is None:
        return None
    if not (1 <= len(s) <= 2):
        raise ValueError(f"'{key}' must be a [heading, body] array of strings")
    return (s[0], s[1]) if len(s) > 1 else (s[0], "")


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
        client_info = params.get("clientInfo", {})
        client_name = client_info.get("name", "unknown")
        # Sanitize client name for actor string
        safe_name = re.sub(r"[^\w.-]", "-", client_name).strip("-") or "client"
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
        return {"tools": TOOLS}

    def _tool_search(self, args: dict[str, Any]) -> dict[str, Any]:
        self.cache.scan()
        res = self.cache.search(
            query=args.get("query") or "",
            type_=args.get("type"),
            status=args.get("status"),
            trust=args.get("trust"),
            tag=args.get("tag"),
            domain=args.get("domain"),
            limit=args.get("limit", 20),
        )
        return _tool_ok([r.to_dict() for r in res])

    def _tool_ground(self, args: dict[str, Any]) -> dict[str, Any]:
        self.cache.scan()
        grounded = ground_notes(
            self.vault,
            args["cids"],
            budget_tokens=args.get("budget_tokens"),
            cache=self.cache,
        )
        return _tool_ok(grounded)

    def _tool_scaffold_note(self, args: dict[str, Any]) -> dict[str, Any]:
        path = scaffold_note(
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
            cache=self.cache,
        )
        rel = self.vault.rel_path(path)
        return _tool_ok(f"Scaffolded note: {rel} (CID: {path_to_cid(rel)})")

    def _tool_update_note(self, args: dict[str, Any]) -> dict[str, Any]:
        path = update_note(
            vault=self.vault,
            cid_or_path=args["cid"],
            frontmatter_patch=args.get("patch_frontmatter"),
            append_section=_section_arg(args, "append_section"),
            replace_section=_section_arg(args, "replace_section"),
            expected_hash=args.get("expected_hash"),
            actor=self.client_id,
            edits=args.get("edits"),
            cache=self.cache,
        )
        rel = self.vault.rel_path(path)
        return _tool_ok(f"Updated note: {rel}")

    def _tool_verify_note(self, args: dict[str, Any]) -> dict[str, Any]:
        actor_param = args.get("actor") or args.get("by")
        if actor_param and str(actor_param).startswith("human:"):
            # Defense in depth: the server stamps its own client id regardless,
            # but an explicit human:* request is refused rather than ignored.
            raise HumanAttestationRefusedError("Verification by human:* cannot be performed over MCP.")

        res = verify_note(
            vault=self.vault,
            cid_or_path=args["cid"],
            actor=self.client_id,
            method=args.get("method") or "automated-check",
            is_human_authorized=False,
            cache=self.cache,
        )
        return _tool_ok(
            f"Attested {res['cid']} by {res['actor']} "
            f"with content-binding {res['of'][:16]}... "
            f"-> derived trust tier: '{res['trust_tier']}'"
        )

    def _tool_status(self, args: dict[str, Any]) -> dict[str, Any]:
        return _tool_ok(status_report(self.vault, self.cache, lint=_bool_arg(args, "lint", default=False)))

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
    }

    def handle_tools_call(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Route tool invocation to underlying engine functions."""
        if args is not None and not isinstance(args, dict):
            return _tool_error(name, INVALID_ARGUMENT, "'arguments' must be a JSON object", retryable=False)
        args = args or {}

        # Checked here rather than left to the `args["cids"]` lookups below,
        # which raise KeyError and classify as INTERNAL -- telling a caller
        # who omitted a required field that the server has a bug. The
        # requirement is already declared in each tool's inputSchema, so
        # reading it back is the only way the two cannot drift.
        missing = [k for k in _required_args(name) if k not in args]
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
            return _tool_error(name, UNKNOWN_TOOL, message, retryable=False)

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

        raise ValueError(f"Unknown resource URI: {uri}")

    def dispatch_request(self, req: dict[str, Any]) -> dict[str, Any] | None:
        """Route a parsed JSON-RPC 2.0 request object and return its response (or None for notifications)."""
        req_id = req.get("id")
        if req_id is None:
            return None

        method = req.get("method")
        params = req.get("params")
        if not isinstance(params, dict):
            params = {}

        resp: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}

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
        elif method == "tools/call":
            try:
                resp["result"] = self.handle_tools_call(
                    params.get("name", ""), params.get("arguments")
                )
            except Exception as e:  # noqa: BLE001
                resp["error"] = {"code": -32603, "message": f"Internal error during tools/call: {e!s}"}
        elif method == "resources/list":
            try:
                resp["result"] = self.handle_resources_list()
            except Exception as e:  # noqa: BLE001
                resp["error"] = {"code": -32603, "message": f"Internal error during resources/list: {e!s}"}
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


_FRAMING_NDJSON = "ndjson"
_FRAMING_CONTENT_LENGTH = "content-length"


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
        and all(isinstance(item, dict) and item.get("id") is None for item in payload)
    )


def _iter_framed_messages() -> Iterator[tuple[dict[str, Any], str]]:
    """Yield `(req_dict, framing)` pairs from `sys.stdin.buffer`, emitting framing errors inline."""
    while True:
        raw_line = sys.stdin.buffer.readline()
        if not raw_line:
            break

        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue

        if line.lower().startswith("content-length:"):
            framing = _FRAMING_CONTENT_LENGTH
            try:
                length = int(line.split(":", 1)[1].strip())
            except ValueError:
                length = -1
            if length < 0:
                _write_framed_error(-32700, "Parse error: malformed Content-Length header", framing)
                break
            while True:
                hdr_bytes = sys.stdin.buffer.readline()
                if hdr_bytes in (b"\r\n", b"\n", b""):
                    break
            if not hdr_bytes:
                break
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


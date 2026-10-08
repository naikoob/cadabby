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
from cadabby.lint import run_vault_lint
from cadabby.ops import ground_notes, scaffold_note, update_note, verify_note
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
        "description": "Full-text BM25 search across vault notes boosted by epistemic trust tiers and note status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query terms"},
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
                "limit": {"type": "integer", "default": 20, "description": "Max results to return"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "vault_ground",
        "description": "Retrieve full note markdown content and 1-hop link/backlink/source graph for specified CIDs, note stems, or wikilinks.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of note CIDs, bare stems (e.g. 'Raft-Consensus'), relative paths, or wikilinks to retrieve",
                },
                "budget_tokens": {
                    "type": "integer",
                    "description": "Optional token budget. Truncates cleanly at section boundaries if exceeded.",
                },
            },
            "required": ["cids"],
        },
    },
    {
        "name": "vault_status",
        "description": "Epistemic health summary: note counts by tier and type, verification debt (stale count), unprocessed and filename-only raw files, and broken-link count.",
        "inputSchema": {
            "type": "object",
            "properties": {},
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
                "path": {"type": "string", "description": "Optional custom relative path within domain or vault"},
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
            "in one write with one log entry. Never truncates a file it could not fully parse."
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
        "description": "Appends a verification entry stamped by: agent:<client_id>, at: now, and of: the current body hash. Writing human:* over MCP is refused unconditionally.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cid": {"type": "string", "description": "Note CID or relative path"},
                "method": {"type": "string", "default": "automated-check", "description": "Verification method"},
            },
            "required": ["cid"],
        },
    },
    {
        "name": "vault_lint",
        "description": "Runs the six normative epistemic lint gates and returns structured diagnostics so an agent can self-heal its own output.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]

_REQUIRED_ARGS: dict[str, tuple[str, ...]] = {
    t["name"]: tuple(t["inputSchema"].get("required", ())) for t in TOOLS  # type: ignore[index]
}


def _required_args(tool: str) -> tuple[str, ...]:
    """Required fields a tool declares. Empty for tools that declare none."""
    return _REQUIRED_ARGS.get(tool, ())


def _section_arg(args: dict[str, Any], key: str) -> tuple[str, str] | None:
    """Parse an optional [heading, body] array parameter for vault_update_note."""
    s = args.get(key)
    if s is None:
        return None
    if not isinstance(s, (list, tuple)) or not (1 <= len(s) <= 2) or not all(isinstance(x, str) for x in s):
        raise ValueError(f"'{key}' must be a [heading, body] array of strings")
    return (s[0], s[1]) if len(s) > 1 else (s[0], "")


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
            query=args["query"],
            type_=args.get("type"),
            status=args.get("status"),
            trust=args.get("trust"),
            tag=args.get("tag"),
            domain=args.get("domain"),
            limit=args.get("limit", 20),
        )
        return _tool_ok([r.to_dict() for r in res])

    def _tool_ground(self, args: dict[str, Any]) -> dict[str, Any]:
        cids = args["cids"]
        if not isinstance(cids, list) or not all(isinstance(c, str) for c in cids):
            raise ValueError("'cids' must be a list of strings")
        self.cache.scan()
        grounded = ground_notes(
            self.vault,
            cids,
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
            body=args.get("body", ""),
            actor=self.client_id,
            domain=args.get("domain", DIR_WIKI),
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
            method=args.get("method", "automated-check"),
            is_human_authorized=False,
            cache=self.cache,
        )
        return _tool_ok(
            f"Attested {res['cid']} by {res['actor']} "
            f"with content-binding {res['of'][:16]}... "
            f"-> derived trust tier: '{res['trust_tier']}'"
        )

    def _tool_status(self, _args: dict[str, Any]) -> dict[str, Any]:
        self.cache.scan()
        return _tool_ok(self.cache.get_status())

    def _tool_lint(self, _args: dict[str, Any]) -> dict[str, Any]:
        findings = run_vault_lint(self.vault, cache=self.cache)
        out = [f.to_dict() for f in findings]
        has_errors = any(f.severity == "error" for f in findings)
        # Deliberately neither _tool_ok nor _tool_error: the call
        # succeeded, and isError mirrors the CLI's exit 1 for "the
        # vault has findings" (§5.4). The body is the findings list,
        # not an error envelope.
        return {
            "content": [{"type": "text", "text": json.dumps(out, indent=2)}],
            "isError": has_errors,
        }

    _TOOL_HANDLERS = {
        "vault_search": _tool_search,
        "vault_ground": _tool_ground,
        "vault_scaffold_note": _tool_scaffold_note,
        "vault_update_note": _tool_update_note,
        "vault_verify_note": _tool_verify_note,
        "vault_status": _tool_status,
        "vault_lint": _tool_lint,
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
            return _tool_error(name, UNKNOWN_TOOL, f"Unknown tool: {name}", retryable=False)

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


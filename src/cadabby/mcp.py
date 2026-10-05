"""Model Context Protocol (MCP 2024-11-05) JSON-RPC 2.0 stdio server.

Conforms strictly to Cadabby Technical Specification §5.1, §5.2.
"""

from __future__ import annotations

import json
import re
import sys
from types import TracebackType
from typing import Any, Self

from cadabby import __version__
from cadabby.cache import VaultCache
from cadabby.errors import HUMAN_ATTESTATION_REFUSED, INVALID_ARGUMENT, UNKNOWN_TOOL, classify
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
        "description": "Epistemic health summary: note counts by tier and type, verification debt (stale count), unprocessed raw files, broken-link and orphan counts.",
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
        "description": "Non-destructive frontmatter patches and content section append/replace-by-heading. Never truncates a file it could not fully parse.",
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
                    "description": "[heading, section_body] to append to note",
                },
                "replace_section": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "[heading, new_section_body] to replace in note",
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


class McpServer:
    """JSON-RPC 2.0 stdio MCP Server implementation."""

    def __init__(self, vault: Vault):
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

    def handle_tools_call(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Route tool invocation to underlying engine functions."""
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

        try:
            if name == "vault_search":
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

            elif name == "vault_ground":
                grounded = ground_notes(
                    self.vault,
                    args["cids"],
                    budget_tokens=args.get("budget_tokens"),
                    cache=self.cache,
                )
                return _tool_ok(grounded)

            elif name == "vault_scaffold_note":
                path = scaffold_note(
                    vault=self.vault,
                    title=args["title"],
                    type_=args["type"],
                    description=args["description"],
                    tags=args.get("tags"),
                    sources=args.get("sources"),
                    body=args.get("body", ""),
                    actor=self.client_id,
                    domain=args.get("domain", "wiki"),
                    path=args.get("path"),
                    template=args.get("template"),
                )
                rel = self.vault.rel_path(path)
                return _tool_ok(f"Scaffolded note: {rel} (CID: {path_to_cid(rel)})")

            elif name == "vault_update_note":
                app_sec = None
                if args.get("append_section"):
                    s = args["append_section"]
                    app_sec = (s[0], s[1]) if len(s) > 1 else (s[0], "")

                rep_sec = None
                if args.get("replace_section"):
                    s = args["replace_section"]
                    rep_sec = (s[0], s[1]) if len(s) > 1 else (s[0], "")

                path = update_note(
                    vault=self.vault,
                    cid_or_path=args["cid"],
                    frontmatter_patch=args.get("patch_frontmatter"),
                    append_section=app_sec,
                    replace_section=rep_sec,
                    expected_hash=args.get("expected_hash"),
                    actor=self.client_id,
                )
                rel = self.vault.rel_path(path)
                return _tool_ok(f"Updated note: {rel}")

            elif name == "vault_verify_note":
                actor_param = args.get("actor") or args.get("by")
                if actor_param and str(actor_param).startswith("human:"):
                    return _tool_error(
                        name,
                        HUMAN_ATTESTATION_REFUSED,
                        "Verification by human:* cannot be performed over MCP.",
                        retryable=False,
                    )

                res = verify_note(
                    vault=self.vault,
                    cid_or_path=args["cid"],
                    actor=self.client_id,
                    method=args.get("method", "automated-check"),
                    is_human_authorized=False,
                )
                return _tool_ok(
                    f"Attested {res['cid']} by {res['actor']} "
                    f"with content-binding {res['of'][:16]}... "
                    f"-> derived trust tier: '{res['trust_tier']}'"
                )

            elif name == "vault_status":
                self.cache.scan()
                status_out = self.cache.get_status()
                return _tool_ok(status_out)

            elif name == "vault_lint":
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

            else:
                return _tool_error(name, UNKNOWN_TOOL, f"Unknown tool: {name}", retryable=False)

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


def run_mcp_server(vault: Vault) -> int:
    """Run stdio JSON-RPC MCP server loop."""

    def write_response(resp: dict[str, Any]) -> None:
        """Emit a JSON-RPC response with a byte-accurate Content-Length header."""
        out_bytes = json.dumps(resp).encode("utf-8")
        header = f"Content-Length: {len(out_bytes)}\r\n\r\n".encode("ascii")
        sys.stdout.buffer.write(header + out_bytes)
        sys.stdout.buffer.flush()

    def write_error(code: int, message: str) -> None:
        """Emit an id-less error so a client awaiting a reply fails fast."""
        write_response({"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}})

    with McpServer(vault) as server:
        # Read lines from stdin using binary buffer for exact byte counts
        while True:
            raw_line = sys.stdin.buffer.readline()
            if not raw_line:
                break

            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            # Check for Content-Length header framing
            if line.lower().startswith("content-length:"):
                try:
                    length = int(line.split(":", 1)[1].strip())
                except ValueError:
                    length = -1
                if length < 0:
                    # The payload length is unknown, so the following bytes
                    # cannot be consumed and the stream cannot be resynchronized;
                    # reply and stop rather than reading payload as headers.
                    write_error(-32700, "Parse error: malformed Content-Length header")
                    break
                # Read through any remaining header lines until empty line
                while True:
                    hdr_bytes = sys.stdin.buffer.readline()
                    if hdr_bytes in (b"\r\n", b"\n", b""):
                        break
                payload_bytes = sys.stdin.buffer.read(length)
                try:
                    req = json.loads(payload_bytes.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    write_error(-32700, "Parse error: request body is not valid JSON")
                    continue
            else:
                try:
                    req = json.loads(line)
                except json.JSONDecodeError:
                    write_error(-32700, "Parse error: request body is not valid JSON")
                    continue

            if not isinstance(req, dict):
                # Batches and scalar payloads are unsupported. A batch holding
                # only notifications must draw no reply at all (JSON-RPC 2.0 §6);
                # anything else gets an error so a client waiting on a reply
                # fails fast instead of blocking.
                if (
                    isinstance(req, list)
                    and req
                    and all(isinstance(item, dict) and item.get("id") is None for item in req)
                ):
                    continue
                write_error(
                    -32600,
                    "Invalid Request: batch and non-object payloads are not supported",
                )
                continue

            req_id = req.get("id")
            method = req.get("method")
            # params may be absent, explicitly null, or (per JSON-RPC) positional
            params = req.get("params")
            if not isinstance(params, dict):
                params = {}

            # Handle notifications (no response needed)
            if req_id is None:
                continue

            resp: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}

            if method == "initialize":
                try:
                    resp["result"] = server.handle_initialize(params)
                except Exception as e:  # noqa: BLE001
                    resp["error"] = {"code": -32603, "message": f"Internal error during initialize: {e!s}"}
            elif method == "tools/list":
                try:
                    resp["result"] = server.handle_tools_list()
                except Exception as e:  # noqa: BLE001
                    resp["error"] = {"code": -32603, "message": f"Internal error during tools/list: {e!s}"}
            elif method == "tools/call":
                try:
                    resp["result"] = server.handle_tools_call(
                        params.get("name", ""), params.get("arguments")
                    )
                except Exception as e:  # noqa: BLE001
                    resp["error"] = {"code": -32603, "message": f"Internal error during tools/call: {e!s}"}
            elif method == "resources/list":
                try:
                    resp["result"] = server.handle_resources_list()
                except Exception as e:  # noqa: BLE001
                    resp["error"] = {"code": -32603, "message": f"Internal error during resources/list: {e!s}"}
            elif method == "resources/read":
                try:
                    resp["result"] = server.handle_resources_read(params.get("uri", ""))
                except Exception as e:  # noqa: BLE001
                    resp["error"] = {"code": -32602, "message": f"Resource error: {e!s}"}
            elif method == "ping":
                resp["result"] = {}
            else:
                resp["error"] = {
                    "code": -32601,
                    "message": f"Method not found: {method}",
                }

            write_response(resp)

    return 0

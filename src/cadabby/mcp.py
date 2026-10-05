"""Model Context Protocol (MCP 2024-11-05) JSON-RPC 2.0 stdio server.

Conforms strictly to Cadabby Technical Specification §5, §6.2.
"""

from __future__ import annotations

import json
import re
import sys
from types import TracebackType
from typing import Any, Self

from cadabby import __version__
from cadabby.cache import VaultCache
from cadabby.lint import run_vault_lint
from cadabby.ops import ground_notes, scaffold_note, update_note, verify_note
from cadabby.vault import Vault, path_to_cid

TOOLS = [
    {
        "name": "vault_search",
        "description": "Full-text BM25 search across vault notes boosted by epistemic trust tiers and note status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query terms"},
                "domain": {"type": "string", "description": "Filter by cognitive domain (e.g. 'wiki', 'customers', 'projects')"},
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
        "description": "Retrieve full note markdown content and 1-hop link/backlink/source graph for specified CIDs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of note CIDs to retrieve",
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
                "patch_frontmatter": {"type": "object", "description": "Dictionary of frontmatter fields to patch"},
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
                data = [r.to_dict() for r in res]
                return {"content": [{"type": "text", "text": json.dumps(data, indent=2)}], "isError": False}

            elif name == "vault_ground":
                grounded = ground_notes(
                    self.vault,
                    args["cids"],
                    budget_tokens=args.get("budget_tokens"),
                    cache=self.cache,
                )
                return {"content": [{"type": "text", "text": json.dumps(grounded, indent=2)}], "isError": False}

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
                )
                rel = self.vault.rel_path(path)
                return {
                    "content": [{"type": "text", "text": f"Scaffolded note: {rel} (CID: {path_to_cid(rel)})"}],
                    "isError": False,
                }

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
                return {
                    "content": [{"type": "text", "text": f"Updated note: {rel}"}],
                    "isError": False,
                }

            elif name == "vault_verify_note":
                actor_param = args.get("actor") or args.get("by")
                if actor_param and str(actor_param).startswith("human:"):
                    return {
                        "content": [
                            {"type": "text", "text": "Error: Verification by human:* cannot be performed over MCP."}
                        ],
                        "isError": True,
                    }

                res = verify_note(
                    vault=self.vault,
                    cid_or_path=args["cid"],
                    actor=self.client_id,
                    method=args.get("method", "automated-check"),
                    is_human_authorized=False,
                )
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Attested {res['cid']} by {res['actor']} "
                                f"with content-binding {res['of'][:16]}... "
                                f"-> derived trust tier: '{res['trust_tier']}'"
                            ),
                        }
                    ],
                    "isError": False,
                }

            elif name == "vault_status":
                self.cache.scan()
                status_out = self.cache.get_status()
                return {"content": [{"type": "text", "text": json.dumps(status_out, indent=2)}], "isError": False}

            elif name == "vault_lint":
                findings = run_vault_lint(self.vault, cache=self.cache)
                out = [f.to_dict() for f in findings]
                has_errors = any(f.severity == "error" for f in findings)
                return {
                    "content": [{"type": "text", "text": json.dumps(out, indent=2)}],
                    "isError": has_errors,
                }

            else:
                return {
                    "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
                    "isError": True,
                }

        except Exception as e:  # noqa: BLE001
            return {
                "content": [{"type": "text", "text": f"Error executing {name}: {e!s}"}],
                "isError": True,
            }

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
                    "searchable": d.searchable,
                    "allowed_types": d.allowed_types,
                    "require_sources": d.require_sources,
                    "enforce_layout": d.enforce_layout,
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

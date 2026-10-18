"""Unit tests for Model Context Protocol (MCP) server implementation."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from cadabby.mcp import TOOLS, McpServer, run_mcp_server
from cadabby.vault import Vault
from tests.helpers import copy_demo_vault


class TestMcpServer(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.vault_root = copy_demo_vault(Path(self.tmp_dir.name) / "demo-vault")
        self.vault = Vault(self.vault_root)
        self.server = McpServer(self.vault)

    def tearDown(self):
        self.server.close()
        self.tmp_dir.cleanup()

    def test_initialize_and_tools_list(self):
        init_res = self.server.handle_initialize(
            {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "claude-code-test", "version": "1.0"},
            }
        )
        self.assertEqual(init_res["protocolVersion"], "2024-11-05")
        self.assertEqual(init_res["serverInfo"]["name"], "cadabby")
        self.assertEqual(self.server.client_id, "agent:claude-code-test")

        tools_res = self.server.handle_tools_list()
        tool_names = [t["name"] for t in tools_res["tools"]]
        expected = [
            "vault_search",
            "vault_ground",
            "vault_status",
            "vault_graph",
            "vault_scaffold_note",
            "vault_update_note",
            "vault_move_note",
            "vault_verify_note",
        ]
        self.assertEqual(len(tools_res["tools"]), 8)
        self.assertEqual(set(tool_names), set(expected))

    def test_update_note_schema_exposes_edits(self):
        tools = {t["name"]: t for t in self.server.handle_tools_list()["tools"]}
        self.assertEqual(len(tools), 8)
        edits = tools["vault_update_note"]["inputSchema"]["properties"]["edits"]
        self.assertEqual(edits["type"], "array")
        self.assertEqual(
            set(edits["items"]["properties"]["op"]["enum"]),
            {"replace_text", "replace_section", "append_section"},
        )
        # Over the wire the batch is all-or-nothing: a bad third edit leaves the file alone.
        target = self.vault_root / "wiki" / "sqlite.md"
        before = target.read_bytes()
        res = self.server.handle_tools_call(
            "vault_update_note",
            {
                "cid": "wiki/sqlite",
                "edits": [
                    {"op": "replace_text", "old": "small, fast", "new": "small and fast"},
                    {"op": "replace_text", "old": "text that is not in the note", "new": "x"},
                ],
            },
        )
        self.assertTrue(res["isError"])
        self.assertEqual(target.read_bytes(), before)

    def test_tools_call_search_and_ground(self):
        self.server.handle_initialize({"clientInfo": {"name": "test-agent"}})

        # Search
        res_search = self.server.handle_tools_call("vault_search", {"query": "SQLite"})
        self.assertFalse(res_search["isError"])
        data_search = json.loads(res_search["content"][0]["text"])
        self.assertGreater(len(data_search), 0)

        # Search with hyphenated words
        res_search_hyphen = self.server.handle_tools_call("vault_search", {"query": "SQLite-vs-DuckDB"})
        self.assertFalse(res_search_hyphen["isError"])
        data_search_hyphen = json.loads(res_search_hyphen["content"][0]["text"])
        self.assertGreater(len(data_search_hyphen), 0)

        # Ground with bare stem and wikilink
        res_ground = self.server.handle_tools_call("vault_ground", {"targets": ["sqlite", "[[epistemic-trust-tiers]]"]})
        self.assertFalse(res_ground["isError"])
        data_ground = json.loads(res_ground["content"][0]["text"])
        self.assertEqual(len(data_ground), 2)
        grounded_cids = [g["cid"] for g in data_ground]
        self.assertIn("wiki/sqlite", grounded_cids)
        self.assertIn("wiki/epistemic-trust-tiers", grounded_cids)

    def test_tools_call_scaffold_update_verify_status_lint(self):
        self.server.handle_initialize({"clientInfo": {"name": "agent-smith"}})

        # Scaffold
        res_scaffold = self.server.handle_tools_call(
            "vault_scaffold_note",
            {
                "title": "Quantum Computing",
                "type": "concept",
                "description": "Superposition and entanglement principles",
                "body": "# Quantum Computing\n\nQubits and quantum gates.\n",
            },
        )
        self.assertFalse(res_scaffold["isError"])

        # Immediate search must find the newly scaffolded note without external sync
        res_immediate_search = self.server.handle_tools_call("vault_search", {"query": "Superposition"})
        self.assertFalse(res_immediate_search["isError"])
        search_hits = json.loads(res_immediate_search["content"][0]["text"])
        self.assertTrue(any(h["cid"] == "wiki/quantum-computing" for h in search_hits))

        # Immediate ground must retrieve the newly scaffolded note
        res_immediate_ground = self.server.handle_tools_call("vault_ground", {"targets": ["wiki/quantum-computing"]})
        self.assertFalse(res_immediate_ground["isError"])
        ground_hits = json.loads(res_immediate_ground["content"][0]["text"])
        self.assertEqual(len(ground_hits), 1)
        self.assertEqual(ground_hits[0]["cid"], "wiki/quantum-computing")

        # Update note
        res_update = self.server.handle_tools_call(
            "vault_update_note",
            {
                "cid": "wiki/quantum-computing",
                "edits": [
                    {"op": "append_section", "heading": "Algorithms", "body": "Shor's algorithm and Grover's algorithm."}
                ],
            },
        )
        self.assertFalse(res_update["isError"])

        # Verify: should stamp agent-smith and bind hash
        res_verify = self.server.handle_tools_call(
            "vault_verify_note",
            {"cid": "wiki/quantum-computing", "method": "automated-check"},
        )
        self.assertFalse(res_verify["isError"])
        self.assertIn("agent:agent-smith", res_verify["content"][0]["text"])

        # Status
        res_status = self.server.handle_tools_call("vault_status", {})
        self.assertFalse(res_status["isError"])
        status_data = json.loads(res_status["content"][0]["text"])
        self.assertGreater(status_data["total_notes"], 0)

        # Status with lint findings (vault_lint folded in, §5.1)
        res_lint = self.server.handle_tools_call("vault_status", {"lint": True})
        self.assertFalse(res_lint["isError"])
        self.assertIn("findings", json.loads(res_lint["content"][0]["text"]))

    def test_status_reports_finding_counts_and_findings_only_on_request(self):
        """§10 C46. Findings describe the vault, not the call: never isError; vault_lint is gone."""
        (self.vault_root / "wiki" / "Dead-Linker.md").write_text(
            "---\ntype: concept\ntitle: Dead Linker\ndescription: d\nstatus: active\n---\n[[Nowhere]] [[sqlite]]\n",
            "utf-8",
        )
        plain = self.server.handle_tools_call("vault_status", {})
        self.assertFalse(plain["isError"])
        report = json.loads(plain["content"][0]["text"])
        self.assertEqual(report["finding_counts"]["error"], 1)
        self.assertEqual(report["finding_counts"]["by_code"]["LINK_DEAD"], 1)
        self.assertNotIn("findings", report)

        full = self.server.handle_tools_call("vault_status", {"lint": True})
        self.assertFalse(full["isError"])
        codes = [f["code"] for f in json.loads(full["content"][0]["text"])["findings"]]
        self.assertIn("LINK_DEAD", codes)

        gone = self.server.handle_tools_call("vault_lint", {})
        self.assertTrue(gone["isError"])
        error = json.loads(gone["content"][0]["text"])["error"]
        self.assertEqual(error["code"], "UNKNOWN_TOOL")
        self.assertIn("vault_status with lint: true", error["message"])

        never = json.loads(self.server.handle_tools_call("vault_nope", {})["content"][0]["text"])["error"]
        self.assertEqual(never["message"], "Unknown tool: vault_nope")

    def test_persistent_cache_reuse_and_context_manager(self):
        # Cache connection is lazily opened or persistent
        conn_before = self.server.cache.get_connection()
        self.assertIsNotNone(conn_before)

        # Invocations of search and status (with and without lint) reuse this exact connection
        self.server.handle_tools_call("vault_search", {"query": "SQLite"})
        self.assertIs(self.server.cache.get_connection(), conn_before)

        self.server.handle_tools_call("vault_status", {})
        self.assertIs(self.server.cache.get_connection(), conn_before)

        self.server.handle_tools_call("vault_status", {"lint": True})
        self.assertIs(self.server.cache.get_connection(), conn_before)

        # Context manager lifecycle
        with McpServer(self.vault) as scoped_server:
            scoped_conn = scoped_server.cache.get_connection()
            self.assertIsNotNone(scoped_conn)

        # Connection should be closed after exit
        self.assertIsNone(scoped_server.cache._conn)

    def _drive_server(self, requests):
        """Feed Content-Length framed requests through run_mcp_server and parse responses."""
        input_chunks = []
        for r in requests:
            r_bytes = r.encode("utf-8")
            hdr = f"Content-Length: {len(r_bytes)}\r\n\r\n".encode("ascii")
            input_chunks.append(hdr + r_bytes)
        return self._drive_server_raw(b"".join(input_chunks))

    def _drive_server_raw(self, stdin_bytes, with_framing=False):
        """Feed arbitrary stdin bytes through run_mcp_server and parse responses.

        Replies may arrive in either framing (§5.2); each is parsed in kind.
        With `with_framing`, returns (framing, message) pairs so a test can
        assert which framing the server chose.
        """
        stdin_stream = io.BytesIO(stdin_bytes)
        stdout_stream = io.BytesIO()

        class DummyStdStream:
            def __init__(self, buffer):
                self.buffer = buffer

        old_stdin = sys.stdin
        old_stdout = sys.stdout
        try:
            sys.stdin = DummyStdStream(stdin_stream)
            sys.stdout = DummyStdStream(stdout_stream)
            ret = run_mcp_server(self.vault)
            self.assertEqual(ret, 0)
        finally:
            sys.stdin = old_stdin
            sys.stdout = old_stdout

        stdout_bytes = stdout_stream.getvalue()
        framed = []
        idx = 0
        while idx < len(stdout_bytes):
            if stdout_bytes[idx:].lower().startswith(b"content-length:"):
                header_end = stdout_bytes.find(b"\r\n\r\n", idx)
                self.assertNotEqual(header_end, -1)
                hdr_str = stdout_bytes[idx:header_end].decode("ascii")
                length = int(hdr_str.split(":", 1)[1].strip())
                body_start = header_end + 4
                body_bytes = stdout_bytes[body_start : body_start + length]
                self.assertEqual(len(body_bytes), length)
                framed.append(("content-length", json.loads(body_bytes.decode("utf-8"))))
                idx = body_start + length
            else:
                line_end = stdout_bytes.find(b"\n", idx)
                self.assertNotEqual(line_end, -1, "an ndjson reply must end with a newline")
                framed.append(("ndjson", json.loads(stdout_bytes[idx:line_end].decode("utf-8"))))
                idx = line_end + 1
        return framed if with_framing else [msg for _, msg in framed]

    def test_oversized_frame_is_refused_and_server_keeps_serving(self):
        """§5.2, §10 C62: a message over the cap is answered -32600 and drained; the next one is served."""
        from unittest import mock

        ping = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}).encode("utf-8")
        big = b'{"jsonrpc": "2.0", "id": 1, "method": "ping", "pad": "' + b"x" * 4000 + b'"}'
        with mock.patch("cadabby.mcp.MAX_MESSAGE_BYTES", 1024):
            for label, stream in (
                ("content-length", f"Content-Length: {len(big)}\r\n\r\n".encode() + big + ping + b"\n"),
                ("ndjson", big + b"\n" + ping + b"\n"),
            ):
                with self.subTest(framing=label):
                    replies = self._drive_server_raw(stream)
                    self.assertEqual(replies[0]["error"]["code"], -32600)
                    self.assertIn("exceeds", replies[0]["error"]["message"])
                    self.assertEqual(replies[1], {"jsonrpc": "2.0", "id": 7, "result": {}})

            # A length far past what ever arrives is answered at once, not allocated (the reviewer's repro).
            replies = self._drive_server_raw(b"Content-Length: 99999999999999\r\n\r\n{}")
            self.assertEqual(replies[0]["error"]["code"], -32600)

    def test_null_required_argument_is_missing(self):
        """§5.1, §10 C62: a JSON null for a required argument is reported missing at the boundary, for every tool."""
        from cadabby.mcp import TOOLS

        for tool in TOOLS:
            for key in tool["inputSchema"].get("required", ()):
                with self.subTest(tool=tool["name"], arg=key):
                    args = {k: "x" for k in tool["inputSchema"]["required"]} | {key: None}
                    res = self.server.handle_tools_call(tool["name"], args)
                    self.assertTrue(res["isError"])
                    error = json.loads(res["content"][0]["text"])["error"]
                    self.assertEqual(error["code"], "INVALID_ARGUMENT")
                    self.assertIn(f"Missing required argument(s): {key}", error["message"])

    def test_non_ascii_client_name_yields_a_valid_actor(self):
        """§3.4, §10 C62: the client name is reduced to the ASCII actor grammar; malformed clientInfo is tolerated."""
        self.server.handle_initialize({"clientInfo": {"name": "Café Agent"}})
        self.assertEqual(self.server.client_id, "agent:Caf--Agent")
        res = self.server.handle_tools_call("vault_verify_note", {"cid": "wiki/flash-attention"})
        self.assertFalse(res["isError"], res)

        for params in ({"clientInfo": "not-an-object"}, {"clientInfo": {"name": ["x"]}}, None):
            with self.subTest(params=params):
                self.server.handle_initialize(params)
                self.assertRegex(self.server.client_id, r"^agent:[A-Za-z0-9._-]+$")

    def test_scaffold_refuses_empty_core_fields_over_mcp(self):
        """§5.1, §10 C62: an empty title, type or description is refused and nothing is written."""
        for field in ("title", "type", "description"):
            with self.subTest(field=field):
                args = {"title": "T", "type": "concept", "description": "d", field: ""}
                res = self.server.handle_tools_call("vault_scaffold_note", args)
                self.assertTrue(res["isError"])
                self.assertEqual(json.loads(res["content"][0]["text"])["error"]["code"], "INVALID_ARGUMENT")
        self.assertFalse((self.vault_root / "wiki" / "untitled.md").exists())
        self.assertFalse((self.vault_root / "wiki" / "t.md").exists())

    def test_newline_delimited_request_gets_newline_delimited_reply(self):
        """§10 C27. The MCP stdio standard framing is answered in kind, with no header."""
        req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "t"}}})
        stdin_bytes = (req + "\n").encode("utf-8")
        framed = self._drive_server_raw(stdin_bytes, with_framing=True)
        self.assertEqual([f for f, _ in framed], ["ndjson"])
        self.assertEqual(framed[0][1]["id"], 1)
        self.assertIn("serverInfo", framed[0][1]["result"])

    def test_content_length_request_keeps_content_length_framing(self):
        """§10 C27. LSP-style clients keep receiving LSP-style replies."""
        req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode("utf-8")
        stdin_bytes = b"Content-Length: %d\r\n\r\n" % len(req) + req
        framed = self._drive_server_raw(stdin_bytes, with_framing=True)
        self.assertEqual([f for f, _ in framed], ["content-length"])

    def test_mixed_framing_stream_answers_each_message_in_kind(self):
        """Framing is decided per message, not latched by the first one."""
        a = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        b = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}).encode("utf-8")
        stdin_bytes = (a + "\n").encode("utf-8") + b"Content-Length: %d\r\n\r\n" % len(b) + b
        framed = self._drive_server_raw(stdin_bytes, with_framing=True)
        self.assertEqual([(f, m["id"]) for f, m in framed], [("ndjson", 1), ("content-length", 2)])

    def test_run_mcp_server_survives_malformed_params(self):
        # Explicit null/non-dict params must not kill the loop or drop later requests
        requests = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": None}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": ["positional"]}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "vault_search"}}),
            json.dumps({"jsonrpc": "2.0", "id": 4, "method": "ping"}),
        ]
        responses = self._drive_server(requests)

        self.assertEqual([r["id"] for r in responses], [1, 2, 3, 4])
        # No tool name at all is malformed params (-32602); a missing required
        # argument is a tool error. Neither is a crash.
        for resp in responses[:2]:
            self.assertEqual(resp["error"]["code"], -32602)
        self.assertTrue(responses[2]["result"]["isError"])
        # The request following the malformed ones is still answered
        self.assertEqual(responses[3].get("result"), {})

    def test_run_mcp_server_rejects_batch_payload(self):
        # A batch (array) payload is unsupported, but must be answered so a
        # client awaiting a reply fails fast instead of blocking forever.
        requests = [
            json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"}]),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
        ]
        responses = self._drive_server(requests)

        self.assertEqual(len(responses), 2)
        self.assertIsNone(responses[0]["id"])
        self.assertEqual(responses[0]["error"]["code"], -32600)
        # The well-formed request after the batch is still served
        self.assertEqual(responses[1]["id"], 2)
        self.assertEqual(responses[1]["result"], {})

    def test_run_mcp_server_notification_only_batch_is_silent(self):
        # JSON-RPC 2.0 §6: a batch holding only notifications draws no reply,
        # so the unsupported-batch error must not be emitted for one.
        requests = [
            json.dumps([
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "method": "notifications/cancelled"},
            ]),
            json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}),
        ]
        responses = self._drive_server(requests)

        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["id"], 9)
        self.assertEqual(responses[0]["result"], {})

    def test_run_mcp_server_answers_parse_errors(self):
        # Unparseable JSON must draw a -32700 rather than silence: a client
        # holding a pending id would otherwise block until timeout.
        good = json.dumps({"jsonrpc": "2.0", "id": 4, "method": "ping"}).encode("utf-8")
        bad_framed = b'{"jsonrpc": "2.0", truncated'
        stdin_bytes = (
            b"{not json at all\n"
            + b"Content-Length: %d\r\n\r\n" % len(bad_framed)
            + bad_framed
            + b"Content-Length: %d\r\n\r\n" % len(good)
            + good
        )
        responses = self._drive_server_raw(stdin_bytes)

        self.assertEqual(len(responses), 3)
        for resp in responses[:2]:
            self.assertIsNone(resp["id"])
            self.assertEqual(resp["error"]["code"], -32700)
        # The stream stays framed, so the following request is still served
        self.assertEqual(responses[2]["id"], 4)
        self.assertEqual(responses[2]["result"], {})

    def test_run_mcp_server_rejects_unparseable_content_length(self):
        # The payload length is unknown, so the body cannot be consumed and the
        # stream cannot be resynchronized: reply, then stop reading.
        for header in (b"Content-Length: notanumber", b"Content-Length: -5"):
            with self.subTest(header=header):
                responses = self._drive_server_raw(header + b'\r\n\r\n{"jsonrpc": "2.0"}')
                self.assertEqual(len(responses), 1)
                self.assertIsNone(responses[0]["id"])
                self.assertEqual(responses[0]["error"]["code"], -32700)

    def test_run_mcp_server_framing_and_resource_error(self):
        # Build stdin bytes with Content-Length headers containing multi-byte characters and invalid resource read
        req1 = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "test"}}})
        req2 = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": "cadabby://invalid-uri"}})
        # req3 contains multi-byte UTF-8 emoji and em-dash
        req3 = json.dumps({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "vault_search", "arguments": {"query": "SQLite 🚀 — database"}},
        })

        responses = self._drive_server((req1, req2, req3))

        self.assertEqual(len(responses), 3)
        # req1 (initialize)
        self.assertEqual(responses[0]["id"], 1)
        self.assertIn("serverInfo", responses[0]["result"])

        # req2 (resources/read with invalid URI) -> must return error, not crash
        self.assertEqual(responses[1]["id"], 2)
        self.assertIn("error", responses[1])
        self.assertEqual(responses[1]["error"]["code"], -32602)

        # req3 (vault_search with Unicode)
        self.assertEqual(responses[2]["id"], 3)
        self.assertIn("result", responses[2])

    def test_dispatch_request_routes_methods_and_suppresses_notifications(self):
        self.assertIsNone(
            self.server.dispatch_request({"jsonrpc": "2.0", "method": "notifications/initialized"})
        )

        ping_resp = self.server.dispatch_request({"jsonrpc": "2.0", "id": 10, "method": "ping"})
        self.assertEqual(ping_resp, {"jsonrpc": "2.0", "id": 10, "result": {}})

        unknown_resp = self.server.dispatch_request({"jsonrpc": "2.0", "id": 11, "method": "unknown/method"})
        self.assertEqual(unknown_resp["id"], 11)
        self.assertEqual(unknown_resp["error"]["code"], -32601)

        res_err = self.server.dispatch_request(
            {"jsonrpc": "2.0", "id": 12, "method": "resources/read", "params": {"uri": "bad://uri"}}
        )
        self.assertEqual(res_err["id"], 12)
        self.assertEqual(res_err["error"]["code"], -32602)

    def test_handle_tools_call_rejects_malformed_arguments_and_section_shapes(self):
        non_dict = self.server.handle_tools_call("vault_status", ["not", "a", "dict"])  # type: ignore[arg-type]
        self.assertTrue(non_dict["isError"])
        self.assertEqual(json.loads(non_dict["content"][0]["text"])["error"]["code"], "INVALID_ARGUMENT")

        str_targets = self.server.handle_tools_call("vault_ground", {"targets": "wiki/SQLite"})
        self.assertTrue(str_targets["isError"])
        self.assertEqual(json.loads(str_targets["content"][0]["text"])["error"]["code"], "INVALID_ARGUMENT")

        str_edits = self.server.handle_tools_call(
            "vault_update_note",
            {"cid": "wiki/SQLite", "edits": "Heading:Body"},
        )
        self.assertTrue(str_edits["isError"])
        self.assertEqual(json.loads(str_edits["content"][0]["text"])["error"]["code"], "INVALID_ARGUMENT")

        # Every argument is checked against its inputSchema before dispatch (§5.1, §5.4):
        # nothing is coerced, nothing mistyped reaches the engine or the disk.
        (self.vault_root / "wiki" / "Movable.md").write_text(
            "---\ntype: concept\ntitle: Movable\ndescription: d\nstatus: active\n---\n# Movable\n", "utf-8"
        )
        for tool, bad_args, message in (
            ("vault_move_note", {"cid": "wiki/Movable", "to": "movable-x", "apply": "false"}, "'apply' must be a boolean"),
            ("vault_move_note", {"cid": "wiki/Movable", "to": "movable-x", "approve_stale": ""}, "'approve_stale' must be an array"),
            ("vault_move_note", {"cid": "wiki/Movable", "to": "movable-x", "approve_stale": [""]}, "'approve_stale[0]' must not be empty"),
            ("vault_move_note", {"cid": "", "to": "movable-x"}, "'cid' must not be empty"),
            ("vault_move_note", {"cid": "wiki/Movable", "to": 123}, "'to' must be a string"),
            ("vault_status", {"lint": "false"}, "'lint' must be a boolean"),
            ("vault_search", {"query": "x", "limit": True}, "'limit' must be an integer"),
            ("vault_search", {"query": "x", "limit": 0}, "'limit' must be at least 1"),
            ("vault_search", {"query": 5}, "'query' must be a string"),
            ("vault_ground", {"targets": ["SQLite"], "budget_tokens": "50"}, "'budget_tokens' must be an integer"),
            ("vault_scaffold_note", {"title": "T", "type": "concept", "description": "d", "domain": 5}, "'domain' must be a string"),
            ("vault_scaffold_note", {"title": "T", "type": "concept", "description": "d", "tags": "a,b"}, "'tags' must be an array"),
            ("vault_update_note", {"cid": "wiki/SQLite", "patch_frontmatter": "status: active"}, "'patch_frontmatter' must be an object"),
            ("vault_update_note", {"cid": "wiki/SQLite", "edits": [{"op": "delete"}]}, "'edits[0].op' must be one of"),
        ):
            with self.subTest(tool=tool, bad_args=bad_args):
                res = self.server.handle_tools_call(tool, bad_args)
                self.assertTrue(res["isError"])
                error = json.loads(res["content"][0]["text"])["error"]
                self.assertEqual(error["code"], "INVALID_ARGUMENT")
                self.assertIn(message, error["message"])
        self.assertTrue((self.vault_root / "wiki" / "Movable.md").exists())
        self.assertFalse((self.vault_root / "wiki" / "movable-x.md").exists())
        self.assertFalse((self.vault_root / "5").exists())
        # A JSON null means "not given", not a type error.
        ok = self.server.handle_tools_call("vault_scaffold_note", {"title": "Nulls", "type": "concept", "description": "d", "path": None})
        self.assertFalse(ok["isError"], ok)

    def test_mcp_filter_only_search_and_section_grounding(self):
        """§4.4, §5.1, §10 C55, C56: MCP vault_search supports filter-only queries and vault_ground supports #section + neighbor metadata."""
        self.server.handle_initialize({"clientInfo": {"name": "test-agent"}})

        # 1. vault_search with omitted query and a filter succeeds
        res_filtered = self.server.handle_tools_call("vault_search", {"trust": "human-reviewed"})
        self.assertFalse(res_filtered["isError"])
        hits = json.loads(res_filtered["content"][0]["text"])
        self.assertGreater(len(hits), 0)
        self.assertTrue(all(h["trust_tier"] == "human-reviewed" for h in hits))

        # 2. vault_search with omitted or empty query and NO filters returns INVALID_ARGUMENT
        for bad in ({}, {"query": ""}, {"query": "   "}):
            res_empty = self.server.handle_tools_call("vault_search", bad)
            self.assertTrue(res_empty["isError"])
            err = json.loads(res_empty["content"][0]["text"])["error"]
            self.assertEqual(err["code"], "INVALID_ARGUMENT")

        # 3. vault_ground with #section extracts section and enriches 1-hop neighbors
        res_ground = self.server.handle_tools_call(
            "vault_ground",
            {"targets": ["wiki/epistemic-trust-tiers#Epistemic Trust Tiers"]},
        )
        self.assertFalse(res_ground["isError"])
        grounded = json.loads(res_ground["content"][0]["text"])
        self.assertEqual(len(grounded), 1)
        self.assertEqual(grounded[0]["section"], "Epistemic Trust Tiers")
        self.assertTrue(all("trust_tier" in l and "title" in l for l in grounded[0]["links"]))

        # 4. vault_ground with a missing #section returns the full note with section_error naming the headings
        res_bad_sec = self.server.handle_tools_call(
            "vault_ground",
            {"targets": ["wiki/epistemic-trust-tiers#NoSuchHeading"]},
        )
        self.assertFalse(res_bad_sec["isError"])
        item = json.loads(res_bad_sec["content"][0]["text"])[0]
        self.assertIsNone(item["section"])
        self.assertIn("Epistemic Trust Tiers", item["section_error"])
        self.assertIn("# Epistemic Trust Tiers", item["content"])

    def test_mcp_ground_partial_results(self):
        """§5.1, §10 C58: a bad target comes back as an error entry in place; isError only when nothing grounds."""
        res = self.server.handle_tools_call("vault_ground", {"targets": ["sqlite", "no-such-note"]})
        self.assertFalse(res["isError"])
        entries = json.loads(res["content"][0]["text"])
        self.assertEqual([e["target"] for e in entries], ["sqlite", "no-such-note"])
        self.assertEqual(entries[0]["cid"], "wiki/sqlite")
        self.assertEqual(entries[1]["error"]["code"], "NOT_FOUND")

        only_bad = self.server.handle_tools_call("vault_ground", {"targets": ["no-such-note"]})
        self.assertTrue(only_bad["isError"])
        self.assertEqual(json.loads(only_bad["content"][0]["text"])["error"]["code"], "NOT_FOUND")

        empty = self.server.handle_tools_call("vault_ground", {"targets": []})
        self.assertTrue(empty["isError"])
        self.assertEqual(json.loads(empty["content"][0]["text"])["error"]["code"], "INVALID_ARGUMENT")


class TestMcpProtocolErrors(unittest.TestCase):
    """C66: malformed requests get the JSON-RPC code that names the fault (§5.2)."""

    setUp = TestMcpServer.setUp
    tearDown = TestMcpServer.tearDown
    _drive_server_raw = TestMcpServer._drive_server_raw

    def _call(self, name, args):
        res = self.server.handle_tools_call(name, args)
        return res, json.loads(res["content"][0]["text"])

    def test_request_without_a_method_is_invalid_request(self):
        resp = self.server.dispatch_request({"jsonrpc": "2.0", "id": 7})
        self.assertEqual(resp["error"]["code"], -32600)

    def test_tools_call_with_a_non_string_name_is_invalid_params(self):
        resp = self.server.dispatch_request(
            {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": ["vault_search"]}}
        )
        self.assertEqual(resp["error"]["code"], -32602)

    def test_reading_directives_of_an_unknown_domain_is_invalid_params(self):
        resp = self.server.dispatch_request(
            {"jsonrpc": "2.0", "id": 9, "method": "resources/read", "params": {"uri": "domain://nope/directives"}}
        )
        self.assertEqual(resp["error"]["code"], -32602)
        self.assertIn("nope", resp["error"]["message"])

    def test_null_limit_means_the_default(self):
        res, payload = self._call("vault_search", {"query": "sqlite", "limit": None})
        self.assertFalse(res.get("isError"), payload)
        self.assertTrue(payload)

    def test_unknown_argument_is_refused_by_name(self):
        res, payload = self._call("vault_search", {"qurey": "sqlite"})
        self.assertTrue(res["isError"])
        self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")
        self.assertIn("qurey", payload["error"]["message"])
        schema = next(t for t in self.server.handle_tools_list()["tools"] if t["name"] == "vault_search")
        self.assertIs(schema["inputSchema"]["additionalProperties"], False)

    def test_human_actor_is_still_refused_as_attestation_not_as_unknown_argument(self):
        res, payload = self._call("vault_verify_note", {"cid": "wiki/sqlite", "actor": "human:x"})
        self.assertEqual(payload["error"]["code"], "HUMAN_ATTESTATION_REFUSED")

    def test_content_length_need_not_be_the_first_header(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
        framed = b"Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n" + (
            f"Content-Length: {len(body)}\r\n\r\n".encode() + body
        )
        responses = self._drive_server_raw(framed)
        self.assertEqual([r.get("id") for r in responses], [1])
        self.assertEqual(responses[0]["result"], {})


def _note(root: Path, rel: str, body: str, *, type_: str = "concept", extra: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    title = Path(rel).stem
    path.write_text(
        f"---\ntype: {type_}\ntitle: {title}\ndescription: d\nstatus: active\n{extra}---\n# {title}\n\n{body}\n",
        "utf-8",
    )


class TestEightToolSurface(unittest.TestCase):
    """§5.1, §4.5 and §10 C67-C70: raw grounding, status queues, vault_graph, retired arguments."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = copy_demo_vault(Path(self.tmp_dir.name) / "demo-vault")
        self.server = McpServer(Vault(self.root))

    def tearDown(self):
        self.server.close()
        self.tmp_dir.cleanup()

    def _call(self, tool: str, args: dict) -> dict:
        res = self.server.handle_tools_call(tool, args)
        payload = json.loads(res["content"][0]["text"])
        return payload["error"] if res["isError"] else payload

    def _ok(self, tool: str, args: dict):
        res = self.server.handle_tools_call(tool, args)
        self.assertFalse(res["isError"], res["content"][0]["text"])
        return json.loads(res["content"][0]["text"])

    # -- C67: raw sources over vault_ground --------------------------------

    def test_ground_reads_a_raw_source_section_with_its_citers(self):
        (self.root / "raw" / "paper.md").write_text("# Paper\n\nIntro.\n\n## Results\n\nSpeedup is 2x.\n", "utf-8")
        _note(self.root, "wiki/cites-paper.md", "Body.", extra='sources:\n  - "raw/paper.md"\n')
        [entry] = self._ok("vault_ground", {"targets": ["raw/paper.md#Results"]})
        self.assertEqual(entry["kind"], "raw")
        self.assertEqual(entry["path"], "raw/paper.md")
        self.assertEqual(entry["section"], "Results")
        self.assertIn("Speedup is 2x.", entry["content"])
        self.assertNotIn("Intro.", entry["content"])
        self.assertNotIn("trust_tier", entry)
        self.assertEqual([c["cid"] for c in entry["cited_by"]], ["wiki/cites-paper"])
        self.assertEqual(entry["cited_by"][0]["trust_tier"], "unverified")

    def test_ground_a_pdf_source_is_filename_only(self):
        [entry] = self._ok("vault_ground", {"targets": ["raw/2026-paper-kv.pdf"]})
        self.assertTrue(entry["filename_only"])
        self.assertIsNone(entry["content"])
        self.assertIn("wiki/flash-attention", [c["cid"] for c in entry["cited_by"]])

    def test_ground_refuses_anything_that_is_not_listed_raw_evidence(self):
        (self.root / "raw" / ".hidden.md").write_text("secret", "utf-8")
        outside = Path(self.tmp_dir.name) / "outside.md"
        outside.write_text("outside", "utf-8")
        (self.root / "raw" / "link.md").symlink_to(outside)
        for target in ("raw/../.cadabby.json", "raw/.hidden.md", "raw/link.md", "raw/no-such.md", "raw/../wiki/sqlite.md"):
            with self.subTest(target=target):
                err = self._call("vault_ground", {"targets": [target]})
                self.assertEqual(err["code"], "NOT_FOUND")

    def test_ground_mixes_notes_and_sources_and_budgets_both(self):
        entries = self._ok(
            "vault_ground", {"targets": ["sqlite", "raw/karpathy-llm-wiki-gist.md"], "budget_tokens": 30}
        )
        self.assertEqual([e["kind"] for e in entries], ["note", "raw"])
        self.assertTrue(entries[1]["truncated"])
        self.assertIn("[truncated:", entries[1]["content"])

    # -- C68: queues in vault_status -----------------------------------------

    def test_status_queues_match_index_and_split_debt(self):
        _note(self.root, "wiki/loose-note.md", "Links out to [[sqlite]].")
        stale = 'verified:\n  - by: "human:alice"\n    at: "2026-01-01T00:00:00Z"\n    of: "sha256:0000"\n'
        _note(self.root, "wiki/human-stale.md", "See [[sqlite]].", extra=stale)
        agent_last = stale + '  - by: "agent:bot"\n    at: "2026-01-02T00:00:00Z"\n    of: "sha256:1111"\n'
        _note(self.root, "wiki/agent-stale.md", "See [[sqlite]].", extra=agent_last)
        status = self._ok("vault_status", {})
        queues = status["queues"]
        self.assertIn("raw/unprocessed-notes.txt", queues["unprocessed_raw"]["items"])
        self.assertIn("wiki/loose-note", queues["unfiled"]["items"])
        debt = queues["verification_debt"]
        self.assertIn("wiki/human-stale", debt["human_owed"]["items"])
        self.assertNotIn("wiki/human-stale", debt["agent_payable"]["items"])
        # A human sign-off later re-checked by an agent is the agent's to renew.
        self.assertIn("wiki/agent-stale", debt["agent_payable"]["items"])
        # One definition: index.md lists exactly what the queues list.
        index = (self.root / "index.md").read_text("utf-8")
        for cid in debt["human_owed"]["items"] + debt["agent_payable"]["items"]:
            self.assertIn(f"`{cid}`", index)
        self.assertIn("[[loose-note]]", index)

    def test_status_queues_are_capped_with_totals(self):
        for i in range(30):
            (self.root / "raw" / f"bulk-{i:02}.txt").write_text("x", "utf-8")
        from cadabby.cache import unprocessed_raw_paths

        queue = self._ok("vault_status", {})["queues"]["unprocessed_raw"]
        self.assertEqual(len(queue["items"]), 25)
        expected = unprocessed_raw_paths(self.server.cache.get_connection())
        self.assertEqual(queue["total"], len(expected), "the full total, not a lower bound (C68)")
        self.assertEqual(queue["items"], expected[:25])

    # -- C69: vault_graph ----------------------------------------------------

    def test_graph_neighborhood_carries_metadata_and_no_text(self):
        graph = self._ok("vault_graph", {"target": "sqlite"})
        self.assertEqual(graph["view"], "neighborhood")
        self.assertEqual(graph["note"]["cid"], "wiki/sqlite")
        self.assertTrue(graph["backlinks"])
        self.assertIn("trust_tier", graph["backlinks"][0])
        self.assertIn("title", graph["backlinks"][0])
        for listed in ("forward_links", "backlinks"):
            self.assertIn(f"{listed}_total", graph)
        dumped = json.dumps(graph)
        self.assertNotIn('"content"', dumped)
        self.assertNotIn('"body"', dumped)
        # No note text at all, not just no text-shaped keys (C69).
        body = (self.root / "wiki" / "sqlite.md").read_text("utf-8").split("---", 2)[2]
        sentence = next(line for line in body.splitlines() if len(line) > 40 and not line.startswith(("#", "[")))
        self.assertNotIn(sentence[:40], dumped)

    def test_graph_mocs_reports_overlap_ratio(self):
        for n in "abcde":
            _note(self.root, f"wiki/n-{n}.md", "Leaf.")
        _note(self.root, "wiki/one-moc.md", "[[n-a]] [[n-b]] [[n-c]] [[n-d]]", type_="moc")
        _note(self.root, "wiki/two-moc.md", "[[n-a]] [[n-b]] [[n-c]] [[n-e]]", type_="moc")
        graph = self._ok("vault_graph", {"view": "mocs"})
        pair = next(o for o in graph["overlaps"] if {o["a"], o["b"]} == {"wiki/one-moc", "wiki/two-moc"})
        self.assertEqual(pair["shared"], 3)
        self.assertEqual(pair["ratio"], 0.75)
        one = next(m for m in graph["mocs"] if m["cid"] == "wiki/one-moc")
        self.assertEqual(one["outgoing"], 4)

    def test_graph_refuses_bad_view_target_combinations(self):
        for args in ({"view": "mocs", "target": "sqlite"}, {}, {"target": "raw/karpathy-llm-wiki-gist.md"}, {"view": "tree"}):
            with self.subTest(args=args):
                self.assertEqual(self._call("vault_graph", args)["code"], "INVALID_ARGUMENT")
        self.assertEqual(self._call("vault_graph", {"target": "no-such-note"})["code"], "NOT_FOUND")

    # -- C70: retired arguments ---------------------------------------------

    def test_retired_arguments_name_their_replacement(self):
        err = self._call("vault_ground", {"cids": ["sqlite"]})
        self.assertEqual(err["code"], "INVALID_ARGUMENT")
        self.assertIn("targets", err["message"])
        err = self._call("vault_update_note", {"cid": "sqlite", "append_section": ["H", "B"]})
        self.assertEqual(err["code"], "INVALID_ARGUMENT")
        self.assertIn("edits", err["message"])
        tools = {t["name"]: t for t in self.server.handle_tools_list()["tools"]}
        self.assertNotIn("append_section", tools["vault_update_note"]["inputSchema"]["properties"])
        self.assertNotIn("cids", tools["vault_ground"]["inputSchema"]["properties"])


class TestMcpReviewTwoFixes(unittest.TestCase):
    """C75: graph on broken notes, JSON-RPC ids, argument ceilings, raw/ routing and caps."""

    setUp = TestMcpServer.setUp
    tearDown = TestMcpServer.tearDown

    def _call(self, name, args):
        res = self.server.handle_tools_call(name, args)
        return res, json.loads(res["content"][0]["text"])

    def test_graph_on_an_unparseable_note_is_unparseable_not_empty(self):
        (self.vault_root / "wiki" / "broken.md").write_text("---\ntitle: a\ntitle: b\n---\n# B\n", "utf-8")
        res, payload = self._call("vault_graph", {"target": "wiki/broken"})
        self.assertTrue(res["isError"])
        self.assertEqual(payload["error"]["code"], "FRONTMATTER_UNPARSEABLE")

    def test_null_and_malformed_ids_are_answered_not_dropped(self):
        for bad_id in (None, [1], {"a": 1}, True):
            with self.subTest(id=bad_id):
                resp = self.server.dispatch_request({"jsonrpc": "2.0", "id": bad_id, "method": "ping"})
                self.assertEqual(resp["error"]["code"], -32600)
        self.assertIsNone(self.server.dispatch_request({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(self.server.dispatch_request({"jsonrpc": "2.0", "id": "x", "method": "ping"})["result"], {})

    def test_a_retired_argument_sent_as_null_is_not_given(self):
        res, payload = self._call("vault_ground", {"targets": ["wiki/sqlite"], "cids": None})
        self.assertFalse(res.get("isError"), payload)

    def test_arguments_have_ceilings(self):
        cases = (
            ("vault_search", {"query": "sqlite", "limit": 10**30}),
            ("vault_graph", {"view": "mocs", "limit": 1001}),
            ("vault_ground", {"targets": ["wiki/sqlite"] * 51}),
            ("vault_ground", {"targets": ["wiki/sqlite"], "budget_tokens": 10**7}),
        )
        for name, args in cases:
            with self.subTest(name=name, args=list(args)):
                res, payload = self._call(name, args)
                self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")

    def test_a_huge_raw_file_is_read_only_up_to_the_cap(self):
        from unittest import mock

        from cadabby.adapters.disk_storage import DiskNoteStorage
        from cadabby.ops import RAW_GROUND_MAX_BYTES

        big = self.vault_root / "raw" / "dump.md"
        big.write_text("# Dump\n" + ("x" * 99 + "\n") * 30_000, "utf-8")  # ~3 MB
        seen = []
        real = DiskNoteStorage.read_source

        def spy(storage, rel_path, max_bytes=None):
            seen.append(max_bytes)
            return real(storage, rel_path, max_bytes)

        with mock.patch.object(DiskNoteStorage, "read_source", spy):
            res, payload = self._call("vault_ground", {"targets": ["raw/dump.md"]})
        self.assertFalse(res.get("isError"), payload)
        self.assertTrue(payload[0]["truncated"])
        self.assertLessEqual(len(payload[0]["content"].encode("utf-8")), RAW_GROUND_MAX_BYTES)
        self.assertTrue(seen and all(m is not None and m <= RAW_GROUND_MAX_BYTES + 1 for m in seen))

    def test_a_domain_named_raw_with_a_capital_holds_notes(self):
        folder = self.vault_root / "Raw"
        if folder.exists():  # `raw/` answers to `Raw` on a case-insensitive filesystem
            self.skipTest("case-insensitive filesystem")
        folder.mkdir()
        (folder / "thing.md").write_text(
            "---\ntype: concept\ntitle: Thing\ndescription: d\nstatus: active\n---\n# Thing\n", "utf-8"
        )
        res, payload = self._call("vault_ground", {"targets": ["Raw/thing"]})
        self.assertFalse(res.get("isError"), payload)
        self.assertEqual(payload[0]["cid"], "Raw/thing")


class TestStructuredWriteResults(unittest.TestCase):
    """C76: every write tool answers with fields an agent can act on, never a sentence (§5.1, §5.4)."""

    setUp = TestMcpServer.setUp
    tearDown = TestMcpServer.tearDown

    def _call(self, name, args):
        res = self.server.handle_tools_call(name, args)
        self.assertFalse(res.get("isError"), res)
        return json.loads(res["content"][0]["text"])

    def test_scaffold_returns_the_new_note_and_its_hash(self):
        out = self._call("vault_scaffold_note", {"title": "Struct Note", "type": "concept", "description": "d"})
        self.assertEqual(set(out), {"cid", "rel_path", "source_hash", "trust_tier", "warnings"})
        self.assertEqual(out["cid"], "wiki/struct-note")
        disk = (self.vault_root / out["rel_path"]).read_bytes()
        import hashlib

        self.assertEqual(out["source_hash"], hashlib.sha256(disk).hexdigest())
        self.assertEqual(out["warnings"], [])

    def test_update_hash_chains_into_the_next_expected_hash(self):
        first = self._call(
            "vault_update_note", {"cid": "wiki/sqlite", "patch_frontmatter": {"description": "first edit"}}
        )
        second = self._call(
            "vault_update_note",
            {"cid": "wiki/sqlite", "patch_frontmatter": {"description": "second edit"}, "expected_hash": first["source_hash"]},
        )
        self.assertNotEqual(first["source_hash"], second["source_hash"])
        stale = self.server.handle_tools_call(
            "vault_update_note",
            {"cid": "wiki/sqlite", "patch_frontmatter": {"description": "third"}, "expected_hash": first["source_hash"]},
        )
        self.assertEqual(json.loads(stale["content"][0]["text"])["error"]["code"], "VAULT_CONFLICT")

    def test_verify_reports_already_verified_as_a_field(self):
        first = self._call("vault_verify_note", {"cid": "wiki/flash-attention"})
        again = self._call("vault_verify_note", {"cid": "wiki/flash-attention"})
        self.assertEqual((first["already_verified"], again["already_verified"]), (False, True))
        self.assertTrue(first["of"].startswith("sha256:"))

    def test_a_failed_refresh_is_a_warning_on_a_write_that_succeeded(self):
        from unittest import mock

        with mock.patch("cadabby.indexer.sync_vault_index", side_effect=OSError("index.md is unwritable")):
            out = self._call("vault_scaffold_note", {"title": "Warned", "type": "concept", "description": "d"})
        self.assertTrue((self.vault_root / out["rel_path"]).is_file())
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("index.md is unwritable", out["warnings"][0])


class TestToolRegistry(unittest.TestCase):
    """C77: one registry; every declared tool has a handler and no reply can edit it."""

    def test_every_tool_has_exactly_one_handler(self):
        self.assertIsInstance(TOOLS, tuple)
        self.assertEqual({t["name"] for t in TOOLS}, set(McpServer._TOOL_HANDLERS))
        self.assertTrue(all(t["inputSchema"]["additionalProperties"] is False for t in TOOLS))

    def test_a_tools_list_reply_is_a_copy(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with McpServer(Vault(copy_demo_vault(Path(tmp.name) / "v"))) as server:
            reply = server.handle_tools_list()
            reply["tools"][0]["inputSchema"]["properties"]["injected"] = {"type": "string"}
            reply["tools"].append({"name": "vault_ninth"})
            again = server.handle_tools_list()
        self.assertEqual(len(again["tools"]), len(TOOLS))
        self.assertNotIn("injected", again["tools"][0]["inputSchema"]["properties"])


class TestUncoveredPaths(unittest.TestCase):
    """T5/T7: criteria observables and user-reachable paths that no test exercised."""

    setUp = TestMcpServer.setUp
    tearDown = TestMcpServer.tearDown

    def _call(self, name, args):
        res = self.server.handle_tools_call(name, args)
        return res, json.loads(res["content"][0]["text"])

    def test_human_refusal_by_either_spelling_leaves_the_bytes(self):
        """C12: refused as HUMAN_ATTESTATION_REFUSED, and the note is untouched."""
        note = self.vault_root / "wiki" / "flash-attention.md"
        before = note.read_bytes()
        for key in ("actor", "by"):
            with self.subTest(key=key):
                res, payload = self._call("vault_verify_note", {"cid": "wiki/flash-attention", key: "human:mallory"})
                self.assertEqual(payload["error"]["code"], "HUMAN_ATTESTATION_REFUSED")
                self.assertEqual(note.read_bytes(), before)

    def test_update_never_rewrites_an_unparseable_note(self):
        """C3: a flow-style note is refused and left byte for byte."""
        note = self.vault_root / "wiki" / "flow.md"
        note.write_text("---\ntype: concept\ntitle: Flow\ntags: [a, b]\n---\n# Flow\n", "utf-8")
        before = note.read_bytes()
        res, payload = self._call("vault_update_note", {"cid": "wiki/flow", "patch_frontmatter": {"status": "active"}})
        self.assertEqual(payload["error"]["code"], "FRONTMATTER_UNPARSEABLE")
        self.assertEqual(note.read_bytes(), before)

    def test_search_type_and_status_filters(self):
        self.server.cache.scan()
        _, by_status = self._call("vault_search", {"status": "deprecated"})
        self.assertEqual({h["cid"] for h in by_status}, {"wiki/sqlite-vs-duckdb"})
        _, by_type = self._call("vault_search", {"type": "moc"})
        self.assertTrue(by_type)
        self.assertTrue(all(h["type"] == "moc" for h in by_type))

    def test_malformed_edits_are_invalid_argument(self):
        for edits in ([{"op": "explode"}], [{"op": "replace_text", "old": "x"}], ["not an object"]):
            with self.subTest(edits=edits):
                _, payload = self._call("vault_update_note", {"cid": "wiki/sqlite", "edits": edits})
                self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")

    def test_graph_view_and_target_validation(self):
        for args in ({"view": "sideways", "target": "sqlite"}, {"view": "mocs", "target": "sqlite"}, {"view": "neighborhood"}):
            with self.subTest(args=args):
                _, payload = self._call("vault_graph", args)
                self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")

    def test_an_unexpected_handler_failure_is_internal_error(self):
        from unittest import mock

        with mock.patch.object(McpServer, "handle_initialize", side_effect=RuntimeError("boom")):
            resp = self.server.dispatch_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(resp["error"]["code"], -32603)


if __name__ == "__main__":
    unittest.main()


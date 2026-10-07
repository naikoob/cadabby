"""Unit tests for Model Context Protocol (MCP) server implementation."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from cadabby.mcp import McpServer, run_mcp_server
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
            "vault_scaffold_note",
            "vault_update_note",
            "vault_verify_note",
            "vault_lint",
        ]
        self.assertEqual(len(tools_res["tools"]), 7)
        self.assertEqual(set(tool_names), set(expected))

    def test_update_note_schema_exposes_edits(self):
        tools = {t["name"]: t for t in self.server.handle_tools_list()["tools"]}
        self.assertEqual(len(tools), 7)
        edits = tools["vault_update_note"]["inputSchema"]["properties"]["edits"]
        self.assertEqual(edits["type"], "array")
        self.assertEqual(
            set(edits["items"]["properties"]["op"]["enum"]),
            {"replace_text", "replace_section", "append_section"},
        )
        # Over the wire the batch is all-or-nothing: a bad third edit leaves the file alone.
        target = self.vault_root / "wiki" / "SQLite.md"
        before = target.read_bytes()
        res = self.server.handle_tools_call(
            "vault_update_note",
            {
                "cid": "wiki/SQLite",
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
        res_ground = self.server.handle_tools_call("vault_ground", {"cids": ["SQLite", "[[Epistemic-Trust-Tiers]]"]})
        self.assertFalse(res_ground["isError"])
        data_ground = json.loads(res_ground["content"][0]["text"])
        self.assertEqual(len(data_ground), 2)
        grounded_cids = [g["cid"] for g in data_ground]
        self.assertIn("wiki/SQLite", grounded_cids)
        self.assertIn("wiki/Epistemic-Trust-Tiers", grounded_cids)

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
        self.assertTrue(any(h["cid"] == "wiki/Quantum-Computing" for h in search_hits))

        # Immediate ground must retrieve the newly scaffolded note
        res_immediate_ground = self.server.handle_tools_call("vault_ground", {"cids": ["wiki/Quantum-Computing"]})
        self.assertFalse(res_immediate_ground["isError"])
        ground_hits = json.loads(res_immediate_ground["content"][0]["text"])
        self.assertEqual(len(ground_hits), 1)
        self.assertEqual(ground_hits[0]["cid"], "wiki/Quantum-Computing")

        # Update note
        res_update = self.server.handle_tools_call(
            "vault_update_note",
            {
                "cid": "wiki/Quantum-Computing",
                "append_section": ["Algorithms", "Shor's algorithm and Grover's algorithm."],
            },
        )
        self.assertFalse(res_update["isError"])

        # Verify: should stamp agent-smith and bind hash
        res_verify = self.server.handle_tools_call(
            "vault_verify_note",
            {"cid": "wiki/Quantum-Computing", "method": "automated-check"},
        )
        self.assertFalse(res_verify["isError"])
        self.assertIn("agent:agent-smith", res_verify["content"][0]["text"])

        # Status
        res_status = self.server.handle_tools_call("vault_status", {})
        self.assertFalse(res_status["isError"])
        status_data = json.loads(res_status["content"][0]["text"])
        self.assertGreater(status_data["total_notes"], 0)

        # Lint
        res_lint = self.server.handle_tools_call("vault_lint", {})
        self.assertFalse(res_lint["isError"])

    def test_persistent_cache_reuse_and_context_manager(self):
        # Cache connection is lazily opened or persistent
        conn_before = self.server.cache.get_connection()
        self.assertIsNotNone(conn_before)

        # Invocations of search, status, and lint should reuse this exact connection
        self.server.handle_tools_call("vault_search", {"query": "SQLite"})
        self.assertIs(self.server.cache.get_connection(), conn_before)

        self.server.handle_tools_call("vault_status", {})
        self.assertIs(self.server.cache.get_connection(), conn_before)

        self.server.handle_tools_call("vault_lint", {})
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
        # Missing tool name and missing required arguments surface as tool errors, not crashes
        for resp in responses[:3]:
            self.assertTrue(resp["result"]["isError"])
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


if __name__ == "__main__":
    unittest.main()

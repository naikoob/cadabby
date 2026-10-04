"""Unit tests for Model Context Protocol (MCP) server implementation."""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from cadabby.mcp import McpServer, run_mcp_server
from cadabby.vault import Vault


class TestMcpServer(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.vault_root = Path(self.tmp_dir.name) / "demo-vault"
        demo_src = Path(__file__).resolve().parent.parent / "examples" / "demo-vault"
        shutil.copytree(demo_src, self.vault_root, ignore=shutil.ignore_patterns(".cadabby", "*.pyc"))
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

    def test_tools_call_search_and_ground(self):
        self.server.handle_initialize({"clientInfo": {"name": "test-agent"}})

        # Search
        res_search = self.server.handle_tools_call("vault_search", {"query": "SQLite"})
        self.assertFalse(res_search["isError"])
        data_search = json.loads(res_search["content"][0]["text"])
        self.assertGreater(len(data_search), 0)

        # Ground
        res_ground = self.server.handle_tools_call("vault_ground", {"cids": ["wiki/entities/SQLite"]})
        self.assertFalse(res_ground["isError"])
        data_ground = json.loads(res_ground["content"][0]["text"])
        self.assertEqual(len(data_ground), 1)
        self.assertEqual(data_ground[0]["cid"], "wiki/entities/SQLite")

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

        # Update note
        res_update = self.server.handle_tools_call(
            "vault_update_note",
            {
                "cid": "wiki/concepts/Quantum-Computing",
                "append_section": ["Algorithms", "Shor's algorithm and Grover's algorithm."],
            },
        )
        self.assertFalse(res_update["isError"])

        # Verify: should stamp agent-smith and bind hash
        res_verify = self.server.handle_tools_call(
            "vault_verify_note",
            {"cid": "wiki/concepts/Quantum-Computing", "method": "automated-check"},
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

        input_chunks = []
        for r in (req1, req2, req3):
            r_bytes = r.encode("utf-8")
            hdr = f"Content-Length: {len(r_bytes)}\r\n\r\n".encode("ascii")
            input_chunks.append(hdr + r_bytes)

        stdin_stream = io.BytesIO(b"".join(input_chunks))
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

        # Parse responses from stdout buffer
        stdout_bytes = stdout_stream.getvalue()
        self.assertGreater(len(stdout_bytes), 0)

        # Parse each Content-Length framed response
        responses = []
        idx = 0
        while idx < len(stdout_bytes):
            header_end = stdout_bytes.find(b"\r\n\r\n", idx)
            self.assertNotEqual(header_end, -1)
            hdr_str = stdout_bytes[idx:header_end].decode("ascii")
            self.assertTrue(hdr_str.lower().startswith("content-length:"))
            length = int(hdr_str.split(":", 1)[1].strip())
            body_start = header_end + 4
            body_bytes = stdout_bytes[body_start : body_start + length]
            self.assertEqual(len(body_bytes), length)
            resp_obj = json.loads(body_bytes.decode("utf-8"))
            responses.append(resp_obj)
            idx = body_start + length

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

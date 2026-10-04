"""Unit tests for Model Context Protocol (MCP) server implementation."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from cadabby.mcp import McpServer
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
            "vault_scaffold_note",
            "vault_update_note",
            "vault_verify_note",
            "vault_sync_indexes",
            "vault_lint",
            "vault_status",
            "vault_triage",
            "vault_archive",
        ]
        for exp in expected:
            self.assertIn(exp, tool_names)

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

    def test_tools_call_scaffold_verify_and_lint(self):
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

    def test_vault_triage_and_archive(self):
        self.server.handle_initialize({"clientInfo": {"name": "test-agent"}})

        # Triage an unprocessed raw source
        res_triage = self.server.handle_tools_call(
            "vault_triage",
            {"raw_path": "raw/unprocessed-notes.txt"},
        )
        self.assertFalse(res_triage["isError"])
        data_triage = json.loads(res_triage["content"][0]["text"])
        self.assertEqual(data_triage["raw_path"], "raw/unprocessed-notes.txt")
        self.assertIn("concept", data_triage["suggested_types"])

        # Archive a note
        res_archive = self.server.handle_tools_call(
            "vault_archive",
            {"cid": "wiki/comparisons/SQLite-vs-DuckDB", "reason": "Superseded by internal benchmarks"},
        )
        self.assertFalse(res_archive["isError"])


if __name__ == "__main__":
    unittest.main()

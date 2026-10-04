"""Unit tests for cadabby.installer (§7.6, §10 AC 18)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cadabby.installer import (
    CADABBY_MCP_SERVER_ENTRY,
    install_antigravity,
    install_claude,
    run_install,
    uninstall_antigravity,
    uninstall_claude,
)


class TestInstaller(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_claude_install_and_idempotency_ac18(self):
        claude_cfg = self.root / ".claude.json"
        # Seed an existing config with user-defined keys
        initial_cfg = {
            "mcpServers": {
                "weather": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-weather"]}
            },
            "userPreference": "dark_mode",
        }
        claude_cfg.write_text(json.dumps(initial_cfg, indent=2) + "\n", "utf-8")

        # 1. First install
        ok, msg = install_claude(dest_path=claude_cfg)
        self.assertTrue(ok)
        self.assertIn("Registered Cadabby MCP server", msg)

        # Verify merge preserved user keys
        data = json.loads(claude_cfg.read_text("utf-8"))
        self.assertEqual(data["userPreference"], "dark_mode")
        self.assertIn("weather", data["mcpServers"])
        self.assertEqual(data["mcpServers"]["cadabby"], CADABBY_MCP_SERVER_ENTRY)

        first_content = claude_cfg.read_text("utf-8")

        # 2. Second install: must be a no-op changing 0 bytes
        ok, msg = install_claude(dest_path=claude_cfg)
        self.assertTrue(ok)
        self.assertIn("unchanged", msg)
        second_content = claude_cfg.read_text("utf-8")
        self.assertEqual(first_content, second_content)

        # 3. Uninstall: must restore pre-install config exactly
        ok, msg = uninstall_claude(dest_path=claude_cfg)
        self.assertTrue(ok)
        restored_data = json.loads(claude_cfg.read_text("utf-8"))
        self.assertEqual(restored_data, initial_cfg)
        self.assertNotIn("cadabby", restored_data.get("mcpServers", {}))

    def test_claude_install_dry_run_writes_nothing(self):
        claude_cfg = self.root / "dry-run.json"
        claude_cfg.write_text('{"mcpServers": {}}\n', "utf-8")
        before_bytes = claude_cfg.read_bytes()

        ok, msg = install_claude(dest_path=claude_cfg, dry_run=True)
        self.assertTrue(ok)
        self.assertIn("[dry-run]", msg)
        self.assertEqual(claude_cfg.read_bytes(), before_bytes)

    def test_claude_install_conflict_without_force(self):
        claude_cfg = self.root / "conflict.json"
        conflict_cfg = {
            "mcpServers": {
                "cadabby": {"command": "other-binary", "args": ["custom"]}
            }
        }
        claude_cfg.write_text(json.dumps(conflict_cfg, indent=2) + "\n", "utf-8")

        # Refuses to overwrite without force
        ok, msg = install_claude(dest_path=claude_cfg, force=False)
        self.assertFalse(ok)
        self.assertIn("Conflict", msg)
        data = json.loads(claude_cfg.read_text("utf-8"))
        self.assertEqual(data["mcpServers"]["cadabby"]["command"], "other-binary")

        # Overwrites with force
        ok, msg = install_claude(dest_path=claude_cfg, force=True)
        self.assertTrue(ok)
        data = json.loads(claude_cfg.read_text("utf-8"))
        self.assertEqual(data["mcpServers"]["cadabby"], CADABBY_MCP_SERVER_ENTRY)

    def test_antigravity_install_and_uninstall(self):
        plugin_dest = self.root / "antigravity" / "plugins" / "cadabby"

        # 1. Install
        ok, msg = install_antigravity(dest_path=plugin_dest)
        self.assertTrue(ok)
        self.assertTrue(plugin_dest.exists())
        self.assertTrue((plugin_dest / "plugin.json").exists())

        # 2. Second install: idempotent
        ok, msg = install_antigravity(dest_path=plugin_dest)
        self.assertTrue(ok)
        self.assertIn("unchanged", msg)

        # 3. Uninstall
        ok, msg = uninstall_antigravity(dest_path=plugin_dest)
        self.assertTrue(ok)
        self.assertFalse(plugin_dest.exists())

    def test_run_install_all_and_uninstall(self):
        claude_cfg = self.root / "run_all.json"
        claude_cfg.write_text('{"existing": true}\n', "utf-8")

        # Install --claude
        ret = run_install(claude=True, dest_path=claude_cfg)
        self.assertEqual(ret, 0)
        data = json.loads(claude_cfg.read_text("utf-8"))
        self.assertIn("cadabby", data["mcpServers"])

        # Uninstall --claude
        ret = run_install(claude=True, uninstall=True, dest_path=claude_cfg)
        self.assertEqual(ret, 0)
        data = json.loads(claude_cfg.read_text("utf-8"))
        self.assertNotIn("cadabby", data.get("mcpServers", {}))
        self.assertTrue(data.get("existing"))


if __name__ == "__main__":
    unittest.main()

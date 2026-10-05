"""Unit tests for cadabby CLI subcommands."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from cadabby.cli import (
    cmd_ground,
    cmd_init,
    cmd_install,
    cmd_lint,
    cmd_scaffold,
    cmd_search,
    cmd_status,
    cmd_sync,
    cmd_verify,
)


class DummyArgs:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_init_scaffolding(self):
        vault_path = self.dir / "test-vault"
        args = DummyArgs(vault=str(vault_path), name="test-vault", obsidian=True)
        ret = cmd_init(args)
        self.assertEqual(ret, 0)

        # Check required files
        self.assertTrue((vault_path / ".cadabby.json").exists())
        self.assertTrue((vault_path / ".mcp.json").exists())
        self.assertTrue((vault_path / "AGENTS.md").exists())
        self.assertTrue((vault_path / "CLAUDE.md").exists())
        self.assertTrue((vault_path / "index.md").exists())
        self.assertTrue((vault_path / "log.md").exists())
        self.assertTrue((vault_path / "raw").is_dir())
        self.assertTrue((vault_path / "wiki" / "concepts").is_dir())
        self.assertTrue((vault_path / ".agents" / "skills" / "librarian" / "SKILL.md").exists())
        self.assertTrue((vault_path / ".agents" / "skills" / "technician" / "SKILL.md").exists())
        self.assertTrue((vault_path / ".agents" / "plugins" / "cadabby" / "plugin.json").exists())
        self.assertTrue((vault_path / ".agents" / "plugins" / "cadabby" / "mcp_config.json").exists())
        self.assertTrue((vault_path / ".agents" / "plugins" / "cadabby" / "skills" / "cadabby-wiki" / "SKILL.md").exists())
        self.assertTrue((vault_path / ".agents" / "plugins" / "cadabby" / "agents" / "librarian" / "agent.md").exists())
        self.assertTrue((vault_path / ".agents" / "plugins" / "cadabby" / "agents" / "technician" / "agent.md").exists())

        # Verify mcp_config.json specifies the vault-scoped cadabby MCP server
        mcp_cfg = json.loads((vault_path / ".agents" / "plugins" / "cadabby" / "mcp_config.json").read_text("utf-8"))
        self.assertIn("cadabby", mcp_cfg.get("mcpServers", {}))
        self.assertEqual(
            mcp_cfg["mcpServers"]["cadabby"]["args"],
            ["-m", "cadabby", "mcp", "--vault", str(vault_path.resolve())],
        )

        # Verify .mcp.json specifies the vault-scoped cadabby MCP server for Claude Code
        claude_mcp = json.loads((vault_path / ".mcp.json").read_text("utf-8"))
        self.assertIn("cadabby", claude_mcp.get("mcpServers", {}))
        self.assertEqual(
            claude_mcp["mcpServers"]["cadabby"]["args"],
            ["-m", "cadabby", "mcp", "--vault", str(vault_path.resolve())],
        )

        self.assertTrue((vault_path / ".claude" / "commands" / "ingest.md").exists())
        self.assertTrue((vault_path / ".obsidian" / "app.json").exists())

    def test_cli_lifecycle_on_demo_vault(self):
        demo_src = Path(__file__).resolve().parent.parent / "examples" / "demo-vault"
        vault_root = self.dir / "demo-vault"
        shutil.copytree(demo_src, vault_root, ignore=shutil.ignore_patterns(".cadabby", "*.pyc"))

        # 1. Sync
        args_sync = DummyArgs(vault=str(vault_root), force=False)
        self.assertEqual(cmd_sync(args_sync), 0)

        # 2. Status (JSON)
        args_status = DummyArgs(vault=str(vault_root), json=True)
        self.assertEqual(cmd_status(args_status), 0)

        # 3. Search
        args_search = DummyArgs(
            vault=str(vault_root),
            query="SQLite",
            type=None,
            status=None,
            trust=None,
            tag=None,
            limit=10,
            json=True,
        )
        self.assertEqual(cmd_search(args_search), 0)

        # 4. Ground
        args_ground = DummyArgs(
            vault=str(vault_root),
            cids=["wiki/entities/SQLite"],
            budget_tokens=None,
            json=True,
        )
        self.assertEqual(cmd_ground(args_ground), 0)

        # 5. Scaffold
        args_scaffold = DummyArgs(
            vault=str(vault_root),
            title="Graph Neural Networks",
            type="concept",
            desc="Introduction to message passing and GNN layers",
            tags="gnn,ml",
            sources=None,
            actor="agent:cli-test",
        )
        self.assertEqual(cmd_scaffold(args_scaffold), 0)
        self.assertTrue((vault_root / "wiki" / "concepts" / "Graph-Neural-Networks.md").exists())

        # 6. Verify
        args_verify = DummyArgs(
            vault=str(vault_root),
            cid="wiki/concepts/Graph-Neural-Networks",
            method="automated-check",
            human=False,
            agent="agent:cli-test",
        )
        self.assertEqual(cmd_verify(args_verify), 0)

        # 7. Lint
        args_lint = DummyArgs(vault=str(vault_root), json=True)
        self.assertEqual(cmd_lint(args_lint), 0)

    def test_sync_triggers_log_rotation_on_threshold(self):
        vault_root = self.dir / "rotate-vault"
        args_init = DummyArgs(vault=str(vault_root), name="rotate-vault", obsidian=False)
        cmd_init(args_init)

        # Pad log.md to exceed default 256KB threshold
        log_path = vault_root / "log.md"
        big_entry = "- [2026-10-04T12:00:00Z] Test log entry\n" * 8000  # ~320 KB
        log_path.write_text(big_entry, "utf-8")
        self.assertGreater(log_path.stat().st_size, 262144)

        # Run sync
        args_sync = DummyArgs(vault=str(vault_root), force=False)
        ret = cmd_sync(args_sync)
        self.assertEqual(ret, 0)

        # Verify rotated file exists in log/
        rotated_files = list((vault_root / "log").glob("*.md"))
        self.assertEqual(len(rotated_files), 1)
        self.assertGreater(rotated_files[0].stat().st_size, 262144)

        # Verify active log.md has been reset
        self.assertLess(log_path.stat().st_size, 500)

    def test_cli_fails_fast_outside_vault(self):
        non_vault_dir = self.dir / "empty-dir"
        non_vault_dir.mkdir(parents=True, exist_ok=True)
        args_status = DummyArgs(vault=str(non_vault_dir), json=False)
        with self.assertRaises(FileNotFoundError) as ctx:
            cmd_status(args_status)
        self.assertIn("No Cadabby vault found", str(ctx.exception))

    def test_explicit_vault_does_not_resolve_to_ancestor(self):
        # An explicit --vault names the root exactly; a typo'd child must not
        # silently fall back to the enclosing vault.
        vault_root = self.dir / "ancestor-vault"
        args_init = DummyArgs(vault=str(vault_root), name="ancestor-vault", obsidian=False)
        cmd_init(args_init)

        typo_child = vault_root / "typo-child"
        args_status = DummyArgs(vault=str(typo_child), json=False)
        with self.assertRaises(FileNotFoundError) as ctx:
            cmd_status(args_status)
        self.assertIn("No Cadabby vault found", str(ctx.exception))
        self.assertFalse((typo_child / ".cadabby").exists())

    def test_verify_mutually_exclusive_flags(self):
        vault_root = self.dir / "verify-excl-vault"
        args_init = DummyArgs(vault=str(vault_root), name="verify-excl", obsidian=False)
        cmd_init(args_init)

        args_verify = DummyArgs(
            vault=str(vault_root),
            cid="wiki/concepts/Test",
            human=True,
            agent="test-agent",
            method=None,
        )
        self.assertEqual(cmd_verify(args_verify), 1)

    def test_cmd_install_workspace_defaults(self):
        vault_root = self.dir / "ws-install-vault"
        args_init = DummyArgs(vault=str(vault_root), name="ws-install-vault", obsidian=False)
        cmd_init(args_init)

        # Re-install with all harnesses, no explicit path, no is_global flag -> workspace mode
        args_inst = DummyArgs(
            vault=str(vault_root),
            path=None,
            is_global=False,
            antigravity=False,
            claude=False,
            all=True,
            uninstall=False,
            dry_run=False,
            force=True,
        )
        self.assertEqual(cmd_install(args_inst), 0)

        claude_mcp = vault_root / ".mcp.json"
        self.assertTrue(claude_mcp.exists())
        claude_data = json.loads(claude_mcp.read_text("utf-8"))
        self.assertEqual(
            claude_data["mcpServers"]["cadabby"]["args"],
            ["-m", "cadabby", "mcp", "--vault", str(vault_root.resolve())],
        )

        ag_mcp = vault_root / ".agents" / "plugins" / "cadabby" / "mcp_config.json"
        self.assertTrue(ag_mcp.exists())
        ag_data = json.loads(ag_mcp.read_text("utf-8"))
        self.assertEqual(
            ag_data["mcpServers"]["cadabby"]["args"],
            ["-m", "cadabby", "mcp", "--vault", str(vault_root.resolve())],
        )

    def test_cmd_install_with_explicit_path(self):
        vault_root = self.dir / "install-vault"
        args_init = DummyArgs(vault=str(vault_root), name="install-vault", obsidian=False)
        cmd_init(args_init)

        claude_cfg = self.dir / ".claude.json"
        ag_dest = self.dir / "antigravity-dest"
        args_inst = DummyArgs(
            vault=str(vault_root),
            path=str(claude_cfg),
            is_global=False,
            antigravity=False,
            claude=True,
            all=False,
            uninstall=False,
            dry_run=False,
            force=False,
        )
        self.assertEqual(cmd_install(args_inst), 0)

        claude_data = json.loads(claude_cfg.read_text("utf-8"))
        self.assertEqual(
            claude_data["mcpServers"]["cadabby"]["args"],
            ["-m", "cadabby", "mcp", "--vault", str(vault_root.resolve())],
        )

        # Also test antigravity install with explicit vault
        args_ag = DummyArgs(
            vault=str(vault_root),
            path=str(ag_dest),
            is_global=False,
            antigravity=True,
            claude=False,
            all=False,
            uninstall=False,
            dry_run=False,
            force=False,
        )
        self.assertEqual(cmd_install(args_ag), 0)
        ag_mcp = json.loads((ag_dest / "mcp_config.json").read_text("utf-8"))
        self.assertEqual(
            ag_mcp["mcpServers"]["cadabby"]["args"],
            ["-m", "cadabby", "mcp", "--vault", str(vault_root.resolve())],
        )

    def test_cmd_install_outside_vault_requires_global(self):
        # When outside a vault, no --path and no --global should raise FileNotFoundError
        import os
        old_cwd = os.getcwd()
        empty_dir = self.dir / "empty-dir"
        empty_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chdir(empty_dir)
            args_fail = DummyArgs(
                vault=None,
                path=None,
                is_global=False,
                antigravity=False,
                claude=True,
                all=False,
                uninstall=False,
                dry_run=False,
                force=False,
            )
            with self.assertRaises(FileNotFoundError) as ctx:
                cmd_install(args_fail)
            self.assertIn("No Cadabby vault found", str(ctx.exception))
            self.assertIn("--global", str(ctx.exception))
        finally:
            os.chdir(old_cwd)

    def test_cmd_install_global_flag(self):
        claude_cfg = self.dir / "global-claude.json"
        args_inst = DummyArgs(
            vault=None,
            path=str(claude_cfg),
            is_global=True,
            antigravity=False,
            claude=True,
            all=False,
            uninstall=False,
            dry_run=False,
            force=False,
        )
        self.assertEqual(cmd_install(args_inst), 0)
        claude_data = json.loads(claude_cfg.read_text("utf-8"))
        self.assertEqual(
            claude_data["mcpServers"]["cadabby"]["args"],
            ["-m", "cadabby", "mcp"],
        )


if __name__ == "__main__":
    unittest.main()



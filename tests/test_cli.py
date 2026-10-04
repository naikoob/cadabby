"""Unit tests for cadabby CLI subcommands."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from cadabby.cli import (
    cmd_ground,
    cmd_init,
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


if __name__ == "__main__":
    unittest.main()

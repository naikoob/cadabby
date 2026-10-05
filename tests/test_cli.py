"""Unit tests for cadabby CLI subcommands."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
import unittest.mock
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
from cadabby.installer import mcp_launch_argv


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
        self.assertTrue((vault_path / "wiki").is_dir())
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
            [*mcp_launch_argv()[1:], "--vault", str(vault_path.resolve())],
        )

        # Verify .mcp.json specifies the vault-scoped cadabby MCP server for Claude Code
        claude_mcp = json.loads((vault_path / ".mcp.json").read_text("utf-8"))
        self.assertIn("cadabby", claude_mcp.get("mcpServers", {}))
        self.assertEqual(
            claude_mcp["mcpServers"]["cadabby"]["args"],
            [*mcp_launch_argv()[1:], "--vault", str(vault_path.resolve())],
        )

        self.assertTrue((vault_path / ".claude" / "commands" / "ingest.md").exists())
        self.assertTrue((vault_path / ".obsidian" / "app.json").exists())
        obs_cfg = json.loads((vault_path / ".obsidian" / "app.json").read_text("utf-8"))
        self.assertEqual(obs_cfg.get("attachmentFolderPath"), "raw/attachments")
        self.assertTrue((vault_path / "raw" / "attachments").is_dir())

    def test_init_without_force_does_not_rewrite_existing_mcp_configs(self):
        # The generated harness configs are subject to --force like every other
        # template: re-running init must not silently rebind a hand-edited one.
        vault_path = self.dir / "preserve-vault"
        self.assertEqual(cmd_init(DummyArgs(vault=str(vault_path), name="v1", obsidian=False)), 0)

        claude_mcp = vault_path / ".mcp.json"
        ag_mcp = vault_path / ".agents" / "plugins" / "cadabby" / "mcp_config.json"
        custom = json.dumps({"mcpServers": {"cadabby": {"command": "my-wrapper", "args": []}}}) + "\n"
        claude_mcp.write_text(custom, "utf-8")
        ag_mcp.write_text(custom, "utf-8")

        # Re-init without --force leaves both untouched
        self.assertEqual(cmd_init(DummyArgs(vault=str(vault_path), name="v1", obsidian=False)), 0)
        self.assertEqual(claude_mcp.read_text("utf-8"), custom)
        self.assertEqual(ag_mcp.read_text("utf-8"), custom)

        # With --force both are rewritten and re-bound to this vault
        self.assertEqual(
            cmd_init(DummyArgs(vault=str(vault_path), name="v1", obsidian=False, force=True)), 0
        )
        expected = [*mcp_launch_argv()[1:], "--vault", str(vault_path.resolve())]
        for cfg in (claude_mcp, ag_mcp):
            data = json.loads(cfg.read_text("utf-8"))
            self.assertEqual(data["mcpServers"]["cadabby"]["args"], expected)

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
        self.assertTrue((vault_root / "wiki" / "Graph-Neural-Networks.md").exists())

        # 6. Verify
        args_verify = DummyArgs(
            vault=str(vault_root),
            cid="wiki/Graph-Neural-Networks",
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
            cid="wiki/Test",
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
            [*mcp_launch_argv()[1:], "--vault", str(vault_root.resolve())],
        )

        ag_mcp = vault_root / ".agents" / "plugins" / "cadabby" / "mcp_config.json"
        self.assertTrue(ag_mcp.exists())
        ag_data = json.loads(ag_mcp.read_text("utf-8"))
        self.assertEqual(
            ag_data["mcpServers"]["cadabby"]["args"],
            [*mcp_launch_argv()[1:], "--vault", str(vault_root.resolve())],
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
            [*mcp_launch_argv()[1:], "--vault", str(vault_root.resolve())],
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
            [*mcp_launch_argv()[1:], "--vault", str(vault_root.resolve())],
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
            mcp_launch_argv()[1:],
        )

    def test_init_with_positional_target_path(self):
        vault_path = self.dir / "positional-vault"
        args = DummyArgs(target_path=vault_path, vault=None, name=None, obsidian=False)
        ret = cmd_init(args)
        self.assertEqual(ret, 0)
        self.assertTrue((vault_path / ".cadabby.json").exists())

    def test_cli_parser_scaffold_description_alias(self):
        from cadabby.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["scaffold", "My-Note", "--type", "concept", "--description", "Desc text", "--vault", str(self.dir)])
        self.assertEqual(args.desc, "Desc text")

    def test_cli_parser_init_positional_target(self):
        from cadabby.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["init", "/tmp/my-vault"])
        self.assertEqual(args.target_path, Path("/tmp/my-vault"))


class TestInstallIsTheRefreshVerb(unittest.TestCase):
    """§7.4/§7.6. `install` repairs what `init` scaffolded and will not revisit.

    Two things go stale in a working vault: the absolute interpreter path baked
    into the MCP config, and the engine-owned shims copied at scaffold time.
    `init` declines to touch either, and `init --force` repairs them only by
    resetting the user's documents as well. These pin the narrow path.
    """

    USER_OWNED = ("AGENTS.md", "CLAUDE.md", "GEMINI.md", "STYLE.md", "index.md")

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp_dir.name)
        self.vault = self.dir / "vault"
        cmd_init(DummyArgs(vault=str(self.vault), name="vault", obsidian=False))

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _install(self, **overrides):
        args = {
            "vault": str(self.vault), "path": None, "is_global": False,
            "antigravity": False, "claude": False, "all": True,
            "uninstall": False, "dry_run": False, "force": False,
        }
        args.update(overrides)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = cmd_install(DummyArgs(**args))
        return code, out.getvalue()

    def _mcp(self):
        return json.loads((self.vault / ".mcp.json").read_text("utf-8"))

    def _mark_user_files(self):
        for name in self.USER_OWNED:
            path = self.vault / name
            path.write_text(path.read_text("utf-8") + "\nZZUSERZZ\n")
        cfg = self.vault / ".cadabby.json"
        data = json.loads(cfg.read_text("utf-8"))
        data["domains"] = {"recipes": {"description": "ZZUSERZZ"}}
        cfg.write_text(json.dumps(data, indent=2))

    def _assert_user_files_intact(self):
        for name in self.USER_OWNED:
            self.assertIn("ZZUSERZZ", (self.vault / name).read_text("utf-8"), f"{name} was overwritten")
        data = json.loads((self.vault / ".cadabby.json").read_text("utf-8"))
        self.assertEqual(data.get("domains", {}).get("recipes", {}).get("description"), "ZZUSERZZ")

    def test_install_on_a_fresh_vault_is_a_no_op(self):
        """`init` already registered both harnesses, so there is nothing to do."""
        code, out = self._install()
        self.assertEqual(code, 0)
        self.assertIn("unchanged", out)

    def test_rebinds_its_own_entry_after_the_interpreter_moves(self):
        """The case install exists for: a venv rebuild leaves a dead command path."""
        data = self._mcp()
        data["mcpServers"]["cadabby"]["command"] = "/gone/venv/bin/python3"
        data["mcpServers"]["other"] = {"command": "keep-me"}
        (self.vault / ".mcp.json").write_text(json.dumps(data, indent=2))

        code, out = self._install()  # deliberately no force=True
        self.assertEqual(code, 0, out)
        after = self._mcp()
        self.assertNotEqual(after["mcpServers"]["cadabby"]["command"], "/gone/venv/bin/python3")
        self.assertEqual(after["mcpServers"]["other"], {"command": "keep-me"}, "merge must preserve foreign keys")

    def test_an_entry_for_another_vault_is_still_a_conflict(self):
        data = self._mcp()
        data["mcpServers"]["cadabby"]["args"] = ["-m", "cadabby", "mcp", "--vault", "/somewhere/else"]
        (self.vault / ".mcp.json").write_text(json.dumps(data, indent=2))

        code, _ = self._install()
        self.assertEqual(code, 1)
        self.assertEqual(self._mcp()["mcpServers"]["cadabby"]["args"][-1], "/somewhere/else")

    def test_a_foreign_server_under_our_key_is_still_a_conflict(self):
        """Someone else's server registered as `cadabby` is not ours to rebind."""
        data = self._mcp()
        data["mcpServers"]["cadabby"] = {"command": "node", "args": ["their-server.js"]}
        (self.vault / ".mcp.json").write_text(json.dumps(data, indent=2))

        code, _ = self._install()
        self.assertEqual(code, 1)
        self.assertEqual(self._mcp()["mcpServers"]["cadabby"]["command"], "node")

    def test_refreshes_every_engine_owned_shim_and_no_user_document(self):
        """The whole point of the engine-owned/user-owned split (§7.4)."""
        shims = (
            ".claude/commands/ingest.md",
            ".agents/skills/librarian/SKILL.md",
            ".agents/skills/technician/SKILL.md",
            ".agents/plugins/cadabby/skills/cadabby-wiki/SKILL.md",
            ".agents/plugins/cadabby/agents/librarian/agent.md",
        )
        for rel in shims:
            (self.vault / rel).write_text("STALE SHIM FROM AN OLD VERSION\n")
        self._mark_user_files()

        code, _ = self._install()
        self.assertEqual(code, 0)
        for rel in shims:
            self.assertNotIn("STALE SHIM", (self.vault / rel).read_text("utf-8"), f"{rel} was not refreshed")
        self._assert_user_files_intact()

    def test_dry_run_reports_the_refresh_without_performing_it(self):
        stale = self.vault / ".claude" / "commands" / "ingest.md"
        stale.write_text("STALE SHIM\n")

        code, out = self._install(dry_run=True)
        self.assertEqual(code, 0)
        self.assertIn("dry-run", out)
        self.assertEqual(stale.read_text("utf-8"), "STALE SHIM\n")

    def test_global_install_writes_no_vault_files(self):
        """--global configures the user environment; the vault is not its business."""
        self._mark_user_files()
        stale = self.vault / ".claude" / "commands" / "ingest.md"
        stale.write_text("STALE SHIM\n")

        # Claude only: --path takes a single destination, so pairing it with
        # --all would hand the same path to both harnesses.
        self._install(is_global=True, all=False, claude=True, path=str(self.dir / "global.json"), force=True)
        self.assertEqual(stale.read_text("utf-8"), "STALE SHIM\n")
        self._assert_user_files_intact()

    def test_init_force_is_a_reset_not_a_refresh(self):
        """Pins why `install` had to exist, and why §7.4 must not recommend this.

        Notes and the ledger survive; every user-owned document does not.
        """
        self._mark_user_files()
        (self.vault / "wiki" / "Mine.md").write_text("---\ntitle: Mine\n---\n\nZZUSERZZ\n")

        with contextlib.redirect_stdout(io.StringIO()):
            cmd_init(DummyArgs(vault=str(self.vault), name="vault", obsidian=False, force=True))

        self.assertIn("ZZUSERZZ", (self.vault / "wiki" / "Mine.md").read_text("utf-8"), "notes must survive")
        for name in self.USER_OWNED:
            self.assertNotIn(
                "ZZUSERZZ",
                (self.vault / name).read_text("utf-8"),
                f"{name} unexpectedly survived --force; if this is now intended, §7.4 needs rewriting",
            )


class TestInstallPathIsSingleTarget(unittest.TestCase):
    """§7.6. `--path` names one destination, and destinations have a kind.

    Antigravity installs a plugin *directory*; Claude writes a JSON *file*. One
    `--path` therefore cannot serve both, and a `--path` of the wrong kind
    cannot serve either. Before these guards, `--all --path X` created the
    plugin directory and then died reading it as JSON, leaving a half-installed
    vault behind -- and `--dry-run` reported that both steps would succeed.
    """

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp_dir.name)
        self.vault = self.dir / "vault"
        cmd_init(DummyArgs(vault=str(self.vault), name="vault", obsidian=False))

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _install(self, **overrides):
        """Capture both streams: the multi-target refusal is a stderr diagnostic."""
        args = {
            "vault": str(self.vault), "path": None, "is_global": False,
            "antigravity": False, "claude": False, "all": False,
            "uninstall": False, "dry_run": False, "force": False,
        }
        args.update(overrides)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cmd_install(DummyArgs(**args))
        return code, out.getvalue() + err.getvalue()

    def test_path_with_all_is_refused_before_anything_is_written(self):
        dest = self.dir / "dest"
        code, output = self._install(all=True, path=str(dest))
        self.assertEqual(code, 1)
        self.assertIn("single destination", output)
        self.assertFalse(dest.exists(), "refusal must precede the Antigravity write, not follow it")

    def test_path_with_both_targets_named_explicitly_is_also_refused(self):
        """--all is sugar for this; the guard keys on the targets, not the flag."""
        dest = self.dir / "dest"
        code, _ = self._install(antigravity=True, claude=True, path=str(dest))
        self.assertEqual(code, 1)
        self.assertFalse(dest.exists())

    def test_path_with_all_is_refused_under_dry_run(self):
        """The preview exists to catch exactly this before the user commits."""
        code, output = self._install(all=True, path=str(self.dir / "dest"), dry_run=True)
        self.assertEqual(code, 1)
        self.assertNotIn("Would", output, "dry-run must not report success for an impossible install")

    def test_path_with_all_is_refused_for_uninstall_too(self):
        code, _ = self._install(all=True, path=str(self.dir / "dest"), uninstall=True)
        self.assertEqual(code, 1)

    def test_claude_rejects_a_directory_destination(self):
        adir = self.dir / "adir"
        adir.mkdir()
        code, output = self._install(claude=True, path=str(adir))
        self.assertEqual(code, 1)
        self.assertIn("must be a JSON file", output)
        self.assertEqual(list(adir.iterdir()), [], "nothing may be written into it")

    def test_antigravity_rejects_a_file_destination(self):
        afile = self.dir / "afile.json"
        afile.write_text("{}\n")
        code, output = self._install(antigravity=True, path=str(afile))
        self.assertEqual(code, 1)
        self.assertIn("must be a directory", output)

    def test_wrong_kind_is_reported_by_dry_run(self):
        adir = self.dir / "adir"
        adir.mkdir()
        code, output = self._install(claude=True, path=str(adir), dry_run=True)
        self.assertEqual(code, 1)
        self.assertNotIn("Would", output)

    def test_force_does_not_destroy_an_unrelated_destination(self):
        """--force overwrites Cadabby's own output, not arbitrary user data."""
        afile = self.dir / "afile.json"
        afile.write_text("{}\n")
        code, _ = self._install(antigravity=True, path=str(afile), force=True)
        self.assertEqual(code, 1)
        self.assertEqual(afile.read_text("utf-8"), "{}\n")

    def test_uninstall_rejects_a_wrong_kind_destination(self):
        """rmtree on a file and read_text on a directory both raise raw errnos."""
        adir = self.dir / "adir"
        adir.mkdir()
        code, _ = self._install(claude=True, path=str(adir), uninstall=True)
        self.assertEqual(code, 1)
        self.assertTrue(adir.is_dir(), "a failed uninstall must not remove it")

    def test_single_target_path_still_installs(self):
        """The guards must not cost the feature they protect."""
        plug, cfg = self.dir / "plug", self.dir / "cfg.json"
        self.assertEqual(self._install(antigravity=True, path=str(plug))[0], 0)
        self.assertEqual(self._install(claude=True, path=str(cfg))[0], 0)
        self.assertTrue((plug / "mcp_config.json").is_file())
        self.assertIn("cadabby", json.loads(cfg.read_text("utf-8"))["mcpServers"])

    def test_all_without_path_is_unaffected(self):
        code, _ = self._install(all=True)
        self.assertEqual(code, 0)
        self.assertTrue((self.vault / ".mcp.json").is_file())
        self.assertTrue((self.vault / ".agents" / "plugins" / "cadabby").is_dir())


class TestGlobalInstallBindsOnlyWhenAsked(unittest.TestCase):
    """§7.4/§7.6. A global install binds to a vault only if one is named.

    Bare `--global` used to adopt whatever vault the cwd happened to sit in,
    writing that one absolute path into a config every workspace on the machine
    reads -- so a second vault's agent silently served the first. It also lost
    the symlink, because a bound plugin must be a copy, quietly voiding §7.4's
    one exception to the frozen-copy rule.
    """

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp_dir.name)
        self.home = self.dir / "home"
        self.home.mkdir()
        self.vault_a = self.dir / "vaultA"
        self.vault_b = self.dir / "vaultB"
        for v in (self.vault_a, self.vault_b):
            cmd_init(DummyArgs(vault=str(v), name=v.name, obsidian=False))
        self._home_patch = unittest.mock.patch.object(Path, "home", staticmethod(lambda: self.home))
        self._home_patch.start()
        self.addCleanup(self._home_patch.stop)
        self._cwd = os.getcwd()
        self.addCleanup(os.chdir, self._cwd)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _install(self, cwd, **overrides):
        args = {
            "vault": None, "path": None, "is_global": True,
            "antigravity": False, "claude": False, "all": True,
            "uninstall": False, "dry_run": False, "force": False,
        }
        args.update(overrides)
        os.chdir(cwd)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cmd_install(DummyArgs(**args))
        os.chdir(self._cwd)
        return code, out.getvalue() + err.getvalue()

    def _global_plugin(self):
        return self.home / ".gemini" / "antigravity" / "plugins" / "cadabby"

    def _global_claude_args(self):
        cfg = json.loads((self.home / ".claude.json").read_text("utf-8"))
        return cfg["mcpServers"]["cadabby"]["args"]

    def test_bare_global_inside_a_vault_does_not_bind_to_it(self):
        """The regression: cwd must not become a machine-wide binding."""
        self.assertEqual(self._install(self.vault_a)[0], 0)
        self.assertNotIn("--vault", self._global_claude_args())
        self.assertNotIn(
            str(self.vault_a),
            json.dumps(self._global_claude_args()),
            "no vault path may leak into a config every workspace reads",
        )

    def test_bare_global_inside_a_vault_still_symlinks_the_plugin(self):
        """§7.4's sole escape from the frozen-copy rule must survive."""
        self._install(self.vault_a)
        self.assertTrue(self._global_plugin().is_symlink())

    def test_bare_global_plugin_config_names_no_vault(self):
        self._install(self.vault_a)
        plugin = self._global_plugin().resolve()
        cfg = json.loads((plugin / "mcp_config.json").read_text("utf-8"))
        self.assertNotIn("--vault", cfg["mcpServers"]["cadabby"]["args"])

    def test_bare_global_is_identical_from_either_vault(self):
        """Where you stand stops being an input."""
        self._install(self.vault_a)
        first = self._global_claude_args()
        code, _ = self._install(self.vault_b)
        self.assertEqual(code, 0, "a second vault must not provoke a --force conflict")
        self.assertEqual(self._global_claude_args(), first)

    def test_bare_global_outside_any_vault_is_unchanged(self):
        self.assertEqual(self._install(self.dir)[0], 0)
        self.assertNotIn("--vault", self._global_claude_args())
        self.assertTrue(self._global_plugin().is_symlink())

    def test_explicit_vault_still_binds_a_global_install(self):
        """--vault is how a single-vault user opts in, from anywhere."""
        code, _ = self._install(self.dir, vault=str(self.vault_a))
        self.assertEqual(code, 0)
        args = self._global_claude_args()
        self.assertIn("--vault", args)
        self.assertIn(str(self.vault_a.resolve()), args)

    def test_binding_a_global_install_costs_the_symlink(self):
        """Not a policy choice: mcp_config.json lives inside the plugin dir."""
        self._install(self.dir, vault=str(self.vault_a))
        plugin = self._global_plugin()
        self.assertFalse(plugin.is_symlink())
        cfg = json.loads((plugin / "mcp_config.json").read_text("utf-8"))
        self.assertIn(str(self.vault_a.resolve()), cfg["mcpServers"]["cadabby"]["args"])

    def test_global_install_still_writes_no_vault_files(self):
        before = sorted(p.name for p in self.vault_a.iterdir())
        self._install(self.vault_a)
        self.assertEqual(sorted(p.name for p in self.vault_a.iterdir()), before)

    def test_workspace_install_is_unaffected(self):
        """Non-global installs still discover and bind the cwd vault."""
        code, _ = self._install(self.vault_a, is_global=False)
        self.assertEqual(code, 0)
        cfg = json.loads((self.vault_a / ".mcp.json").read_text("utf-8"))
        self.assertIn(str(self.vault_a.resolve()), cfg["mcpServers"]["cadabby"]["args"])


if __name__ == "__main__":
    unittest.main()



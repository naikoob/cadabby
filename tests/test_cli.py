"""Unit tests for cadabby CLI subcommands."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from cadabby.errors import EXIT_ENVIRONMENT, EXIT_USAGE, classify, exit_code_for
from cadabby.cli import (
    build_parser,
    cmd_graph,
    cmd_ground,
    cmd_init,
    cmd_install,
    cmd_lint,
    cmd_move,
    cmd_scaffold,
    cmd_search,
    cmd_status,
    cmd_sync,
    cmd_update,
    cmd_verify,
)
from cadabby.installer import mcp_launch_argv
from cadabby.mcp import McpServer
from cadabby.vault import Vault
from tests.helpers import DummyArgs, copy_demo_vault, demo_vault, temp_dir


def _run_like_main(cmd, args) -> int:
    """Run a command the way `main()` does: a raised error becomes its exit code (§5.4)."""
    try:
        return cmd(args)
    except Exception as e:  # noqa: BLE001
        info = classify(e)
        print(f"Error [{info.code}]: {info.message}", file=sys.stderr)
        return exit_code_for(info.code)


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = temp_dir(self)
        self.dir = self.tmp_dir

    def test_abbreviated_flags_are_rejected(self):
        """`--for` must not silently mean `--force`, on the root or any subcommand."""
        parser = build_parser()
        for argv in (["sync", "--for"], ["update", "x", "--exp", "h"], ["--vau", "v", "status"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    parser.parse_args(argv)
                self.assertEqual(ctx.exception.code, EXIT_USAGE)
        self.assertTrue(parser.parse_args(["sync", "--force"]).force)

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
        vault_root = copy_demo_vault(self.dir / "demo-vault")

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
            targets=["wiki/sqlite"],
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
        self.assertTrue((vault_root / "wiki" / "graph-neural-networks.md").exists())

        # 6. Verify
        args_verify = DummyArgs(
            vault=str(vault_root),
            cid="wiki/graph-neural-networks",
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
        # The parser rejects the pair itself, so exit 2 stays argparse's (§5.4).
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            build_parser().parse_args(["verify", "wiki/Test", "--human", "--agent", "a"])
        self.assertEqual(ctx.exception.code, EXIT_USAGE)
        # A programmatic caller that bypasses the parser gets INVALID_ARGUMENT.
        with self.assertRaises(ValueError):
            cmd_verify(args_verify)

    def _verify_args(self, vault_root: Path, *, human: bool) -> DummyArgs:
        return DummyArgs(vault=str(vault_root), cid="wiki/sqlite", human=human, agent=None, method=None)

    def _run_verify_on_tty(self, args: DummyArgs, answer: str) -> tuple[int, str]:
        out = io.StringIO()
        with (
            unittest.mock.patch("sys.stdin.isatty", return_value=True),
            unittest.mock.patch("cadabby.cli.secrets.token_hex", return_value="ab12"),
            unittest.mock.patch("builtins.input", return_value=answer) as fake_input,
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            try:
                code = cmd_verify(args)
            finally:
                self.last_prompt_count = fake_input.call_count
        return code, out.getvalue()

    def test_flagless_verify_in_a_tty_stamps_process_cli(self):
        # §5.3: agents run shell commands in pseudo-terminals, so a TTY is no
        # evidence of a human. Only the explicit flag reaches the human tier.
        vault_root = copy_demo_vault(self.dir / "flagless-vault")
        note = vault_root / "wiki" / "sqlite.md"
        humans_before = note.read_text("utf-8").count("human:")
        code, out = self._run_verify_on_tty(self._verify_args(vault_root, human=False), answer="")
        self.assertEqual(code, 0)
        self.assertEqual(self.last_prompt_count, 0)
        self.assertIn("'process:cli'", out)
        text = note.read_text("utf-8")
        self.assertIn("by: \"process:cli\"", text)
        self.assertEqual(text.count("human:"), humans_before)

    def test_human_verify_requires_typed_token(self):
        vault_root = copy_demo_vault(self.dir / "token-vault")
        code, out = self._run_verify_on_tty(self._verify_args(vault_root, human=True), answer="ab12")
        self.assertEqual(code, 0)
        self.assertEqual(self.last_prompt_count, 1)
        self.assertIn("wiki/sqlite", out)
        self.assertIn("'human:", out)

    def test_human_verify_wrong_token_writes_nothing(self):
        vault_root = copy_demo_vault(self.dir / "wrong-token-vault")
        note = vault_root / "wiki" / "sqlite.md"
        before = note.read_bytes()
        for answer in ("y", "yes", ""):
            with self.subTest(answer=answer):
                # INVALID_ARGUMENT (exit 3) via main(), never 2, which is argparse's (§5.4).
                with self.assertRaises(ValueError):
                    self._run_verify_on_tty(self._verify_args(vault_root, human=True), answer=answer)
                self.assertEqual(note.read_bytes(), before)

    def test_human_verify_signs_only_the_body_that_was_shown(self):
        """C75, S4: an agent editing the body while the person types the token gets nothing signed."""
        from cadabby.errors import VaultConflictError
        from cadabby.frontmatter import parse_frontmatter

        vault_root = copy_demo_vault(self.dir / "race-vault")
        note = vault_root / "wiki" / "sqlite.md"
        entries_before = len(parse_frontmatter(note.read_text("utf-8"))[0].get("verified", []))

        def agent_edits_then_person_types(prompt):
            note.write_text(note.read_text("utf-8") + "\nThe agent's new claim.\n", "utf-8")
            return "ab12"

        with (
            unittest.mock.patch("sys.stdin.isatty", return_value=True),
            unittest.mock.patch("cadabby.cli.secrets.token_hex", return_value="ab12"),
            unittest.mock.patch("builtins.input", side_effect=agent_edits_then_person_types),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaisesRegex(VaultConflictError, "nothing was signed"):
                cmd_verify(self._verify_args(vault_root, human=True))
        fm, body = parse_frontmatter(note.read_text("utf-8"))
        self.assertIn("The agent's new claim.", body)
        self.assertEqual(len(fm.get("verified", [])), entries_before, "no attestation was added")

    def test_human_verify_without_tty_is_invalid_argument(self):
        vault_root = copy_demo_vault(self.dir / "no-tty-vault")
        with unittest.mock.patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(ValueError) as ctx:
                cmd_verify(self._verify_args(vault_root, human=True))
        self.assertIn("TTY", str(ctx.exception))

    def test_cli_writes_default_to_process_cli_even_on_a_tty(self):
        """C37: scaffold and update record `process:cli` unless `--actor` is given.

        A terminal is no evidence of a person (§5.3): agents run commands in
        pseudo-terminals. The old `human:<user>` default let any flagless
        agent call attribute its writes to the human.
        """
        vault_root = copy_demo_vault(self.dir / "actor-vault")
        common = dict(vault=str(vault_root), sources=None, tags=None, actor=None)
        with unittest.mock.patch("sys.stdin.isatty", return_value=True), contextlib.redirect_stdout(io.StringIO()):
            cmd_scaffold(DummyArgs(title="Actor Probe", type="concept", desc="d", **common))
            cmd_update(
                DummyArgs(
                    vault=str(vault_root),
                    cid="wiki/actor-probe",
                    patch_frontmatter=None,
                    expected_hash=None,
                    edits='[{"op": "append_section", "heading": "Notes", "body": "more"}]',
                    actor=None,
                )
            )
            cmd_update(
                DummyArgs(
                    vault=str(vault_root),
                    cid="wiki/actor-probe",
                    patch_frontmatter='{"status": "active"}',
                    expected_hash=None,
                    edits=None,
                    actor="agent:me",
                )
            )

        note = (vault_root / "wiki" / "actor-probe.md").read_text("utf-8")
        self.assertIn('by: "process:cli"', note)
        self.assertNotIn("human:", note)
        log = (vault_root / "log.md").read_text("utf-8")
        updates = [l for l in log.splitlines() if "Updated wiki/actor-probe.md" in l]
        self.assertIn("process:cli", next(l for l in log.splitlines() if "Scaffolded wiki/actor-probe.md" in l))
        self.assertIn("process:cli", updates[0])
        self.assertIn("agent:me", updates[1])

    def test_graph_unknown_note_is_not_found(self):
        vault_root = copy_demo_vault(self.dir / "graph-vault")
        with self.assertRaises(FileNotFoundError):
            cmd_graph(DummyArgs(vault=str(vault_root), target="wiki/No-Such-Note", mocs=False, limit=20, json=True))

    def test_install_usage_errors_and_refusals_never_exit_findings(self):
        """C38: install's usage errors are INVALID_ARGUMENT; a refused install exits 3, never 1."""
        vault_root = self.dir / "install-codes-vault"
        cmd_init(DummyArgs(vault=str(vault_root), name="v", obsidian=False))
        base = dict(vault=str(vault_root), path=None, is_global=False, uninstall=False, dry_run=False, force=False)
        with self.assertRaises(ValueError):
            cmd_install(DummyArgs(antigravity=False, claude=False, all=False, **base))
        with self.assertRaises(ValueError):
            cmd_install(DummyArgs(antigravity=False, claude=False, all=True, **{**base, "path": str(self.dir / "x")}))

        claude_cfg = self.dir / "claude.json"
        claude_cfg.write_text("{not json", "utf-8")
        args = DummyArgs(antigravity=False, claude=True, all=False, **{**base, "path": str(claude_cfg)})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cmd_install(args), EXIT_ENVIRONMENT)
        self.assertEqual(claude_cfg.read_text("utf-8"), "{not json")

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

    def test_cmd_update_invalid_json_flags_name_the_offending_flag(self):
        vault_path = copy_demo_vault(self.dir / "demo-vault")
        base = {
            "vault": str(vault_path),
            "cid": "SQLite",
            "patch_frontmatter": None,
            "expected_hash": None,
            "actor": None,
            "edits": None,
        }
        with self.assertRaisesRegex(ValueError, r"Invalid JSON for --patch-frontmatter"):
            cmd_update(DummyArgs(**{**base, "patch_frontmatter": "{bad"}))
        with self.assertRaisesRegex(ValueError, r"Expected JSON object for --patch-frontmatter"):
            cmd_update(DummyArgs(**{**base, "patch_frontmatter": "[1, 2]"}))
        with self.assertRaisesRegex(ValueError, r"Invalid JSON for --edits"):
            cmd_update(DummyArgs(**{**base, "edits": "[bad"}))
        with self.assertRaisesRegex(ValueError, r"Expected JSON array for --edits"):
            cmd_update(DummyArgs(**{**base, "edits": '{"op": "append_section"}'}))


class TestInstallIsTheRefreshVerb(unittest.TestCase):
    """§7.4/§7.6. `install` repairs what `init` scaffolded and will not revisit.

    Two things go stale in a working vault: the absolute interpreter path baked
    into the MCP config, and the engine-owned shims copied at scaffold time.
    `init` declines to touch either, and `init --force` repairs them only by
    resetting the user's documents as well. These pin the narrow path.
    """

    USER_OWNED = ("AGENTS.md", "CLAUDE.md", "GEMINI.md", "STYLE.md", "index.md")

    def setUp(self):
        self.tmp_dir = temp_dir(self)
        self.dir = self.tmp_dir
        self.vault = self.dir / "vault"
        cmd_init(DummyArgs(vault=str(self.vault), name="vault", obsidian=False))

    def _install(self, **overrides):
        args = {
            "vault": str(self.vault), "path": None, "is_global": False,
            "antigravity": False, "claude": False, "all": True,
            "uninstall": False, "dry_run": False, "force": False,
        }
        args.update(overrides)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = _run_like_main(cmd_install, DummyArgs(**args))
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
        self.assertEqual(code, EXIT_ENVIRONMENT)
        self.assertEqual(self._mcp()["mcpServers"]["cadabby"]["args"][-1], "/somewhere/else")

    def test_a_foreign_server_under_our_key_is_still_a_conflict(self):
        """Someone else's server registered as `cadabby` is not ours to rebind."""
        data = self._mcp()
        data["mcpServers"]["cadabby"] = {"command": "node", "args": ["their-server.js"]}
        (self.vault / ".mcp.json").write_text(json.dumps(data, indent=2))

        code, _ = self._install()
        self.assertEqual(code, EXIT_ENVIRONMENT)
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
        self.tmp_dir = temp_dir(self)
        self.dir = self.tmp_dir
        self.vault = self.dir / "vault"
        cmd_init(DummyArgs(vault=str(self.vault), name="vault", obsidian=False))

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
            code = _run_like_main(cmd_install, DummyArgs(**args))
        return code, out.getvalue() + err.getvalue()

    def test_path_with_all_is_refused_before_anything_is_written(self):
        dest = self.dir / "dest"
        code, output = self._install(all=True, path=str(dest))
        self.assertEqual(code, EXIT_ENVIRONMENT)
        self.assertIn("single destination", output)
        self.assertFalse(dest.exists(), "refusal must precede the Antigravity write, not follow it")

    def test_path_with_both_targets_named_explicitly_is_also_refused(self):
        """--all is sugar for this; the guard keys on the targets, not the flag."""
        dest = self.dir / "dest"
        code, _ = self._install(antigravity=True, claude=True, path=str(dest))
        self.assertEqual(code, EXIT_ENVIRONMENT)
        self.assertFalse(dest.exists())

    def test_path_with_all_is_refused_under_dry_run(self):
        """The preview exists to catch exactly this before the user commits."""
        code, output = self._install(all=True, path=str(self.dir / "dest"), dry_run=True)
        self.assertEqual(code, EXIT_ENVIRONMENT)
        self.assertNotIn("Would", output, "dry-run must not report success for an impossible install")

    def test_path_with_all_is_refused_for_uninstall_too(self):
        code, _ = self._install(all=True, path=str(self.dir / "dest"), uninstall=True)
        self.assertEqual(code, EXIT_ENVIRONMENT)

    def test_claude_rejects_a_directory_destination(self):
        adir = self.dir / "adir"
        adir.mkdir()
        code, output = self._install(claude=True, path=str(adir))
        self.assertEqual(code, EXIT_ENVIRONMENT)
        self.assertIn("must be a JSON file", output)
        self.assertEqual(list(adir.iterdir()), [], "nothing may be written into it")

    def test_antigravity_rejects_a_file_destination(self):
        afile = self.dir / "afile.json"
        afile.write_text("{}\n")
        code, output = self._install(antigravity=True, path=str(afile))
        self.assertEqual(code, EXIT_ENVIRONMENT)
        self.assertIn("must be a directory", output)

    def test_wrong_kind_is_reported_by_dry_run(self):
        adir = self.dir / "adir"
        adir.mkdir()
        code, output = self._install(claude=True, path=str(adir), dry_run=True)
        self.assertEqual(code, EXIT_ENVIRONMENT)
        self.assertNotIn("Would", output)

    def test_force_does_not_destroy_an_unrelated_destination(self):
        """--force overwrites Cadabby's own output, not arbitrary user data."""
        afile = self.dir / "afile.json"
        afile.write_text("{}\n")
        code, _ = self._install(antigravity=True, path=str(afile), force=True)
        self.assertEqual(code, EXIT_ENVIRONMENT)
        self.assertEqual(afile.read_text("utf-8"), "{}\n")

    def test_uninstall_rejects_a_wrong_kind_destination(self):
        """rmtree on a file and read_text on a directory both raise raw errnos."""
        adir = self.dir / "adir"
        adir.mkdir()
        code, _ = self._install(claude=True, path=str(adir), uninstall=True)
        self.assertEqual(code, EXIT_ENVIRONMENT)
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
        self.tmp_dir = temp_dir(self)
        self.dir = self.tmp_dir
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
            code = _run_like_main(cmd_install, DummyArgs(**args))
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

    def test_cmd_status_renders_filename_only_raw_line_in_human_output(self):
        (self.vault_a / "raw" / "paper.pdf").write_bytes(b"%PDF-1.4\n")
        (self.vault_a / "raw" / "slides.pptx").write_bytes(b"PK\x03\x04")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = cmd_status(DummyArgs(vault=str(self.vault_a), json=False))
        self.assertEqual(code, 0)
        rendered = out.getvalue()
        self.assertIn("Filename-Only Raw (body not indexed): 2 (.pdf: 1, .pptx: 1)", rendered)


class TestStatusAndMoveCommands(unittest.TestCase):
    """`cadabby status` shares MCP's report builder; `cadabby move` mirrors `vault_move_note`."""

    def setUp(self):
        self.vault_root = demo_vault(self, "demo-vault").root

    def _run(self, fn, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fn(DummyArgs(vault=str(self.vault_root), **kwargs))
        return code, out.getvalue()

    def test_status_json_carries_the_same_finding_counts_as_mcp(self):
        """§10 C46. On a vault with one dead link, CLI and MCP report the same non-zero counts."""
        (self.vault_root / "wiki" / "Dead-Linker.md").write_text(
            "---\ntype: concept\ntitle: Dead Linker\ndescription: d\nstatus: active\n---\n[[Nowhere]] [[sqlite]]\n", "utf-8"
        )
        code, out = self._run(cmd_status, json=True)
        self.assertEqual(code, 0)
        cli_counts = json.loads(out)["finding_counts"]
        self.assertEqual(cli_counts["error"], 1)
        self.assertEqual(cli_counts["by_code"]["LINK_DEAD"], 1)
        with McpServer(Vault(self.vault_root)) as server:
            res = server.handle_tools_call("vault_status", {})
        self.assertEqual(json.loads(res["content"][0]["text"])["finding_counts"], cli_counts)

    def test_move_dry_run_refusal_and_approved_apply(self):
        common = {"cid": "wiki/flash-attention", "to": "fa-tiling", "actor": None, "json": False}
        code, out = self._run(cmd_move, apply=False, approve_stale=None, **common)
        self.assertEqual(code, 0)
        self.assertIn("BLOCKED", out)
        self.assertTrue((self.vault_root / "wiki" / "flash-attention.md").exists())

        with self.assertRaises(ValueError) as ctx:
            self._run(cmd_move, apply=True, approve_stale=None, **common)
        self.assertEqual(exit_code_for(classify(ctx.exception).code), EXIT_ENVIRONMENT)

        code, out = self._run(cmd_move, apply=False, approve_stale=["wiki/epistemic-trust-tiers"], **common)
        token = out.rsplit("--plan-token ", 1)[1].split()[0]
        code, out = self._run(
            cmd_move, apply=True, approve_stale=["wiki/epistemic-trust-tiers"], plan_token=token, **common
        )
        self.assertEqual(code, 0)
        self.assertIn("Moved wiki/flash-attention -> wiki/fa-tiling", out)
        self.assertIn("fa-tiling.md", {p.name for p in (self.vault_root / "wiki").glob("*.md")})

    def test_move_json_dry_run_reports_blockers_tiers_and_token(self):
        code, out = self._run(cmd_move, cid="wiki/flash-attention", to="fa", apply=False, approve_stale=None, actor=None, json=True)
        self.assertEqual(code, 0)
        plan = json.loads(out)
        self.assertFalse(plan["applied"])
        self.assertTrue(any("epistemic-trust-tiers" in b for b in plan["blocked"]))
        tiers = {n["cid"]: n["tier_after"] for n in plan["notes"]}
        self.assertEqual(tiers["wiki/epistemic-trust-tiers"], "stale-verified")
        self.assertEqual(len(plan["plan_token"]), 32)

    def test_move_parser_takes_one_approval_per_flag(self):
        """§5.3. `--approve-stale` repeats; a value after the last one is a positional, never swallowed."""
        parser = build_parser()
        parsed = parser.parse_args(["move", "--approve-stale", "wiki/Ref1", "wiki/A", "wiki/B", "--approve-stale", "wiki/Ref2"])
        self.assertEqual((parsed.cid, parsed.to), ("wiki/A", "wiki/B"))
        self.assertEqual(parsed.approve_stale, ["wiki/Ref1", "wiki/Ref2"])
        self.assertEqual(parser.parse_args(["move", "a", "b", "--plan-token", "t0"]).plan_token, "t0")

    def test_counts_must_be_positive(self):
        """§5.3. `--limit -1` and `--budget 0` are usage errors, not "unlimited"."""
        for argv in (["search", "q", "--limit", "-1"], ["search", "q", "--limit", "x"], ["ground", "a", "--budget", "0"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
                build_parser().parse_args(argv)
            self.assertEqual(ctx.exception.code, EXIT_USAGE)

    def test_verify_human_resolves_stems_and_wikilinks_before_prompting(self):
        """§5.1. `cadabby verify <name> --human` shows the canonical note before asking for the token."""
        for cid in ("sqlite", "[[sqlite]]"):
            with (
                self.subTest(cid=cid),
                unittest.mock.patch("sys.stdin.isatty", return_value=True),
                unittest.mock.patch("builtins.input", return_value="wrong-token"),
                contextlib.redirect_stdout(io.StringIO()) as out,
            ):
                with self.assertRaisesRegex(ValueError, "Aborted: confirmation token did not match"):
                    cmd_verify(DummyArgs(vault=str(self.vault_root), cid=cid, human=True, agent=None, method=None))
            self.assertIn("cid:  wiki/sqlite", out.getvalue())

    def test_verify_human_refuses_an_ambiguous_name_before_prompting(self):
        for rel in ("customers/acme/README.md", "projects/apollo/README.md"):
            (self.vault_root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.vault_root / rel).write_text("---\ntype: moc\ntitle: Hub\ndescription: d\nstatus: active\n---\n# Hub\n", "utf-8")
        prompt = unittest.mock.Mock(return_value="x")
        with unittest.mock.patch("sys.stdin.isatty", return_value=True), unittest.mock.patch("builtins.input", prompt):
            with self.assertRaisesRegex(ValueError, "matches 2 notes"):
                cmd_verify(DummyArgs(vault=str(self.vault_root), cid="README", human=True, agent=None, method=None))
        prompt.assert_not_called()

    def test_cli_filter_only_search_and_section_grounding(self):
        """§5.3, §10 C55, C56: `cadabby search` allows omitting query when a filter is passed, and `cadabby ground` renders `[Section: ...]`."""
        parser = build_parser()

        # 1. Parser accepts `cadabby search --domain wiki` with no positional query
        parsed = parser.parse_args(["search", "--domain", "wiki", "--trust", "human-reviewed"])
        self.assertEqual(parsed.query, "")
        self.assertEqual(parsed.domain, "wiki")
        self.assertEqual(parsed.trust, "human-reviewed")

        # 2. cmd_search with filter-only prints "active filters" header
        code, out = self._run(
            cmd_search,
            query="",
            domain="wiki",
            type=None,
            status=None,
            trust="human-reviewed",
            tag=None,
            limit=10,
            json=False,
        )
        self.assertEqual(code, 0)
        self.assertIn("active filters", out)
        self.assertIn("wiki/epistemic-trust-tiers", out)

        # 3. cmd_search with no query and no filters exits EXIT_ENVIRONMENT (INVALID_ARGUMENT)
        with contextlib.redirect_stderr(io.StringIO()):
            exit_code = _run_like_main(
                cmd_search,
                DummyArgs(
                    vault=str(self.vault_root),
                    query="",
                    domain=None,
                    type=None,
                    status=None,
                    trust=None,
                    tag=None,
                    limit=10,
                    json=False,
                ),
            )
        self.assertEqual(exit_code, EXIT_ENVIRONMENT)

        # 4. cmd_ground with #section renders `[Section: ...]` in text output
        code_g, out_g = self._run(
            cmd_ground,
            targets=["wiki/epistemic-trust-tiers#Epistemic Trust Tiers"],
            budget_tokens=None,
            json=False,
        )
        self.assertEqual(code_g, 0)
        self.assertIn("[Section: Epistemic Trust Tiers]", out_g)

    def test_cli_ground_reports_failed_targets_and_keeps_the_rest(self):
        """§5.3, §10 C58: unresolvable targets print as errors on stderr; the rest still print; all-bad exits 3."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, out = self._run(cmd_ground, targets=["sqlite", "no-such-note"], budget_tokens=None, json=False)
        self.assertEqual(code, 0)
        self.assertIn("CID: wiki/sqlite", out)
        self.assertIn("Error [NOT_FOUND] no-such-note", err.getvalue())

        with contextlib.redirect_stderr(io.StringIO()):
            exit_code = _run_like_main(
                cmd_ground, DummyArgs(vault=str(self.vault_root), targets=["no-such-note"], budget_tokens=None, json=False)
            )
        self.assertEqual(exit_code, EXIT_ENVIRONMENT)


class TestCliMirrorsMcp(unittest.TestCase):
    """§5.3, §10 C71: the eight vault verbs mirror the eight MCP tools; the ledger has no free-text write."""

    def _subcommands(self) -> list[str]:
        import argparse

        parser = build_parser()
        action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        return list(action.choices)

    def test_every_mcp_tool_has_a_cli_verb_listed_first(self):
        from cadabby.mcp import TOOLS

        verbs = [t["name"].removeprefix("vault_").removesuffix("_note") for t in TOOLS]
        commands = self._subcommands()
        self.assertEqual(len(verbs), 8)
        self.assertEqual(set(commands[:8]), set(verbs))

    def test_log_is_gone(self):
        self.assertNotIn("log", self._subcommands())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            build_parser().parse_args(["log", "hello"])
        self.assertEqual(ctx.exception.code, EXIT_USAGE)

    def test_graph_needs_exactly_one_of_target_or_mocs(self):
        parser = build_parser()
        for argv in (["graph"], ["graph", "x", "--mocs"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser.parse_args(argv)
        self.assertTrue(parser.parse_args(["graph", "--mocs"]).mocs)

    def test_status_lint_lists_findings_and_still_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = copy_demo_vault(Path(tmp) / "v")
            (root / "wiki" / "broken.md").write_text("---\ntype: concept\ntitle: B\ndescription: d\nstatus: active\n---\n[[nowhere]]\n", "utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cmd_status(DummyArgs(vault=str(root), json=True, lint=True))
            data = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertIn("LINK_DEAD", {f["code"] for f in data["findings"]})
        self.assertIn("queues", data)

    def test_graph_mocs_json_and_raw_ground_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = copy_demo_vault(Path(tmp) / "v")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cmd_graph(DummyArgs(vault=str(root), target=None, mocs=True, limit=20, json=True))
            self.assertEqual(json.loads(out.getvalue())["view"], "mocs")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cmd_ground(DummyArgs(vault=str(root), targets=["raw/karpathy-llm-wiki-gist.md"], budget_tokens=None, json=False))
            self.assertIn("Cited by:", out.getvalue())


class TestCliPolish(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir(self)
        self.dir = self.tmp

    def test_audit_json_has_one_shape_with_or_without_a_notice(self):
        import contextlib
        import io

        from cadabby.cli import cmd_audit

        vault_root = self.dir / "v"
        cmd_init(DummyArgs(vault=str(vault_root), name="v", obsidian=False))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cmd_audit(DummyArgs(vault=str(vault_root), require_signed=False, json=True))
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(set(payload), {"notice", "findings"})
        self.assertEqual(payload["findings"], [])

        from unittest import mock

        from cadabby.audit import AuditFinding

        finding = AuditFinding(code="X", rel_path="wiki/n.md", line=7, actor="human:a", commit="c" * 40, message="m")
        out = io.StringIO()
        with mock.patch("cadabby.cli.run_vault_audit", return_value=([finding], None)), contextlib.redirect_stdout(out):
            code = cmd_audit(DummyArgs(vault=str(vault_root), require_signed=False, json=True))
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertIsNone(payload["notice"])
        self.assertEqual([f["line"] for f in payload["findings"]], [7])

    def test_search_without_query_or_filter_is_invalid_argument(self):
        """C38: a cross-argument rule argparse cannot express is INVALID_ARGUMENT, exit 3."""
        import os
        import subprocess
        import sys

        vault_root = self.dir / "v"
        cmd_init(DummyArgs(vault=str(vault_root), name="v", obsidian=False))
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        res = subprocess.run(
            [sys.executable, "-m", "cadabby", "search", "--vault", str(vault_root)],
            capture_output=True, text=True, env=env, check=False,
        )
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertIn("INVALID_ARGUMENT", res.stderr)


class TestCliActorFlag(unittest.TestCase):
    """C72: --actor is attribution, so it obeys §3.4 and never claims a human."""

    def test_parser_refuses_human_and_malformed_actors(self):
        import contextlib
        import io

        parser = build_parser()
        for bad in ("human:alice", "agent:a\nhuman:eve", "bob"):
            for argv in (["scaffold", "T", "--type", "concept", "--description", "d", "--actor", bad],
                         ["update", "wiki/x", "--actor", bad]):
                with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as ctx:
                        parser.parse_args(argv)
                    self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(parser.parse_args(["update", "wiki/x", "--actor", "agent:me"]).actor, "agent:me")

    def test_programmatic_callers_get_invalid_argument(self):
        from cadabby.cli import _default_cli_actor

        with self.assertRaises(ValueError):
            _default_cli_actor(DummyArgs(actor="human:alice"))
        self.assertEqual(_default_cli_actor(DummyArgs(actor=None)), "process:cli")


class TestCliEntryPoint(unittest.TestCase):
    """C75: main(argv) is testable in-process; usage and pipe edge cases exit as specified."""

    def setUp(self):
        self.vault_root = demo_vault(self, "demo-vault").root

    def _main(self, *argv):
        from cadabby.cli import main

        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            main(list(argv))
        return ctx.exception.code

    def test_an_empty_graph_target_is_a_usage_error(self):
        self.assertEqual(self._main("graph", "", "--vault", str(self.vault_root)), 2)

    def test_a_closed_pipe_is_not_a_failure(self):
        from unittest import mock

        import cadabby.cli as cli

        with (
            mock.patch.object(cli, "cmd_status", side_effect=BrokenPipeError),
            mock.patch.object(cli.os, "open", return_value=99),
            mock.patch.object(cli.os, "dup2"),
        ):
            self.assertEqual(self._main("status", "--vault", str(self.vault_root)), 0)

    def test_main_runs_in_process_with_the_taxonomy_exit_code(self):
        self.assertEqual(self._main("ground", "wiki/does-not-exist", "--vault", str(self.vault_root)), 3)


class TestCliWriteJson(unittest.TestCase):
    """C76: scaffold, update and verify print the MCP payload with --json (§5.3)."""

    def setUp(self):
        self.vault_root = demo_vault(self, "demo-vault").root

    def _json(self, *argv):
        from cadabby.cli import main

        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                main([*argv, "--vault", str(self.vault_root), "--json"])
        self.assertEqual(ctx.exception.code, 0, out.getvalue())
        return json.loads(out.getvalue())

    def test_write_verbs_print_the_mcp_payload(self):
        made = self._json("scaffold", "Cli Json", "--type", "concept", "--desc", "d", "--body", "# Cli Json\n\nBody text.")
        self.assertEqual(set(made), {"cid", "rel_path", "source_hash", "trust_tier", "warnings"})
        self.assertIn("Body text.", (self.vault_root / made["rel_path"]).read_text("utf-8"))
        edited = self._json("update", made["cid"], "--patch-frontmatter", '{"status": "active"}',
                            "--expected-hash", made["source_hash"])
        self.assertNotEqual(edited["source_hash"], made["source_hash"])
        verified = self._json("verify", made["cid"])
        self.assertLessEqual({"cid", "actor", "of", "trust_tier", "already_verified", "warnings"}, set(verified))


class TestSimulationRemediations(unittest.TestCase):
    """C78: init never plants a vault inside another, and search points at raw/ when that is where the term is."""

    def setUp(self):
        self.tmp = temp_dir(self)
        self.dir = self.tmp

    def _init(self, path: Path, **opts) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = _run_like_main(cmd_init, DummyArgs(vault=str(path), name=path.name, obsidian=False, **opts))
        return code, err.getvalue()

    def test_init_refuses_a_path_inside_another_vault(self):
        parent = self.dir / "parent"
        self.assertEqual(self._init(parent)[0], 0)
        nested = parent / "customers" / "sub"
        for force in (False, True):
            with self.subTest(force=force):
                code, err = self._init(nested, force=force)
                self.assertEqual(code, EXIT_ENVIRONMENT)
                self.assertIn("INVALID_ARGUMENT", err)
                self.assertIn(str(parent.resolve()), err)
                self.assertFalse(nested.exists(), "a refused init must write nothing")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cmd_lint(DummyArgs(vault=str(parent), json=True)), 0)

    def test_init_under_a_plain_obsidian_folder_is_allowed(self):
        outer = self.dir / "notes"
        (outer / ".obsidian").mkdir(parents=True)
        vault = outer / "v"
        self.assertEqual(self._init(vault)[0], 0)
        self.assertTrue((vault / ".cadabby.json").is_file())
        # Re-running init on the vault itself is not nesting.
        self.assertEqual(self._init(vault)[0], 0)

    def test_search_hints_at_raw_matches_only_when_unscoped(self):
        vault = self.dir / "v"
        self.assertEqual(self._init(vault)[0], 0)
        (vault / "raw" / "study.md").write_text("# Study\nPaxos outperforms Raft here.\n", "utf-8")

        def search(query: str, **opts) -> str:
            base = dict(vault=str(vault), query=query, domain=None, type=None, status=None,
                        trust=None, tag=None, limit=10, json=False)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cmd_search(DummyArgs(**{**base, **opts})), 0)
            return out.getvalue()

        hint = "--domain raw"
        self.assertIn(hint, search("Paxos"))
        self.assertNotIn(hint, search("Paxos", domain="wiki"))
        self.assertNotIn(hint, search("Paxos", trust="human-reviewed"))
        self.assertEqual(json.loads(search("Paxos", json=True)), [])
        self.assertNotIn(hint, search("Zanzibar"))

    def _human_verify(self, vault: Path, login: str, prompt: unittest.mock.Mock) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            unittest.mock.patch("sys.stdin.isatty", return_value=True),
            unittest.mock.patch("builtins.input", prompt),
            unittest.mock.patch("getpass.getuser", return_value=login),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = _run_like_main(
                cmd_verify, DummyArgs(vault=str(vault), cid="wiki/a", human=True, agent=None, method=None, json=False)
            )
        return code, out.getvalue(), err.getvalue()

    def _vault_with_note(self) -> Path:
        vault = self.dir / "v"
        self.assertEqual(self._init(vault)[0], 0)
        McpServer(Vault(vault)).handle_tools_call(
            "vault_scaffold_note", {"title": "A", "type": "concept", "description": "d", "path": "wiki/a.md"}
        )
        return vault

    def test_human_verify_sanitizes_the_os_username(self):
        """§3.5, §10 C79. SSSD/AD and Windows usernames sign as the actor grammar allows, shown first."""
        vault = self._vault_with_note()
        for login, actor in (("alice@corp.example.com", "human:alice-corp.example.com"), ("John Smith", "human:John-Smith")):
            with self.subTest(login=login):
                answer = unittest.mock.Mock(side_effect=lambda msg: msg.split("Type ")[1].split(" ")[0])
                code, out, err = self._human_verify(vault, login, answer)
                self.assertEqual(code, 0, err)
                self.assertIn(f"as:   {actor}", out)
                self.assertIn(f"by: {actor}", (vault / "wiki" / "a.md").read_text("utf-8").replace('"', ""))

    def test_human_verify_refuses_an_unusable_username_before_prompting(self):
        """§3.5, §10 C79. Nothing of the name fits the grammar: refused before the person reviews anything."""
        vault = self._vault_with_note()
        prompt = unittest.mock.Mock(return_value="x")
        code, out, err = self._human_verify(vault, "李雷", prompt)
        self.assertEqual(code, EXIT_ENVIRONMENT)
        self.assertIn("INVALID_ARGUMENT", err)
        prompt.assert_not_called()
        self.assertNotIn("Type ", out)
        self.assertNotIn("human:", (vault / "wiki" / "a.md").read_text("utf-8"))

    def test_cli_scaffold_status_aliases_and_frontmatter(self):
        from cadabby.cli import build_parser
        from cadabby.frontmatter import parse_frontmatter

        vault = self.dir / "v"
        self.assertEqual(self._init(vault)[0], 0)
        parser = build_parser()
        args = parser.parse_args(
            [
                "--vault",
                str(vault),
                "scaffold",
                "Kubernetes CrashLoopBackOff",
                "--type",
                "guide",
                "--desc",
                "Pod restart troubleshooting",
                "--status",
                "active",
                "--aliases",
                "K8s CrashLoop, CLBO",
                "--frontmatter",
                '{"severity": "sev2"}',
                "--json",
            ]
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(args.func(args), 0)
        fm, _ = parse_frontmatter((vault / "wiki" / "kubernetes-crashloopbackoff.md").read_text("utf-8"))
        self.assertEqual(fm["status"], "active")
        self.assertEqual(fm["aliases"], ["K8s CrashLoop", "CLBO"])
        self.assertEqual(fm["severity"], "sev2")


if __name__ == "__main__":
    unittest.main()



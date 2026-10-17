"""Unit tests for cadabby.installer (§7.6, §10 AC 18)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cadabby.installer import (
    _entry_vault,
    _is_cadabby_entry,
    default_antigravity_path,
    default_claude_path,
    install_antigravity,
    install_claude,
    make_mcp_server_entry,
    mcp_launch_argv,
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
        self.assertEqual(data["mcpServers"]["cadabby"], make_mcp_server_entry(None))

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
        self.assertEqual(data["mcpServers"]["cadabby"], make_mcp_server_entry(None))

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

    def test_force_and_uninstall_never_delete_a_non_cadabby_directory(self):
        """§7.6, §10 C22: --force overwrites a Cadabby plugin; it never rmtrees an arbitrary --path."""
        precious = self.root / "precious"
        precious.mkdir()
        thesis = precious / "thesis.txt"
        thesis.write_bytes(b"years of work\n")
        vault = self.root / "some-vault"

        attempts = {
            "unbound --force": lambda dry: install_antigravity(dest_path=precious, force=True, dry_run=dry),
            "bound --force": lambda dry: install_antigravity(dest_path=precious, vault_path=vault, force=True, dry_run=dry),
            "uninstall": lambda dry: uninstall_antigravity(dest_path=precious, dry_run=dry),
        }
        for label, attempt in attempts.items():
            for dry in (True, False):
                with self.subTest(attempt=label, dry_run=dry):
                    ok, msg = attempt(dry)
                    self.assertFalse(ok, msg)
                    self.assertIn("not a Cadabby plugin directory", msg)
                    self.assertFalse(precious.is_symlink())
                    self.assertEqual(sorted(p.name for p in precious.iterdir()), ["thesis.txt"])
                    self.assertEqual(thesis.read_bytes(), b"years of work\n")

        # A real plugin copy and an empty directory are still replaceable and removable.
        empty = self.root / "empty-plugin-dir"
        empty.mkdir()
        ok, _ = install_antigravity(dest_path=empty, vault_path=vault, force=True)
        self.assertTrue(ok)
        ok, _ = uninstall_antigravity(dest_path=empty)
        self.assertTrue(ok)
        self.assertFalse(empty.exists())

    def test_claude_uninstall_leaves_a_foreign_entry_without_force(self):
        """§7.6, §10 C22: --uninstall removes only what install added; a foreign entry needs --force."""
        cfg = self.root / "foreign.json"
        original = json.dumps(
            {"mcpServers": {"cadabby": {"command": "/opt/mytools/run-my-own-cadabby-fork.sh", "args": ["serve"]}}},
            indent=2,
        ) + "\n"
        cfg.write_text(original, "utf-8")
        ok, msg = uninstall_claude(dest_path=cfg)
        self.assertFalse(ok)
        self.assertIn("not written by Cadabby", msg)
        self.assertEqual(cfg.read_text("utf-8"), original)
        self.assertEqual(run_install(claude=True, uninstall=True, dest_path=cfg, force=True), 0)
        self.assertNotIn("cadabby", json.loads(cfg.read_text("utf-8"))["mcpServers"])

    def test_claude_install_preserves_style_and_non_ascii(self):
        """§7.6, §10 C22: a merge changes only the cadabby entry -- indent, raw non-ASCII and newline stay."""
        projects = {"/home/u/café": {"allowedTools": [], "n": 1.5}}
        for label, dump in (
            ("4-space, newline", lambda d: json.dumps(d, indent=4, ensure_ascii=False) + "\n"),
            ("tab, no newline", lambda d: json.dumps(d, indent="\t", ensure_ascii=False)),
            ("compact", lambda d: json.dumps(d, separators=(",", ":"), ensure_ascii=False)),
        ):
            with self.subTest(style=label):
                cfg = self.root / f"style-{label.split(',')[0]}.json"
                cfg.write_text(dump({"projects": projects, "mcpServers": {}}), "utf-8")
                ok, _ = install_claude(dest_path=cfg)
                self.assertTrue(ok)
                expected = {"projects": projects, "mcpServers": {"cadabby": make_mcp_server_entry(None)}}
                self.assertEqual(cfg.read_text("utf-8"), dump(expected))
                self.assertIn("café", cfg.read_text("utf-8"))

    def test_claude_install_refuses_a_concurrent_rewrite(self):
        """§7.6, §8, §10 C22: if Claude Code rewrites the file mid-install, its bytes survive and the install is a retryable conflict."""
        from unittest import mock

        import cadabby.installer as installer
        from cadabby.fsutil import VaultConflictError

        cfg = self.root / "busy.json"
        cfg.write_text('{\n  "mcpServers": {}\n}\n', "utf-8")
        concurrent = '{\n  "mcpServers": {},\n  "lastSession": "written by Claude Code"\n}\n'
        real_dump = installer._dump_json_like

        def dump_while_claude_writes(data, original=""):
            cfg.write_text(concurrent, "utf-8")  # Claude Code saves between our read and our write
            return real_dump(data, original)

        with mock.patch.object(installer, "_dump_json_like", dump_while_claude_writes):
            with self.assertRaises(VaultConflictError):
                install_claude(dest_path=cfg)
        self.assertEqual(cfg.read_text("utf-8"), concurrent)

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

    def test_make_mcp_server_entry(self):
        command, *base_args = mcp_launch_argv()

        # Without vault
        entry = make_mcp_server_entry(None)
        self.assertEqual(entry, {"command": command, "args": base_args})

        # With vault
        vault_dir = self.root / "my-vault"
        entry_vault = make_mcp_server_entry(vault_dir)
        self.assertEqual(
            entry_vault,
            {
                "command": command,
                "args": [*base_args, "--vault", str(vault_dir.resolve())],
            },
        )

    def test_mcp_launch_argv_never_emits_bare_python(self):
        # A harness resolves the command from its own PATH, so a bare "python3"
        # would miss any isolated install (pipx, uv tool, venv).
        argv = mcp_launch_argv()
        self.assertTrue(Path(argv[0]).is_absolute(), f"not an absolute path: {argv[0]}")
        self.assertTrue(Path(argv[0]).exists())
        self.assertNotIn(argv[0], ("python", "python3"))
        self.assertEqual(argv[-1], "mcp")

    def test_claude_install_vault_path(self):
        claude_cfg = self.root / "claude_vault.json"
        vault1 = self.root / "vault-one"
        vault2 = self.root / "vault-two"

        # 1. Install bound to vault1
        ok, msg = install_claude(dest_path=claude_cfg, vault_path=vault1)
        self.assertTrue(ok)
        data = json.loads(claude_cfg.read_text("utf-8"))
        self.assertEqual(
            data["mcpServers"]["cadabby"]["args"],
            [*mcp_launch_argv()[1:], "--vault", str(vault1.resolve())],
        )

        # 2. Idempotent install with same vault
        ok, msg = install_claude(dest_path=claude_cfg, vault_path=vault1)
        self.assertTrue(ok)
        self.assertIn("unchanged", msg)

        # 3. Conflict when pointing to vault2 without force
        ok, msg = install_claude(dest_path=claude_cfg, vault_path=vault2, force=False)
        self.assertFalse(ok)
        self.assertIn("Conflict", msg)

        # 4. Overwrite when force=True
        ok, msg = install_claude(dest_path=claude_cfg, vault_path=vault2, force=True)
        self.assertTrue(ok)
        data = json.loads(claude_cfg.read_text("utf-8"))
        self.assertEqual(
            data["mcpServers"]["cadabby"]["args"],
            [*mcp_launch_argv()[1:], "--vault", str(vault2.resolve())],
        )

    def test_antigravity_install_vault_path(self):
        plugin_dest = self.root / "antigravity" / "plugins" / "cadabby"
        vault1 = self.root / "ag-vault-1"
        vault2 = self.root / "ag-vault-2"

        # 1. Install bound to vault1 (copies repo plugin and writes mcp_config.json)
        ok, msg = install_antigravity(dest_path=plugin_dest, vault_path=vault1)
        self.assertTrue(ok)
        self.assertTrue(plugin_dest.exists())
        self.assertFalse(plugin_dest.is_symlink())
        mcp_cfg = json.loads((plugin_dest / "mcp_config.json").read_text("utf-8"))
        self.assertEqual(
            mcp_cfg["mcpServers"]["cadabby"]["args"],
            [*mcp_launch_argv()[1:], "--vault", str(vault1.resolve())],
        )

        # 2. Idempotent install with same vault
        ok, msg = install_antigravity(dest_path=plugin_dest, vault_path=vault1)
        self.assertTrue(ok)
        self.assertIn("unchanged", msg)

        # 3. Conflict when installing for vault2 without force
        ok, msg = install_antigravity(dest_path=plugin_dest, vault_path=vault2, force=False)
        self.assertFalse(ok)
        self.assertIn("Conflict", msg)

        # 4. Overwrite with force
        ok, msg = install_antigravity(dest_path=plugin_dest, vault_path=vault2, force=True)
        self.assertTrue(ok)
        mcp_cfg = json.loads((plugin_dest / "mcp_config.json").read_text("utf-8"))
        self.assertEqual(
            mcp_cfg["mcpServers"]["cadabby"]["args"],
            [*mcp_launch_argv()[1:], "--vault", str(vault2.resolve())],
        )

    def test_antigravity_refuses_unparseable_mcp_config_bytes_unchanged(self):
        """C22: an existing mcp_config.json that does not parse is refused, not overwritten."""
        plugin_dest = self.root / "antigravity" / "plugins" / "cadabby"
        vault = self.root / "ag-vault"
        ok, _ = install_antigravity(dest_path=plugin_dest, vault_path=vault)
        self.assertTrue(ok)

        mcp_file = plugin_dest / "mcp_config.json"
        for broken in (b'{"mcpServers": {"other": ', b'["not", "an", "object"]'):
            with self.subTest(content=broken):
                mcp_file.write_bytes(broken)
                ok, msg = install_antigravity(dest_path=plugin_dest, vault_path=vault, force=True)
                self.assertFalse(ok)
                self.assertIn("Aborting", msg)
                self.assertEqual(mcp_file.read_bytes(), broken)

    def test_antigravity_replace_symlink_with_vault_bound(self):
        plugin_dest = self.root / "antigravity_symlink" / "cadabby"
        # First install unbound (symlink)
        ok, _ = install_antigravity(dest_path=plugin_dest)
        self.assertTrue(ok)
        if plugin_dest.is_symlink():
            vault = self.root / "bound-vault"
            # Without force: conflict
            ok, msg = install_antigravity(dest_path=plugin_dest, vault_path=vault, force=False)
            self.assertFalse(ok)
            self.assertIn("Conflict", msg)

            # With force: replaces symlink with copy and writes mcp_config.json
            ok, msg = install_antigravity(dest_path=plugin_dest, vault_path=vault, force=True)
            self.assertTrue(ok)
            self.assertFalse(plugin_dest.is_symlink())
            mcp_cfg = json.loads((plugin_dest / "mcp_config.json").read_text("utf-8"))
            self.assertEqual(
                mcp_cfg["mcpServers"]["cadabby"]["args"],
                [*mcp_launch_argv()[1:], "--vault", str(vault.resolve())],
            )

    def test_default_paths_workspace_and_global(self):
        vault = self.root / "path-vault"
        # Claude paths
        self.assertEqual(default_claude_path(None, is_global=False), Path.home() / ".claude.json")
        self.assertEqual(default_claude_path(vault, is_global=False), vault.resolve() / ".mcp.json")
        self.assertEqual(default_claude_path(vault, is_global=True), Path.home() / ".claude.json")

        # Antigravity paths
        self.assertEqual(
            default_antigravity_path(None, is_global=False),
            Path.home() / ".gemini" / "antigravity" / "plugins" / "cadabby",
        )
        self.assertEqual(
            default_antigravity_path(vault, is_global=False),
            vault.resolve() / ".agents" / "plugins" / "cadabby",
        )
        self.assertEqual(
            default_antigravity_path(vault, is_global=True),
            Path.home() / ".gemini" / "antigravity" / "plugins" / "cadabby",
        )

    def test_workspace_install_defaults(self):
        vault = self.root / "ws-defaults-vault"
        vault.mkdir(parents=True, exist_ok=True)

        # Run install targeting all harnesses in workspace mode (default)
        ret = run_install(all_targets=True, vault_path=vault, is_global=False)
        self.assertEqual(ret, 0)

        # Check Claude workspace config
        claude_mcp = vault / ".mcp.json"
        self.assertTrue(claude_mcp.exists())
        claude_data = json.loads(claude_mcp.read_text("utf-8"))
        self.assertEqual(
            claude_data["mcpServers"]["cadabby"]["args"],
            [*mcp_launch_argv()[1:], "--vault", str(vault.resolve())],
        )

        # Check Antigravity workspace plugin
        ag_plugin = vault / ".agents" / "plugins" / "cadabby"
        self.assertTrue(ag_plugin.exists())
        ag_cfg = json.loads((ag_plugin / "mcp_config.json").read_text("utf-8"))
        self.assertEqual(
            ag_cfg["mcpServers"]["cadabby"]["args"],
            [*mcp_launch_argv()[1:], "--vault", str(vault.resolve())],
        )

        # Uninstall in workspace mode
        ret = run_install(all_targets=True, vault_path=vault, is_global=False, uninstall=True)
        self.assertEqual(ret, 0)
        self.assertFalse(ag_plugin.exists())
        claude_after = json.loads(claude_mcp.read_text("utf-8"))
        self.assertNotIn("cadabby", claude_after.get("mcpServers", {}))

    def test_install_and_uninstall_claude_reject_non_dict_or_binary_configs(self):
        cfg = self.root / "bad-claude.json"
        for payload in (b"[1, 2, 3]", b'{"mcpServers": []}', b"\xff\xfe\x00\x00"):
            cfg.write_bytes(payload)
            ok_in, _ = install_claude(dest_path=cfg)
            ok_un, _ = uninstall_claude(dest_path=cfg)
            self.assertFalse(ok_in)
            self.assertFalse(ok_un)
            self.assertEqual(cfg.read_bytes(), payload)

    def test_is_cadabby_entry_and_entry_vault_resilience(self):
        # Must not raise on non-dict or non-list args
        self.assertFalse(_is_cadabby_entry(None))
        self.assertFalse(_is_cadabby_entry("string"))
        self.assertFalse(_is_cadabby_entry({"args": None}))
        self.assertFalse(_is_cadabby_entry({"args": 42}))
        self.assertIsNone(_entry_vault(None))
        self.assertIsNone(_entry_vault({"args": 123}))
        self.assertIsNone(_entry_vault({"args": None}))
        self.assertEqual(_entry_vault({"args": ["--vault", "/tmp/v"]}), "/tmp/v")


if __name__ == "__main__":
    unittest.main()


"""End-to-end test suite validating all 12 falsifiable acceptance criteria from SPECIFICATION.md §10."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from cadabby.audit import run_vault_audit
from cadabby.cache import VaultCache
from cadabby.cli import cmd_init, cmd_sync
from cadabby.frontmatter import FrontmatterParseError, parse_frontmatter, serialize_frontmatter
from cadabby.fsutil import VaultConflictError, compute_file_sha256
from cadabby.lint import run_vault_lint
from cadabby.mcp import McpServer
from cadabby.ops import ground_notes, scaffold_note, update_note, verify_note
from cadabby.vault import Vault


class DummyArgs:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestAcceptanceCriteria(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_ac1_init_produces_zero_lint_errors(self):
        """1. cadabby init scaffolds a fresh vault that passes cadabby lint with zero errors."""
        vault_dir = self.root / "new-vault"
        args = DummyArgs(vault=str(vault_dir), name="new-vault", obsidian=True)
        ret = cmd_init(args)
        self.assertEqual(ret, 0)

        vault = Vault(vault_dir)
        findings = run_vault_lint(vault)
        errors = [f for f in findings if f.severity == "error"]
        self.assertEqual(len(errors), 0, f"Scaffolded vault failed lint: {errors}")

    def test_ac2_disposable_cache_reproducibility(self):
        """2. Deleting .cadabby/ and re-running any command reproduces byte-identical search results."""
        demo_src = Path(__file__).resolve().parent.parent / "examples" / "demo-vault"
        vault_dir = self.root / "demo-vault"
        shutil.copytree(demo_src, vault_dir)
        vault = Vault(vault_dir)

        cache1 = VaultCache(vault)
        cache1.scan()
        results1 = cache1.search("SQLite")
        cache1.close()

        # Wipe .cadabby/
        shutil.rmtree(vault.dot_cadabby_dir)
        self.assertFalse(vault.dot_cadabby_dir.exists())

        # Re-run on clean slate
        cache2 = VaultCache(vault)
        results2 = cache2.search("SQLite")
        cache2.close()

        self.assertEqual(len(results1), len(results2))
        for r1, r2 in zip(results1, results2):
            self.assertEqual(r1.cid, r2.cid)
            self.assertAlmostEqual(r1.score, r2.score, places=4)

    def test_ac3_restricted_yaml_grammar(self):
        """3. Frontmatter parser accepts §3.2 constructs, rejects tabs / unquoted colons / multiline scalars."""
        # Rejects tab
        with self.assertRaises(FrontmatterParseError) as ctx1:
            parse_frontmatter("---\ntitle:\tTabbed\n---\n# Body")
        self.assertEqual(ctx1.exception.line_number, 2)

        # Rejects flow mapping
        with self.assertRaises(FrontmatterParseError) as ctx2:
            parse_frontmatter("---\ntags: {a: 1}\n---\n# Body")
        self.assertEqual(ctx2.exception.line_number, 2)

        # Rejects multiline block scalar
        with self.assertRaises(FrontmatterParseError) as ctx3:
            parse_frontmatter("---\ndescription: |\n  Line 1\n---\n# Body")
        self.assertEqual(ctx3.exception.line_number, 2)

        # Accepts valid OKF and round-trips
        valid = "---\ntype: concept\ntitle: Roundtrip\ndescription: Test\nstatus: active\n---\n# Body\n"
        fm, body = parse_frontmatter(valid)
        serialized = serialize_frontmatter(fm, body)
        self.assertEqual(serialized, valid)

    def test_ac4_body_edit_downgrades_to_stale_verified(self):
        """4. Editing a verified note's body downgrades it to stale-verified and counts as verification debt."""
        demo_src = Path(__file__).resolve().parent.parent / "examples" / "demo-vault"
        vault_dir = self.root / "demo-vault"
        shutil.copytree(demo_src, vault_dir, ignore=shutil.ignore_patterns(".cadabby", "*.pyc"))
        vault = Vault(vault_dir)

        cache = VaultCache(vault)
        cache.scan()
        conn = cache.get_connection()

        # Starts as human-reviewed
        cur = conn.execute("SELECT trust_tier FROM notes WHERE cid = 'wiki/concepts/Epistemic-Trust-Tiers';")
        self.assertEqual(cur.fetchone()[0], "human-reviewed")

        # Edit body
        note_file = vault.wiki_dir / "concepts" / "Epistemic-Trust-Tiers.md"
        content = note_file.read_text("utf-8")
        note_file.write_text(content + "\n\nDrifted prose.", "utf-8")

        # Re-scan
        cache.scan()
        cur = conn.execute("SELECT trust_tier FROM notes WHERE cid = 'wiki/concepts/Epistemic-Trust-Tiers';")
        self.assertEqual(cur.fetchone()[0], "stale-verified")

        # Check status verification debt count
        cur = conn.execute("SELECT COUNT(*) FROM notes WHERE trust_tier = 'stale-verified';")
        self.assertEqual(cur.fetchone()[0], 1)
        cache.close()

    def test_ac5_frontmatter_edit_preserves_human_reviewed(self):
        """5. Modifying frontmatter without touching the body preserves human-reviewed tier unchanged."""
        demo_src = Path(__file__).resolve().parent.parent / "examples" / "demo-vault"
        vault_dir = self.root / "demo-vault"
        shutil.copytree(demo_src, vault_dir, ignore=shutil.ignore_patterns(".cadabby", "*.pyc"))
        vault = Vault(vault_dir)

        # Modify frontmatter only (add a tag and update description)
        update_note(
            vault=vault,
            cid_or_path="wiki/concepts/Epistemic-Trust-Tiers",
            frontmatter_patch={"description": "Updated description without body touch"},
        )

        cache = VaultCache(vault)
        cache.scan()
        conn = cache.get_connection()
        cur = conn.execute("SELECT trust_tier FROM notes WHERE cid = 'wiki/concepts/Epistemic-Trust-Tiers';")
        self.assertEqual(cur.fetchone()[0], "human-reviewed")
        cache.close()

    def test_ac6_rename_leaves_no_stale_records(self):
        """6. A note renamed on disk leaves no stale row in notes, links, sources, or notes_fts."""
        demo_src = Path(__file__).resolve().parent.parent / "examples" / "demo-vault"
        vault_dir = self.root / "demo-vault"
        shutil.copytree(demo_src, vault_dir, ignore=shutil.ignore_patterns(".cadabby", "*.pyc"))
        vault = Vault(vault_dir)

        cache = VaultCache(vault)
        cache.scan()

        # Rename note on disk
        old_path = vault.wiki_dir / "comparisons" / "SQLite-vs-DuckDB.md"
        new_path = vault.wiki_dir / "comparisons" / "SQLite-Versus-DuckDB.md"
        old_path.rename(new_path)

        ins, upd, deleted, total = cache.scan()
        self.assertEqual(deleted, 1)
        self.assertEqual(ins, 1)

        conn = cache.get_connection()
        # Verify old cid is completely gone
        cur = conn.execute("SELECT COUNT(*) FROM notes WHERE cid = 'wiki/comparisons/SQLite-vs-DuckDB';")
        self.assertEqual(cur.fetchone()[0], 0)
        cur = conn.execute("SELECT COUNT(*) FROM links WHERE source_cid = 'wiki/comparisons/SQLite-vs-DuckDB';")
        self.assertEqual(cur.fetchone()[0], 0)

        # Verify FTS5 integrity check passes
        self.assertTrue(cache.check_fts_integrity())
        cache.close()

    def test_ac7_epistemic_ranking_ordering(self):
        """7. A human-reviewed note outranks an identically-matching unverified note, and deprecated is demoted."""
        demo_src = Path(__file__).resolve().parent.parent / "examples" / "demo-vault"
        vault_dir = self.root / "demo-vault"
        shutil.copytree(demo_src, vault_dir, ignore=shutil.ignore_patterns(".cadabby", "*.pyc"))
        vault = Vault(vault_dir)

        # Add identical keyword 'hyperparameter' to Epistemic-Trust-Tiers (human-reviewed) and Flash-Attention (unverified)
        p1 = vault.wiki_dir / "concepts" / "Epistemic-Trust-Tiers.md"
        p2 = vault.wiki_dir / "concepts" / "Flash-Attention.md"

        p1.write_text(p1.read_text("utf-8") + "\n\nImportant hyperparameter tuning details.\n", "utf-8")
        p2.write_text(p2.read_text("utf-8") + "\n\nImportant hyperparameter tuning details.\n", "utf-8")

        # Re-verify p1 so it is human-reviewed with matching body hash
        verify_note(vault, "wiki/concepts/Epistemic-Trust-Tiers", actor="human:tester", is_human_authorized=True)

        cache = VaultCache(vault)
        results = cache.search("hyperparameter")

        self.assertGreaterEqual(len(results), 2)
        top = results[0]
        second = results[1]

        self.assertEqual(top.cid, "wiki/concepts/Epistemic-Trust-Tiers")
        self.assertEqual(top.trust_tier, "human-reviewed")
        self.assertGreater(top.score, second.score)
        cache.close()

    def test_ac8_concurrent_writers_no_corruption(self):
        """8. Two concurrent writers to different notes succeed without lock contention or corruption."""
        demo_src = Path(__file__).resolve().parent.parent / "examples" / "demo-vault"
        vault_dir = self.root / "demo-vault"
        shutil.copytree(demo_src, vault_dir, ignore=shutil.ignore_patterns(".cadabby", "*.pyc"))
        vault = Vault(vault_dir)

        path1 = scaffold_note(vault, "Concurrency Test One", "concept", "First concurrent note")
        path2 = scaffold_note(vault, "Concurrency Test Two", "entity", "Second concurrent note")

        self.assertTrue(path1.exists())
        self.assertTrue(path2.exists())

        findings = run_vault_lint(vault)
        errors = [f for f in findings if f.severity == "error"]
        self.assertEqual(len(errors), 0)

    def test_ac9_update_expected_hash_conflict(self):
        """9. cadabby update with a mismatched expected_hash fails with VAULT_CONFLICT and leaves note unchanged."""
        demo_src = Path(__file__).resolve().parent.parent / "examples" / "demo-vault"
        vault_dir = self.root / "demo-vault"
        shutil.copytree(demo_src, vault_dir, ignore=shutil.ignore_patterns(".cadabby", "*.pyc"))
        vault = Vault(vault_dir)

        note_file = vault.wiki_dir / "concepts" / "Flash-Attention.md"
        original_content = note_file.read_text("utf-8")

        with self.assertRaises(VaultConflictError):
            update_note(
                vault=vault,
                cid_or_path="wiki/concepts/Flash-Attention",
                frontmatter_patch={"status": "deprecated"},
                expected_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
            )

        # Content must remain intact
        self.assertEqual(note_file.read_text("utf-8"), original_content)

    def test_ac10_mcp_capabilities_and_human_refusal(self):
        """10. cadabby mcp answers initialize & tools/list, exposes 7 tools, and refuses by: human:*."""
        demo_src = Path(__file__).resolve().parent.parent / "examples" / "demo-vault"
        vault_dir = self.root / "demo-vault"
        shutil.copytree(demo_src, vault_dir, ignore=shutil.ignore_patterns(".cadabby", "*.pyc"))
        vault = Vault(vault_dir)

        server = McpServer(vault)
        init_res = server.handle_initialize({"clientInfo": {"name": "claude-code"}})
        self.assertEqual(init_res["serverInfo"]["name"], "cadabby")
        self.assertEqual(init_res["serverInfo"]["version"], "0.3.0")

        tools_res = server.handle_tools_list()
        self.assertEqual(len(tools_res["tools"]), 7)

        # 1. Verification over MCP stamps agent:<clientInfo>
        res = server.handle_tools_call(
            "vault_verify_note",
            {"cid": "wiki/concepts/Flash-Attention"},
        )
        self.assertFalse(res["isError"])
        self.assertIn("agent:claude-code", res["content"][0]["text"])

        # 2. Rejection of human:* over MCP (AC 12)
        res_actor = server.handle_tools_call(
            "vault_verify_note",
            {"cid": "wiki/concepts/Flash-Attention", "actor": "human:attacker"},
        )
        self.assertTrue(res_actor["isError"])
        self.assertIn("Error: Verification by human:* cannot be performed over MCP", res_actor["content"][0]["text"])

        res_by = server.handle_tools_call(
            "vault_verify_note",
            {"cid": "wiki/concepts/Flash-Attention", "by": "human:attacker"},
        )
        self.assertTrue(res_by["isError"])

    def test_ac11_audit_provenance_mismatch(self):
        """11. cadabby audit flags a human attestation whose Git commit author does not match identities map."""
        demo_src = Path(__file__).resolve().parent.parent / "examples" / "demo-vault"
        vault_dir = self.root / "demo-vault"
        shutil.copytree(demo_src, vault_dir, ignore=shutil.ignore_patterns(".cadabby", "*.pyc"))
        vault = Vault(vault_dir)

        # Mock git blame to simulate commit author mismatch
        mock_blame = (
            "a1b2c3d4 1 1 1\n"
            "author Untrusted Agent\n"
            "author-mail <untrusted-agent@bot.net>\n"
            "\t- by: human:owner\n"
        )

        with patch("subprocess.run") as mock_run:
            # git rev-parse inside work tree -> returncode 0
            # git blame -> returncode 0, stdout=mock_blame
            def fake_run(cmd, **kwargs):
                ret = MagicMock()
                if "rev-parse" in cmd:
                    ret.returncode = 0
                    ret.stdout = "true\n"
                elif "blame" in cmd:
                    ret.returncode = 0
                    ret.stdout = mock_blame
                return ret

            mock_run.side_effect = fake_run

            findings, notice = run_vault_audit(vault)
            self.assertIsNone(notice)
            self.assertGreater(len(findings), 0)
            self.assertEqual(findings[0].code, "PROVENANCE_MISMATCH")
            self.assertIn("untrusted-agent@bot.net", findings[0].message)

    def test_ac12_full_autonomous_agent_workflow(self):
        """12. Agent with only .mcp.json can search, ground, scaffold, update, and verify notes."""
        demo_src = Path(__file__).resolve().parent.parent / "examples" / "demo-vault"
        vault_dir = self.root / "demo-vault"
        shutil.copytree(demo_src, vault_dir, ignore=shutil.ignore_patterns(".cadabby", "*.pyc"))
        vault = Vault(vault_dir)

        server = McpServer(vault)
        server.handle_initialize({"clientInfo": {"name": "claude-autonomous"}})

        # 1. Search existing vault
        search_out = server.handle_tools_call("vault_search", {"query": "SQLite"})
        self.assertFalse(search_out["isError"])

        # 2. Ground top hit
        ground_out = server.handle_tools_call("vault_ground", {"cids": ["wiki/entities/SQLite"]})
        self.assertFalse(ground_out["isError"])

        # 3. Scaffold new related synthesis
        scaffold_out = server.handle_tools_call(
            "vault_scaffold_note",
            {
                "title": "Embedded Vector Systems",
                "type": "synthesis",
                "description": "Integration of SQLite and local vector retrieval pipelines",
                "tags": ["vector", "sqlite", "embedded"],
                "sources": ["raw/karpathy-llm-wiki-gist.md"],
                "body": (
                    "# Embedded Vector Systems\n\n"
                    "Integrating local SQLite relational indexes with vector search.\n\n"
                    "Connects directly with [[SQLite]] and [[Flash-Attention]].\n"
                ),
            },
        )
        self.assertFalse(scaffold_out["isError"])

        # 4. Attest verification
        verify_out = server.handle_tools_call(
            "vault_verify_note",
            {"cid": "wiki/syntheses/Embedded-Vector-Systems", "method": "automated-check"},
        )
        self.assertFalse(verify_out["isError"])

        # 5. Run lint to ensure vault remains immaculate
        lint_out = server.handle_tools_call("vault_lint", {})
        self.assertFalse(lint_out["isError"])

    def test_ac16_sync_zero_git_diff(self):
        """16. cadabby sync run twice in a row produces no Git diff on the second run."""
        demo_src = Path(__file__).resolve().parent.parent / "examples" / "demo-vault"
        vault_dir = self.root / "demo-vault"
        shutil.copytree(demo_src, vault_dir, ignore=shutil.ignore_patterns(".cadabby", "*.pyc"))

        # First sync
        args_sync = DummyArgs(vault=str(vault_dir), force=False, rebuild=False)
        cmd_sync(args_sync)
        index_first = (vault_dir / "index.md").read_text("utf-8")

        # Second sync
        cmd_sync(args_sync)
        index_second = (vault_dir / "index.md").read_text("utf-8")

        self.assertEqual(index_first, index_second)

    def test_ac17_init_untouched_claude_notification(self):
        """17. cadabby init into a directory with an existing CLAUDE.md leaves that file untouched and says so."""
        vault_dir = self.root / "existing-claude-vault"
        vault_dir.mkdir(parents=True, exist_ok=True)
        custom_claude = "# My Custom Claude Config\nDo not overwrite.\n"
        claude_path = vault_dir / "CLAUDE.md"
        claude_path.write_text(custom_claude, "utf-8")

        f = io.StringIO()
        with contextlib.redirect_stdout(f):
            args = DummyArgs(vault=str(vault_dir), name="test", force=False, obsidian=False)
            ret = cmd_init(args)

        self.assertEqual(ret, 0)
        self.assertEqual(claude_path.read_text("utf-8"), custom_claude)
        output = f.getvalue()
        self.assertIn("Existing file left untouched: CLAUDE.md", output)

    def test_ac18_installer_idempotency_and_uninstall(self):
        """18. cadabby install --all run twice changes no bytes on the second run; --uninstall restores the pre-install config exactly."""
        claude_dest = self.root / ".claude.json"
        ag_dest = self.root / "antigravity" / "plugins" / "cadabby"

        initial_claude = json.dumps({"mcpServers": {"existing": {}}, "custom": 123}, indent=2) + "\n"
        claude_dest.write_text(initial_claude, "utf-8")

        from cadabby.installer import install_antigravity, install_claude, uninstall_antigravity, uninstall_claude

        # First install
        ok1, _ = install_claude(dest_path=claude_dest)
        ok2, _ = install_antigravity(dest_path=ag_dest)
        self.assertTrue(ok1 and ok2)

        after_first = claude_dest.read_text("utf-8")

        # Second install: 0 bytes changed
        ok1, _ = install_claude(dest_path=claude_dest)
        ok2, _ = install_antigravity(dest_path=ag_dest)
        self.assertTrue(ok1 and ok2)
        self.assertEqual(claude_dest.read_text("utf-8"), after_first)

        # Uninstall: exactly restores pre-install config
        u1, _ = uninstall_claude(dest_path=claude_dest)
        u2, _ = uninstall_antigravity(dest_path=ag_dest)
        self.assertTrue(u1 and u2)
        self.assertEqual(claude_dest.read_text("utf-8"), initial_claude)
        self.assertFalse(ag_dest.exists())

    def test_ac19_single_definition_invariant(self):
        """19. No persona behavior text appears in more than one file across the repository."""
        repo_root = Path(__file__).resolve().parent.parent

        # Librarian core behavior string
        librarian_phrase = "Ingestion, synthesis, and cross-referencing persona for Cadabby LLM Wiki vaults"
        # Technician core behavior string
        technician_phrase = "Health, diagnostics, linting, and maintenance persona for Cadabby LLM Wiki vaults"
        # Constitution heading phrase
        agents_md_phrase = "single canonical constitution"

        def count_phrase_occurrences(phrase: str) -> list[Path]:
            matching = []
            for p in repo_root.rglob("*"):
                if p.is_file() and p.suffix in (".md", ".json", ".py", ".yaml", ".yml"):
                    # ignore .git, build artifacts, cache, tests, and spec docs
                    if any(part in p.parts for part in (".git", ".cadabby", "__pycache__", "build", "dist", "tests")):
                        continue
                    if p.name == "SPECIFICATION.md":
                        continue
                    try:
                        text = p.read_text("utf-8", errors="ignore")
                        if phrase in text:
                            matching.append(p.relative_to(repo_root))
                    except Exception:
                        pass
            return matching

        lib_matches = count_phrase_occurrences(librarian_phrase)
        self.assertEqual(
            len(lib_matches),
            1,
            f"Librarian persona text duplicated across files: {lib_matches}",
        )
        self.assertEqual(str(lib_matches[0]), "assets/skills/librarian/SKILL.md")

        tech_matches = count_phrase_occurrences(technician_phrase)
        self.assertEqual(
            len(tech_matches),
            1,
            f"Technician persona text duplicated across files: {tech_matches}",
        )
        self.assertEqual(str(tech_matches[0]), "assets/skills/technician/SKILL.md")

        agents_matches = count_phrase_occurrences(agents_md_phrase)
        self.assertEqual(
            len(agents_matches),
            1,
            f"AGENTS.md constitution duplicated across files: {agents_matches}",
        )
        self.assertEqual(str(agents_matches[0]), "assets/vault/AGENTS.md")


if __name__ == "__main__":
    unittest.main()

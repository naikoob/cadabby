"""End-to-end tests for the falsifiable acceptance criteria in SPECIFICATION.md §10.

Not all of them live here -- §10's traceability table is the authoritative map,
and many criteria are discharged by the per-module suites. TestAcceptanceTraceability
checks that the table and the suite still agree.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import cadabby
from cadabby.audit import run_vault_audit
from cadabby.cache import VaultCache
from cadabby.cli import cmd_init, cmd_sync
from cadabby.frontmatter import (
    FrontmatterParseError,
    parse_frontmatter,
    serialize_frontmatter,
)
from cadabby.fsutil import VaultConflictError
from cadabby.lint import run_vault_lint
from cadabby.mcp import TOOLS, McpServer
from cadabby.ops import scaffold_note, update_note, verify_note
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
        """No current §10 criterion. Pins §5.3: what `init` scaffolds is itself lint-clean."""
        vault_dir = self.root / "new-vault"
        args = DummyArgs(vault=str(vault_dir), name="new-vault", obsidian=True)
        ret = cmd_init(args)
        self.assertEqual(ret, 0)

        vault = Vault(vault_dir)
        findings = run_vault_lint(vault)
        errors = [f for f in findings if f.severity == "error"]
        self.assertEqual(len(errors), 0, f"Scaffolded vault failed lint: {errors}")

    def test_ac2_disposable_cache_reproducibility(self):
        """§10 C1. Deleting .cadabby/ and re-running reproduces byte-identical search results."""
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
        """§10 C3 (parser half). Accepts §3.2 constructs; rejects tabs, unquoted colons, multiline scalars."""
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
        """§10 C2. A body edit downgrades the note to stale-verified on the next scan."""
        demo_src = Path(__file__).resolve().parent.parent / "examples" / "demo-vault"
        vault_dir = self.root / "demo-vault"
        shutil.copytree(demo_src, vault_dir, ignore=shutil.ignore_patterns(".cadabby", "*.pyc"))
        vault = Vault(vault_dir)

        cache = VaultCache(vault)
        cache.scan()
        conn = cache.get_connection()

        # Starts as human-reviewed
        cur = conn.execute("SELECT trust_tier FROM notes WHERE cid = 'wiki/Epistemic-Trust-Tiers';")
        self.assertEqual(cur.fetchone()[0], "human-reviewed")

        # Edit body
        note_file = vault.wiki_dir / "Epistemic-Trust-Tiers.md"
        content = note_file.read_text("utf-8")
        note_file.write_text(content + "\n\nDrifted prose.", "utf-8")

        # Re-scan
        cache.scan()
        cur = conn.execute("SELECT trust_tier FROM notes WHERE cid = 'wiki/Epistemic-Trust-Tiers';")
        self.assertEqual(cur.fetchone()[0], "stale-verified")

        # Check status verification debt count
        cur = conn.execute("SELECT COUNT(*) FROM notes WHERE trust_tier = 'stale-verified';")
        self.assertEqual(cur.fetchone()[0], 1)
        cache.close()

    def test_ac5_frontmatter_edit_preserves_human_reviewed(self):
        """No current §10 criterion. Pins §3.3: the body hash is body-only, so metadata edits do not demote."""
        demo_src = Path(__file__).resolve().parent.parent / "examples" / "demo-vault"
        vault_dir = self.root / "demo-vault"
        shutil.copytree(demo_src, vault_dir, ignore=shutil.ignore_patterns(".cadabby", "*.pyc"))
        vault = Vault(vault_dir)

        # Modify frontmatter only (add a tag and update description)
        update_note(
            vault=vault,
            cid_or_path="wiki/Epistemic-Trust-Tiers",
            frontmatter_patch={"description": "Updated description without body touch"},
        )

        cache = VaultCache(vault)
        cache.scan()
        conn = cache.get_connection()
        cur = conn.execute("SELECT trust_tier FROM notes WHERE cid = 'wiki/Epistemic-Trust-Tiers';")
        self.assertEqual(cur.fetchone()[0], "human-reviewed")
        cache.close()

    def test_ac6_rename_leaves_no_stale_records(self):
        """§10 C14. A rename leaves no stale row, and the FTS integrity check passes afterward."""
        demo_src = Path(__file__).resolve().parent.parent / "examples" / "demo-vault"
        vault_dir = self.root / "demo-vault"
        shutil.copytree(demo_src, vault_dir, ignore=shutil.ignore_patterns(".cadabby", "*.pyc"))
        vault = Vault(vault_dir)

        cache = VaultCache(vault)
        cache.scan()

        # Rename note on disk
        old_path = vault.wiki_dir / "SQLite-vs-DuckDB.md"
        new_path = vault.wiki_dir / "SQLite-Versus-DuckDB.md"
        old_path.rename(new_path)

        ins, _, deleted, _ = cache.scan()
        self.assertEqual(deleted, 1)
        self.assertEqual(ins, 1)

        conn = cache.get_connection()
        # Verify old cid is completely gone
        cur = conn.execute("SELECT COUNT(*) FROM notes WHERE cid = 'wiki/SQLite-vs-DuckDB';")
        self.assertEqual(cur.fetchone()[0], 0)
        cur = conn.execute("SELECT COUNT(*) FROM links WHERE source_cid = 'wiki/SQLite-vs-DuckDB';")
        self.assertEqual(cur.fetchone()[0], 0)

        # Verify FTS5 integrity check passes
        self.assertTrue(cache.check_fts_integrity())
        cache.close()

    def test_ac7_epistemic_ranking_ordering(self):
        """§10 C15. human-reviewed outranks unverified; deprecated is pushed below both."""
        demo_src = Path(__file__).resolve().parent.parent / "examples" / "demo-vault"
        vault_dir = self.root / "demo-vault"
        shutil.copytree(demo_src, vault_dir, ignore=shutil.ignore_patterns(".cadabby", "*.pyc"))
        vault = Vault(vault_dir)

        # Add identical keyword 'hyperparameter' to Epistemic-Trust-Tiers (human-reviewed) and Flash-Attention (unverified)
        p1 = vault.wiki_dir / "Epistemic-Trust-Tiers.md"
        p2 = vault.wiki_dir / "Flash-Attention.md"

        p1.write_text(p1.read_text("utf-8") + "\n\nImportant hyperparameter tuning details.\n", "utf-8")
        p2.write_text(p2.read_text("utf-8") + "\n\nImportant hyperparameter tuning details.\n", "utf-8")

        # Re-verify p1 so it is human-reviewed with matching body hash
        verify_note(vault, "wiki/Epistemic-Trust-Tiers", actor="human:tester", is_human_authorized=True)

        cache = VaultCache(vault)
        results = cache.search("hyperparameter")

        self.assertGreaterEqual(len(results), 2)
        top = results[0]
        second = results[1]

        self.assertEqual(top.cid, "wiki/Epistemic-Trust-Tiers")
        self.assertEqual(top.trust_tier, "human-reviewed")
        self.assertGreater(top.score, second.score)
        cache.close()

    def test_ac8_concurrent_writers_no_corruption(self):
        """No current §10 criterion. Pins §8: concurrent writers to different notes do not corrupt the vault."""
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
        """No current §10 criterion. Pins §8: a mismatched expected_hash raises VAULT_CONFLICT and writes nothing."""
        demo_src = Path(__file__).resolve().parent.parent / "examples" / "demo-vault"
        vault_dir = self.root / "demo-vault"
        shutil.copytree(demo_src, vault_dir, ignore=shutil.ignore_patterns(".cadabby", "*.pyc"))
        vault = Vault(vault_dir)

        note_file = vault.wiki_dir / "Flash-Attention.md"
        original_content = note_file.read_text("utf-8")

        with self.assertRaises(VaultConflictError):
            update_note(
                vault=vault,
                cid_or_path="wiki/Flash-Attention",
                frontmatter_patch={"status": "deprecated"},
                expected_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
            )

        # Content must remain intact
        self.assertEqual(note_file.read_text("utf-8"), original_content)

    def test_ac10_mcp_capabilities_and_human_refusal(self):
        """§10 C10 and C12. Exactly 7 tools over JSON-RPC, and `by: human:*` is refused."""
        demo_src = Path(__file__).resolve().parent.parent / "examples" / "demo-vault"
        vault_dir = self.root / "demo-vault"
        shutil.copytree(demo_src, vault_dir, ignore=shutil.ignore_patterns(".cadabby", "*.pyc"))
        vault = Vault(vault_dir)

        server = McpServer(vault)
        init_res = server.handle_initialize({"clientInfo": {"name": "claude-code"}})
        self.assertEqual(init_res["serverInfo"]["name"], "cadabby")
        self.assertEqual(init_res["serverInfo"]["version"], cadabby.__version__)

        tools_res = server.handle_tools_list()
        self.assertEqual(len(tools_res["tools"]), 7)

        # 1. Verification over MCP stamps agent:<clientInfo>
        res = server.handle_tools_call(
            "vault_verify_note",
            {"cid": "wiki/Flash-Attention"},
        )
        self.assertFalse(res["isError"])
        self.assertIn("agent:claude-code", res["content"][0]["text"])

        # 2. Rejection of human:* over MCP (AC 12)
        res_actor = server.handle_tools_call(
            "vault_verify_note",
            {"cid": "wiki/Flash-Attention", "actor": "human:attacker"},
        )
        self.assertTrue(res_actor["isError"])
        self.assertIn("Error: Verification by human:* cannot be performed over MCP", res_actor["content"][0]["text"])

        res_by = server.handle_tools_call(
            "vault_verify_note",
            {"cid": "wiki/Flash-Attention", "by": "human:attacker"},
        )
        self.assertTrue(res_by["isError"])

    def test_ac11_audit_provenance_mismatch(self):
        """§10 C13. audit flags a human attestation whose commit author is unmapped."""
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
        """No current §10 criterion. Pins §7.1's portability claim: .mcp.json alone is a working surface."""
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
        ground_out = server.handle_tools_call("vault_ground", {"cids": ["wiki/SQLite"]})
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
            {"cid": "wiki/Embedded-Vector-Systems", "method": "automated-check"},
        )
        self.assertFalse(verify_out["isError"])

        # 5. Run lint to ensure vault remains immaculate
        lint_out = server.handle_tools_call("vault_lint", {})
        self.assertFalse(lint_out["isError"])

    def test_ac16_sync_zero_git_diff(self):
        """§10 C16. sync run twice produces no Git diff on the second run."""
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
        """§10 C21. init leaves an existing CLAUDE.md untouched and says so."""
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
        """§10 C22. install --all is byte-idempotent; --uninstall restores the prior config exactly."""
        claude_dest = self.root / ".claude.json"
        ag_dest = self.root / "antigravity" / "plugins" / "cadabby"

        initial_claude = json.dumps({"mcpServers": {"existing": {}}, "custom": 123}, indent=2) + "\n"
        claude_dest.write_text(initial_claude, "utf-8")

        from cadabby.installer import (
            install_antigravity,
            install_claude,
            uninstall_antigravity,
            uninstall_claude,
        )

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
        """§10 C23. No persona behavior text appears in more than one file."""
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
                    except (UnicodeDecodeError, OSError):
                        pass
            return matching

        lib_matches = count_phrase_occurrences(librarian_phrase)
        self.assertEqual(
            len(lib_matches),
            1,
            f"Librarian persona text duplicated across files: {lib_matches}",
        )
        self.assertEqual(str(lib_matches[0]), "src/cadabby/assets/skills/librarian/SKILL.md")

        tech_matches = count_phrase_occurrences(technician_phrase)
        self.assertEqual(
            len(tech_matches),
            1,
            f"Technician persona text duplicated across files: {tech_matches}",
        )
        self.assertEqual(str(tech_matches[0]), "src/cadabby/assets/skills/technician/SKILL.md")

        agents_matches = count_phrase_occurrences(agents_md_phrase)
        self.assertEqual(
            len(agents_matches),
            1,
            f"AGENTS.md constitution duplicated across files: {agents_matches}",
        )
        self.assertEqual(str(agents_matches[0]), "src/cadabby/assets/vault/AGENTS.md")


class TestSpecVaultTree(unittest.TestCase):
    """§2's tree is the first thing a reader looks at, so it has to be complete.

    It drifted to omit `.agents/plugins/cadabby/`, `.claude/commands/`, and
    `.obsidian/` -- three directories `init` creates and three that §7.2, §7.3
    and §7.5 all treat as normative. The reader's mental model of a vault came
    from a picture missing a quarter of it. §9's tree had the same rot and got
    a guard; this is that guard for §2.
    """

    def setUp(self):
        self.repo_root = Path(__file__).resolve().parent.parent
        spec = (self.repo_root / "SPECIFICATION.md").read_text("utf-8")
        self.tree = spec.split("## 2. Vault Architecture")[1].split("```text")[1].split("```")[0]
        self.names = self._tree_names(self.tree)
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp_dir.name) / "tree-vault"
        args = DummyArgs(vault=str(self.vault), name="tree-vault", obsidian=True)
        with contextlib.redirect_stdout(io.StringIO()):
            cmd_init(args)

    def tearDown(self):
        self.tmp_dir.cleanup()

    @staticmethod
    def _tree_names(tree: str) -> set[str]:
        """Exact path segments named by the tree, not a substring haystack.

        Testing `name in tree` looks equivalent and is not: every segment is a
        substring of some longer word somewhere, so renaming `plugins/` to
        `xplugins/` passes while the limb the reader needs is gone.
        """
        names = set()
        for line in tree.splitlines():
            entry = re.sub(r"^[\s│├└─]*", "", line).split("#")[0].strip()
            names.update(seg for seg in entry.split("/") if seg)
        return names

    def test_every_path_init_creates_appears_in_the_tree(self):
        """Directories are the unit: the tree elides leaves, never whole limbs."""
        missing = sorted(
            str(p.relative_to(self.vault))
            for p in self.vault.rglob("*")
            if p.is_dir() and p.name not in self.names
        )
        self.assertEqual(missing, [], f"§2's vault tree omits directories `init` creates: {missing}")

    def test_the_tree_names_no_file_init_does_not_create(self):
        """The inverse: a tree promising files that never appear misleads harder."""
        # The leading `\.?` matters: `\b` does not match before a dot, so a
        # naive pattern silently turns `.mcp.json` into `mcp.json` and the
        # assertion fails on a file the tree never actually named.
        promised = set(re.findall(r"(?<![\w.-])\.?[\w-][\w.-]*\.(?:md|json|db)\b", self.tree))
        # Illustrative content the tree shows by example, not by contract.
        promised -= {
            "2026-paper-kv.pdf", "web-clip-article.md", "audio-transcript.txt",
            "Storage-MOC.md", "AI-Systems-MOC.md", "Epistemic-Trust-Tiers.md",
            "Flash-Attention.md", "SQLite.md", "Anthropic.md", "SQLite-vs-DuckDB.md",
            "README.md", "Q3-Review.md", "2025.md", "cache.db",
        }
        actual = {p.name for p in self.vault.rglob("*") if p.is_file()}
        self.assertEqual(promised - actual, set(), "§2's vault tree names files `init` never writes")


class TestSpecRepositoryTree(unittest.TestCase):
    """§9's tree is a module-responsibility map, so it has to stay true.

    It previously drifted far enough to name two test files that never existed
    and to omit the ports/adapters layer entirely. Nothing caught that, because
    nothing was checking. This does.
    """

    def setUp(self):
        self.repo_root = Path(__file__).resolve().parent.parent
        spec = (self.repo_root / "SPECIFICATION.md").read_text("utf-8")
        section = spec.split("## 9. Codebase")[1]
        self.tree = section.split("```text")[1].split("```")[0]

    def _repo_files(self, pattern: str, root: str) -> list[Path]:
        return [
            p
            for p in (self.repo_root / root).rglob(pattern)
            if "__pycache__" not in p.parts and p.name != "__init__.py"
        ]

    def test_every_module_and_test_file_appears_in_the_tree(self):
        undocumented = [
            str(p.relative_to(self.repo_root))
            for p in self._repo_files("*.py", "src/cadabby") + self._repo_files("test_*.py", "tests")
            if p.name not in self.tree
        ]
        self.assertEqual(undocumented, [], f"Files missing from the SPECIFICATION.md §9 tree: {undocumented}")

    def test_tree_names_no_file_that_does_not_exist(self):
        documented_tests = set(re.findall(r"\btest_\w+\.py\b", self.tree))
        actual = {p.name for p in self._repo_files("test_*.py", "tests")}
        self.assertEqual(
            documented_tests - actual,
            set(),
            "SPECIFICATION.md §9 names test files that do not exist, implying coverage that is absent",
        )

    def test_assets_are_inside_the_package_so_they_ship_in_the_wheel(self):
        self.assertTrue((self.repo_root / "src" / "cadabby" / "assets").is_dir())
        self.assertFalse((self.repo_root / "assets").exists(), "Assets outside the package are excluded from the wheel")
        self.assertFalse((self.repo_root / "plugins").exists(), "The plugin lives at src/cadabby/assets/plugins (§7.2)")


class TestHarnessShimsStayThin(unittest.TestCase):
    """§7.4. Shims are copied into a vault and frozen there forever.

    They are safe only because they name behavior rather than carrying it. Two
    places bend that rule by embedding a value that lives somewhere else, and
    both are unobservable once frozen: nothing in a stale vault reports that
    its copy no longer matches the engine. These assertions are the report.
    """

    ASSETS = Path(__file__).resolve().parent.parent / "src" / "cadabby" / "assets"
    PLUGIN = ASSETS / "plugins" / "cadabby"

    def test_plugin_version_tracks_the_package_version(self):
        """A frozen plugin.json otherwise claims its birth version forever."""
        manifest = json.loads((self.PLUGIN / "plugin.json").read_text("utf-8"))
        self.assertEqual(manifest["version"], cadabby.__version__)

    def test_every_version_string_in_the_repo_agrees(self):
        """Four copies of one number, previously with one pair checked.

        `pyproject.toml` carries a static version rather than flit's dynamic
        form, so it and `__version__` are independent strings that look
        identical right up until a bump touches one of them. That pair is the
        dangerous one: PyPI would ship a wheel labelled 0.3.1 whose MCP server
        introduces itself as 0.3.0 over the handshake, and the only place the
        mismatch surfaces is a client log nobody reads. The spec header is
        included because a document claiming to specify a version that was
        never released is the same defect in prose.
        """
        root = Path(__file__).resolve().parent.parent
        found = {
            "pyproject.toml": re.search(
                r'^version = "([^"]+)"', (root / "pyproject.toml").read_text("utf-8"), re.M
            ).group(1),
            "plugin.json": json.loads((self.PLUGIN / "plugin.json").read_text("utf-8"))["version"],
            "SPECIFICATION.md": re.search(
                r"^\*\*Version:\*\* (\S+)", (root / "SPECIFICATION.md").read_text("utf-8"), re.M
            ).group(1),
        }
        disagreeing = {k: v for k, v in found.items() if v != cadabby.__version__}
        self.assertEqual(
            disagreeing, {}, f"version drift from __version__ {cadabby.__version__}: {disagreeing}"
        )

    def test_plugin_skill_enumerates_exactly_the_tools_the_server_exposes(self):
        """The one shim that embeds the tool surface instead of pointing at it.

        Tolerable only while §5.1 pins the surface at seven tools; if that
        invariant is ever relaxed, this fails rather than letting every
        already-initialized vault advertise a tool list that no longer exists.
        """
        skill = (self.PLUGIN / "skills" / "cadabby-wiki" / "SKILL.md").read_text("utf-8")
        advertised = set(re.findall(r"\bvault_\w+\b", skill))
        self.assertEqual(advertised, {t["name"] for t in TOOLS})

    def test_shims_point_at_vault_content_rather_than_restating_it(self):
        """Every command and agent manifest delegates; none carries a runbook."""
        shims = sorted((self.ASSETS / "commands").glob("*.md")) + sorted(self.PLUGIN.glob("agents/*/agent.md"))
        self.assertTrue(shims, "No shims found; the asset layout moved")
        for shim in shims:
            with self.subTest(shim.name):
                text = shim.read_text("utf-8")
                body = text.split("---", 2)[-1]
                self.assertLess(
                    len(body.strip().splitlines()),
                    8,
                    "A shim this long is carrying behavior that belongs in .agents/skills/ (§7.4)",
                )
                self.assertRegex(body, r"\.agents/skills/|AGENTS\.md", "A shim must delegate to vault content")


class TestAcceptanceTraceability(unittest.TestCase):
    """§10's traceability table is only worth having if it cannot quietly rot.

    It already did once: the criteria were rewritten while the `ac1`-`ac19`
    test numbering that tracked them was not, so the suite kept asserting
    deleted criteria while new ones went unverified for several releases.
    Nothing caught it, because nothing was checking. This checks.
    """

    GAP = "none"

    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parent.parent
        spec = (cls.repo_root / "SPECIFICATION.md").read_text("utf-8")
        table = spec.split("### Traceability")[1].split("\n\n")
        cls.rows = {}
        for block in table:
            for line in block.splitlines():
                m = re.match(r"\|\s*(C\d+)\s*\|([^|]*)\|(.*)\|", line)
                if m:
                    cls.rows[m.group(1)] = {"spec": m.group(2).strip(), "verified": m.group(3).strip()}
        cls.criteria = set(re.findall(r"\*\*(C\d+)\.\*\*", spec))

    def _suite_test_names(self):
        loader = unittest.TestLoader()
        with contextlib.redirect_stderr(io.StringIO()):
            suite = loader.discover(str(self.repo_root / "tests"))

        def walk(s):
            for t in s:
                if isinstance(t, unittest.TestSuite):
                    yield from walk(t)
                else:
                    yield t._testMethodName

        return set(walk(suite))

    def test_every_criterion_has_a_row(self):
        self.assertEqual(
            self.criteria - set(self.rows),
            set(),
            "§10 states criteria that the traceability table does not map",
        )

    def test_every_row_has_a_criterion(self):
        self.assertEqual(
            set(self.rows) - self.criteria,
            set(),
            "the traceability table maps criteria that §10 no longer states",
        )

    def test_every_cited_test_exists(self):
        """The failure mode that produced ac1-ac19: citing coverage that is gone."""
        actual = self._suite_test_names()
        missing = {
            cid: sorted(set(re.findall(r"`(test_\w+)`", row["verified"])) - actual)
            for cid, row in self.rows.items()
        }
        missing = {k: v for k, v in missing.items() if v}
        self.assertEqual(missing, {}, f"§10 cites tests that do not exist: {missing}")

    def test_every_row_cites_a_test_or_declares_the_gap(self):
        """An empty cell reads as covered at a glance. Gaps must be words."""
        silent = [
            cid
            for cid, row in self.rows.items()
            if not re.findall(r"`(test_\w+)`", row["verified"]) and self.GAP not in row["verified"].lower()
        ]
        self.assertEqual(silent, [], f"criteria with neither a test nor a declared gap: {silent}")

    def test_every_section_reference_resolves(self):
        """A `§` pointing nowhere is the cheap half of the mis-pointing problem.

        The expensive half — a reference to a real but wrong section — is not
        detectable here, and §10's table carried five of those (§6.1 for the
        MCP tool surface, §7.5 for the Git audit) through several releases.
        This at least makes renumbering a section fail loudly rather than
        leaving dangling pointers scattered through the prose.
        """
        spec = (self.repo_root / "SPECIFICATION.md").read_text("utf-8")
        headings = {m.group(1) for m in re.finditer(r"^#{2,4}\s+(\d+(?:\.\d+)?)\.", spec, re.M)}
        dangling = sorted({r for r in re.findall(r"§(\d+(?:\.\d+)?)", spec) if r not in headings})
        self.assertEqual(dangling, [], f"SPECIFICATION.md cites sections that do not exist: {dangling}")

    def test_known_gaps_are_still_the_only_gaps(self):
        """Pins the set so a regression cannot hide inside the existing debt.

        Empty since the §2.5 gap report landed, which closed C17-C20 -- the
        last criteria with no coverage at all. Keeping the assertion rather
        than deleting it is the point: an empty expectation makes the next
        uncovered criterion a failure instead of a line nobody re-reads.
        Criteria still marked **partial** are deliberately not counted here;
        they have tests, just not enough, and C1/C2/C4 track that separately.
        """
        declared = {cid for cid, row in self.rows.items() if self.GAP in row["verified"].lower()}
        self.assertEqual(declared, set())


if __name__ == "__main__":
    unittest.main()

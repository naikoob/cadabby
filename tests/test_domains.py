"""Unit tests for Self-Describing, Self-Governing Cognitive Domains.

Conforms to Cadabby Technical Specification and Cognitive Domain Architecture.
Tests Phase 1: Foundations, path/CID generalization, and domain discovery.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cadabby.cache import VaultCache
from cadabby.domain import DomainDefinition
from cadabby.lint import run_vault_lint
from cadabby.mcp import McpServer
from cadabby.ops import ground_notes, scaffold_note, verify_note
from cadabby.vault import cid_to_path, path_to_cid, path_to_layer
from tests.helpers import create_test_vault


class TestDomainFoundations(unittest.TestCase):
    def test_path_and_cid_mappings_across_domains(self):
        # 1. Canonical wiki
        wiki_rel = "wiki/concepts/Epistemic-Trust-Tiers.md"
        wiki_cid = "wiki/concepts/Epistemic-Trust-Tiers"
        self.assertEqual(path_to_cid(wiki_rel), wiki_cid)
        self.assertEqual(cid_to_path(wiki_cid), wiki_rel)
        self.assertEqual(path_to_layer(wiki_rel), "wiki")

        # 2. Customers domain
        cust_rel = "customers/acme-corp/README.md"
        cust_cid = "customers/acme-corp/README"
        self.assertEqual(path_to_cid(cust_rel), cust_cid)
        self.assertEqual(cid_to_path(cust_cid), cust_rel)
        self.assertEqual(path_to_layer(cust_rel), "customers")

        # 3. Projects domain
        proj_rel = "projects/apollo/rfc-001.md"
        proj_cid = "projects/apollo/rfc-001"
        self.assertEqual(path_to_cid(proj_rel), proj_cid)
        self.assertEqual(cid_to_path(proj_cid), proj_rel)
        self.assertEqual(path_to_layer(proj_rel), "projects")

        # 4. Raw evidence retains extension
        raw_rel = "raw/papers/attention.pdf"
        self.assertEqual(path_to_cid(raw_rel), raw_rel)
        self.assertEqual(cid_to_path(raw_rel), raw_rel)
        self.assertEqual(path_to_layer(raw_rel), "raw")

        raw_md = "raw/notes.md"
        self.assertEqual(path_to_cid(raw_md), raw_md)
        self.assertEqual(cid_to_path(raw_md), raw_md)
        self.assertEqual(path_to_layer(raw_md), "raw")

        # 5. Log retains extension
        log_rel = "log/2026.md"
        self.assertEqual(path_to_cid(log_rel), log_rel)
        self.assertEqual(cid_to_path(log_rel), log_rel)
        self.assertEqual(path_to_layer(log_rel), "log")


class TestDomainDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault_dir = Path(self.tmp.name) / "vault"
        self.vault = create_test_vault(self.vault_dir, subdirs=("wiki", "raw", "log"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_wiki_domain_fallback(self):
        domains = self.vault.discover_domains()
        self.assertIn("wiki", domains)
        wiki_def = domains["wiki"]
        self.assertIsInstance(wiki_def, DomainDefinition)
        self.assertEqual(wiki_def.name, "wiki")
        self.assertIsNone(wiki_def.allowed_types)  # Open / permissive by default
        self.assertFalse(wiki_def.require_sources)
        self.assertEqual(wiki_def.directives_markdown, "")

    def test_domain_with_agents_md(self):
        cust_dir = self.vault_dir / "customers"
        cust_dir.mkdir()
        agents_md = cust_dir / "AGENTS.md"
        agents_md.write_text(
            """---
domain: customers
description: Customer accounts and intelligence
allowed_types:
  - account
  - meeting
  - requirement
require_sources: true
---
# Customer Intelligence Directives

You are the Account Intelligence Specialist.
Always cite raw evidence.
""",
            encoding="utf-8",
        )

        domains = self.vault.discover_domains()
        self.assertIn("customers", domains)
        cust_def = domains["customers"]
        self.assertEqual(cust_def.name, "customers")
        self.assertEqual(cust_def.description, "Customer accounts and intelligence")
        self.assertEqual(cust_def.allowed_types, ["account", "meeting", "requirement"])
        self.assertTrue(cust_def.require_sources)
        self.assertIn("# Customer Intelligence Directives", cust_def.directives_markdown)
        self.assertIn("Always cite raw evidence.", cust_def.directives_markdown)

    def test_domain_without_agents_md_is_permissive(self):
        proj_dir = self.vault_dir / "projects"
        proj_dir.mkdir()

        domains = self.vault.discover_domains()
        self.assertIn("projects", domains)
        proj_def = domains["projects"]
        self.assertEqual(proj_def.name, "projects")
        self.assertIsNone(proj_def.allowed_types)  # Open / permissive
        self.assertFalse(proj_def.require_sources)
        self.assertEqual(proj_def.directives_markdown, "")

    def test_search_inclusion_is_structural_not_configurable(self):
        """A legacy `searchable: false` manifest key is inert: every discovered domain is searched.

        Search inclusion is decided before discovery (reserved names, dot-prefixed
        directories, DEFAULT_IGNORED_DIRS). A per-domain opt-out would make search
        lie by omission, so the field was removed (SPECIFICATION.md §2.2).
        """
        archive_dir = self.vault_dir / "archive"
        archive_dir.mkdir()
        (archive_dir / "AGENTS.md").write_text(
            """---
domain: archive
description: Retired material
searchable: false
---
Directives...
""",
            encoding="utf-8",
        )
        (archive_dir / "Decommissioned-Pipeline.md").write_text(
            """---
type: synthesis
title: Decommissioned Pipeline
description: Retired ingestion pipeline notes
status: deprecated
---
# Decommissioned Pipeline
The pipeline relied on quasicrystalline sharding.
""",
            encoding="utf-8",
        )

        domains = self.vault.discover_domains()
        self.assertIn("archive", domains)
        # The key is not parsed into the definition at all.
        self.assertFalse(hasattr(domains["archive"], "searchable"))

        # An unfiltered search still reaches the domain.
        with VaultCache(self.vault) as cache:
            cache.scan()
            hits = cache.search("quasicrystalline")
            self.assertEqual([r.cid for r in hits], ["archive/Decommissioned-Pipeline"])

    def test_discover_domains_ignores_reserved_and_ignored_directories(self):
        for ignored in [".git", ".cadabby", ".obsidian", ".venv", "venv", "node_modules", "target"]:
            (self.vault_dir / ignored).mkdir()

        domains = self.vault.discover_domains()
        self.assertIn("wiki", domains)
        for ignored in [".git", ".cadabby", ".obsidian", ".venv", "venv", "node_modules", "target", "raw", "log"]:
            self.assertNotIn(ignored, domains)

    def test_malformed_agents_md_graceful_fallback(self):
        research_dir = self.vault_dir / "research"
        research_dir.mkdir()
        agents_md = research_dir / "AGENTS.md"
        agents_md.write_text(
            """---
[unparseable YAML invalid syntax:::
---
# Research Directives
Explore ideas freely.
""",
            encoding="utf-8",
        )

        domains = self.vault.discover_domains()
        self.assertIn("research", domains)
        res_def = domains["research"]
        self.assertEqual(res_def.name, "research")
        self.assertIsNone(res_def.allowed_types)
        self.assertIn("Research Directives", res_def.directives_markdown)


class TestMultiDomainCacheAndGraph(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault_dir = Path(self.tmp.name) / "vault"
        self.vault = create_test_vault(
            self.vault_dir,
            subdirs=("wiki/concepts", "customers/acme", "projects/apollo", "raw", "log"),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_multi_domain_cache_scan_and_fts_search(self):
        # 1. Setup wiki note
        (self.vault_dir / "wiki" / "concepts" / "Architecture.md").write_text(
            """---
type: concept
title: Canonical Architecture
status: evergreen
tags:
  - architecture
---
# Canonical Architecture
This is the central architecture note.
""",
            encoding="utf-8",
        )

        # 2. Setup customers note and AGENTS.md
        (self.vault_dir / "customers" / "AGENTS.md").write_text(
            """---
domain: customers
description: Customer domain
---
Directives...
""",
            encoding="utf-8",
        )
        (self.vault_dir / "customers" / "acme" / "README.md").write_text(
            """---
type: account
title: ACME Corp Account
status: active
tags:
  - customer
---
# ACME Corp
Leading enterprise client. Referencing [[Architecture]].
""",
            encoding="utf-8",
        )

        # 3. Setup project note
        (self.vault_dir / "projects" / "apollo" / "Architecture.md").write_text(
            """---
type: project
title: Apollo Project Architecture
status: active
---
# Apollo Architecture
Apollo specific architecture design.
""",
            encoding="utf-8",
        )

        # 4. Setup nested ignored directories to ensure they are ignored
        ignored_dir = self.vault_dir / "projects" / "apollo" / "node_modules"
        ignored_dir.mkdir(parents=True)
        (ignored_dir / "package.json").write_text("{}", encoding="utf-8")
        (ignored_dir / "README.md").write_text("# Ignored", encoding="utf-8")

        cache = VaultCache(self.vault)
        _, _, _, total = cache.scan()

        # Should index 3 notes: wiki, customers, projects.
        # Should NOT index AGENTS.md, node_modules/README.md
        self.assertEqual(total, 3)

        status = cache.get_status()
        self.assertEqual(status["total_notes"], 3)
        self.assertEqual(status["domains"]["wiki"], 1)
        self.assertEqual(status["domains"]["customers"], 1)
        self.assertEqual(status["domains"]["projects"], 1)

        # Verify AGENTS.md is not in notes table
        conn = cache.get_connection()
        cur = conn.execute("SELECT cid FROM notes WHERE cid LIKE '%AGENTS%';")
        self.assertEqual(len(cur.fetchall()), 0)

        # Verify search across all domains
        all_results = cache.search("Architecture")
        self.assertEqual(len(all_results), 3)
        cids = {r.cid for r in all_results}
        self.assertIn("wiki/concepts/Architecture", cids)
        self.assertIn("projects/apollo/Architecture", cids)
        self.assertIn("customers/acme/README", cids)

        # Verify domain-filtered search
        wiki_results = cache.search("Architecture", domain="wiki")
        self.assertEqual(len(wiki_results), 1)
        self.assertEqual(wiki_results[0].cid, "wiki/concepts/Architecture")
        self.assertEqual(wiki_results[0].domain, "wiki")

        proj_results = cache.search("Architecture", domain="projects")
        self.assertEqual(len(proj_results), 1)
        self.assertEqual(proj_results[0].cid, "projects/apollo/Architecture")
        self.assertEqual(proj_results[0].domain, "projects")

        cust_results = cache.search("Architecture", domain="customers")
        self.assertEqual(len(cust_results), 1)
        self.assertEqual(cust_results[0].cid, "customers/acme/README")
        self.assertEqual(cust_results[0].domain, "customers")

        # Verify SearchResult.to_dict contains domain
        d = proj_results[0].to_dict()
        self.assertEqual(d["domain"], "projects")
        self.assertEqual(d["cid"], "projects/apollo/Architecture")
        cache.close()

    def test_cross_domain_links_and_stem_priority(self):
        # 1. Wiki canonical Architecture note
        (self.vault_dir / "wiki" / "concepts" / "Architecture.md").write_text(
            """---
type: concept
title: Architecture
status: evergreen
---
# Architecture
""",
            encoding="utf-8",
        )

        # 2. Apollo project Architecture note
        (self.vault_dir / "projects" / "apollo" / "Architecture.md").write_text(
            """---
type: project
title: Apollo Architecture
status: active
---
# Apollo Architecture
""",
            encoding="utf-8",
        )

        # 3. Customer note referencing both bare [[Architecture]] and specific [[apollo/Architecture]]
        (self.vault_dir / "customers" / "acme" / "Overview.md").write_text(
            """---
type: account
title: Overview
status: active
---
# Overview
See standard [[Architecture]] and project [[apollo/Architecture]].
""",
            encoding="utf-8",
        )

        cache = VaultCache(self.vault)
        cache.scan()

        conn = cache.get_connection()
        cur = conn.execute(
            """
            SELECT target_raw, target_cid
            FROM links
            WHERE source_cid = 'customers/acme/Overview'
            ORDER BY target_raw;
            """
        )
        links = {row["target_raw"]: row["target_cid"] for row in cur.fetchall()}

        # Bare [[Architecture]] MUST resolve to canonical wiki note
        self.assertEqual(links["Architecture"], "wiki/concepts/Architecture")

        # Specific [[apollo/Architecture]] MUST resolve to project note
        self.assertEqual(links["apollo/Architecture"], "projects/apollo/Architecture")
        cache.close()


class TestMultiDomainLinter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault_dir = Path(self.tmp.name) / "vault"
        self.vault = create_test_vault(
            self.vault_dir,
            subdirs=("wiki/concepts", "customers/acme", "projects/apollo", "raw", "log"),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_lint_custom_types_and_allowed_types(self):
        # Configure customers domain with allowed_types
        (self.vault_dir / "customers" / "AGENTS.md").write_text(
            """---
domain: customers
allowed_types:
  - account
  - contact
---
Customer directives.
""",
            encoding="utf-8",
        )

        # Valid note with type: account
        (self.vault_dir / "customers" / "acme" / "README.md").write_text(
            """---
type: account
title: Acme Corp
status: active
---
# Acme Corp
Referencing [[wiki/concepts/Database]].
""",
            encoding="utf-8",
        )

        # Invalid note with type: stranger (not in allowed_types)
        (self.vault_dir / "customers" / "acme" / "Unknown.md").write_text(
            """---
type: stranger
title: Unknown Person
status: active
---
# Unknown
""",
            encoding="utf-8",
        )

        # Also add target wiki note so link isn't dead
        (self.vault_dir / "wiki" / "concepts" / "Database.md").write_text(
            """---
type: concept
title: Database
status: evergreen
---
# Database
""",
            encoding="utf-8",
        )

        findings = run_vault_lint(self.vault)
        cust_findings = [f for f in findings if f.rel_path.startswith("customers/")]
        codes = [f.code for f in cust_findings]
        self.assertIn("ENUM_INVALID", codes)

        enum_err = next(f for f in cust_findings if f.code == "ENUM_INVALID")
        self.assertIn("stranger", enum_err.message)
        self.assertEqual(enum_err.rel_path, "customers/acme/Unknown.md")

    def test_lint_permissive_domain_allows_arbitrary_types(self):
        # Projects has NO AGENTS.md -> permissive domain
        (self.vault_dir / "projects" / "apollo" / "RFC-001.md").write_text(
            """---
type: rfc
title: RFC 001
status: active
---
# RFC 001
Content.
""",
            encoding="utf-8",
        )

        findings = run_vault_lint(self.vault)
        proj_findings = [f for f in findings if f.rel_path.startswith("projects/")]
        # type: rfc is accepted without ENUM_INVALID
        self.assertEqual(len([f for f in proj_findings if f.code == "ENUM_INVALID"]), 0)

    def test_lint_layout_bypass_for_flexible_domains(self):
        # Note in customers/acme/README.md is nested inside acme/
        (self.vault_dir / "customers" / "acme" / "README.md").write_text(
            """---
type: account
title: Acme Corp
status: active
---
# Acme Corp
""",
            encoding="utf-8",
        )

        # Note in wiki/concepts/Mismatched.md with type: entity
        (self.vault_dir / "wiki" / "concepts" / "Mismatched.md").write_text(
            """---
type: entity
title: Mismatched Entity
status: active
---
# Mismatched
""",
            encoding="utf-8",
        )

        findings = run_vault_lint(self.vault)
        mismatch_findings = [f for f in findings if f.code == "WIKI_NESTING_DISALLOWED"]

        # Only wiki note triggers WIKI_NESTING_DISALLOWED, customers note does NOT!
        self.assertEqual(len(mismatch_findings), 1)
        self.assertEqual(mismatch_findings[0].rel_path, "wiki/concepts/Mismatched.md")

    def test_lint_require_sources_enforcement(self):
        # Customers domain configured with require_sources: true
        (self.vault_dir / "customers" / "AGENTS.md").write_text(
            """---
domain: customers
require_sources: true
---
Directives.
""",
            encoding="utf-8",
        )

        # 1. Note missing sources
        (self.vault_dir / "customers" / "acme" / "NoSource.md").write_text(
            """---
type: account
title: No Source
status: active
---
# No Source
""",
            encoding="utf-8",
        )

        # 2. Note with valid source
        (self.vault_dir / "raw" / "contract.txt").write_text("Contract text", encoding="utf-8")
        (self.vault_dir / "customers" / "acme" / "WithSource.md").write_text(
            """---
type: account
title: With Source
status: active
sources:
  - raw/contract.txt
---
# With Source
""",
            encoding="utf-8",
        )

        findings = run_vault_lint(self.vault)
        source_missing = [f for f in findings if f.code == "SOURCE_MISSING" and f.rel_path.startswith("customers/")]
        self.assertEqual(len(source_missing), 1)
        self.assertEqual(source_missing[0].rel_path, "customers/acme/NoSource.md")
        self.assertIn("requires 'sources:'", source_missing[0].message)

    def test_lint_cross_domain_orphan_prevention(self):
        # Wiki note that has 0 wiki links
        (self.vault_dir / "wiki" / "concepts" / "Database.md").write_text(
            """---
type: concept
title: Database
status: evergreen
---
# Database
Central storage.
""",
            encoding="utf-8",
        )

        # Customer note linking to [[Database]]
        (self.vault_dir / "customers" / "acme" / "README.md").write_text(
            """---
type: account
title: Acme Corp
status: active
---
# Acme
Uses [[Database]].
""",
            encoding="utf-8",
        )

        findings = run_vault_lint(self.vault)
        orphan_findings = [f for f in findings if f.code == "NOTE_ORPHAN"]
        # Database.md must NOT be an orphan because of the inbound link from customers!
        self.assertEqual(len(orphan_findings), 0)


class TestMultiDomainOpsAndMcp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault_dir = Path(self.tmp.name) / "vault"
        self.vault = create_test_vault(
            self.vault_dir,
            subdirs=("wiki/concepts", "wiki/entities", "customers/acme", "raw", "log"),
        )

        # Setup customers/AGENTS.md
        (self.vault_dir / "customers" / "AGENTS.md").write_text(
            """---
domain: customers
description: Customer accounts and contracts
allowed_types:
  - account
  - contract
require_sources: false
---
# Customer Domain Agent Instructions
1. Never commit PII or customer passwords.
2. Cross-reference enterprise architecture concepts.
""",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_scaffold_note_in_custom_domain(self):
        path = scaffold_note(
            vault=self.vault,
            title="Acme Corporation",
            type_="account",
            description="Leading industrial client",
            domain="customers",
            actor="agent:ops-test",
        )
        self.assertTrue(path.exists())
        self.assertEqual(self.vault.rel_path(path), "customers/Acme-Corporation.md")
        content = path.read_text("utf-8")
        self.assertIn("type: account", content)
        self.assertIn("Acme Corporation", content)
        self.assertIn("agent:ops-test", content)

    def test_scaffold_note_with_custom_path(self):
        path = scaffold_note(
            vault=self.vault,
            title="Acme Profile",
            type_="account",
            description="Deep profile",
            domain="customers",
            path="customers/acme/README.md",
            actor="agent:ops-test",
        )
        self.assertTrue(path.exists())
        self.assertEqual(self.vault.rel_path(path), "customers/acme/README.md")

    def test_scaffold_note_enforces_domain_allowed_types(self):
        with self.assertRaises(ValueError) as ctx:
            scaffold_note(
                vault=self.vault,
                title="Invalid Type Note",
                type_="invalid_type",
                description="Should fail",
                domain="customers",
            )
        self.assertIn("allowed types are ['account', 'contract']", str(ctx.exception))

    def test_ground_notes_enriches_domain_and_directives(self):
        note_path = self.vault_dir / "customers" / "Acme.md"
        note_path.write_text(
            """---
type: account
title: Acme
status: active
---
# Acme Corp
Details here.
""",
            encoding="utf-8",
        )
        with VaultCache(self.vault) as cache:
            cache.scan()
            grounded = ground_notes(self.vault, ["customers/Acme"], cache=cache)

        self.assertEqual(len(grounded), 1)
        item = grounded[0]
        self.assertEqual(item["domain"], "customers")
        self.assertEqual(item["type"], "account")
        self.assertIn("Never commit PII", item["directives"])

    def test_mcp_server_resources_list_and_read(self):
        with McpServer(self.vault) as server:
            # 1. Initialize
            init_res = server.handle_initialize({"clientInfo": {"name": "test-suite"}})
            self.assertIn("resources", init_res["capabilities"])

            # 2. Resources List
            res_list = server.handle_resources_list()
            uris = [r["uri"] for r in res_list["resources"]]
            self.assertIn("vault://domains", uris)
            self.assertIn("domain://customers/directives", uris)

            # 3. Read vault://domains
            domains_res = server.handle_resources_read("vault://domains")
            domains_json = json.loads(domains_res["contents"][0]["text"])
            self.assertIn("customers", domains_json)
            self.assertEqual(domains_json["customers"]["allowed_types"], ["account", "contract"])

            # 4. Read domain://customers/directives
            directives_res = server.handle_resources_read("domain://customers/directives")
            directives_text = directives_res["contents"][0]["text"]
            self.assertIn("Customer Domain Agent Instructions", directives_text)

    def test_mcp_tools_call_scaffold_and_search_with_domain(self):
        with McpServer(self.vault) as server:
            # Scaffold via MCP
            scaffold_res = server.handle_tools_call(
                "vault_scaffold_note",
                {
                    "title": "Beta Client",
                    "type": "account",
                    "description": "Beta customer",
                    "domain": "customers",
                },
            )
            self.assertFalse(scaffold_res["isError"])
            self.assertIn("Scaffolded note: customers/Beta-Client.md", scaffold_res["content"][0]["text"])

            # Search filtered by domain
            server.cache.scan()
            search_res = server.handle_tools_call(
                "vault_search",
                {
                    "query": "Beta",
                    "domain": "customers",
                },
            )
            self.assertFalse(search_res["isError"])
            results = json.loads(search_res["content"][0]["text"])
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["cid"], "customers/Beta-Client")
            self.assertEqual(results[0]["domain"], "customers")


class TestDomainManifestValidity(unittest.TestCase):
    """§2.2, §6.3, C35. A manifest that cannot be honored is reported, never read as 'open'."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault_dir = Path(self.tmp.name) / "vault"
        self.vault = create_test_vault(self.vault_dir, subdirs=("wiki", "customers", "raw", "log"))

    def tearDown(self):
        self.tmp.cleanup()

    def _manifest(self, text):
        (self.vault_dir / "customers" / "AGENTS.md").write_text(text, encoding="utf-8")

    def test_invalid_manifest_is_reported_and_blocks_scaffold(self):
        self._manifest("---\nallowed_types: [account, contact]\n---\n# Directives\n")
        findings = [f for f in run_vault_lint(self.vault) if f.code == "DOMAIN_MANIFEST_INVALID"]
        self.assertEqual([f.rel_path for f in findings], ["customers/AGENTS.md"])
        self.assertEqual(findings[0].severity, "error")
        with self.assertRaisesRegex(ValueError, "AGENTS.md is invalid"):
            scaffold_note(self.vault, title="Acme", type_="account", description="d", domain="customers")
        self.assertFalse((self.vault_dir / "customers" / "Acme.md").exists())
        directives = self.vault.discover_domains()["customers"].directives_markdown
        self.assertNotIn("allowed_types", directives)
        self.assertIn("# Directives", directives)

    def test_manifest_field_shapes_are_checked(self):
        cases = {
            "allowed_types": "---\nallowed_types: account\n---\n",
            "require_sources": "---\nrequire_sources: yes\n---\n",
            "folder name": "---\ndomain: clients\n---\n",
        }
        for needle, text in cases.items():
            with self.subTest(field=needle):
                self._manifest(text)
                error = self.vault.discover_domains()["customers"].manifest_error
                self.assertIsNotNone(error)
                self.assertIn(needle, error)

    def test_valid_manifest_has_no_error(self):
        self._manifest("---\ndomain: customers\nallowed_types:\n  - account\nrequire_sources: true\n---\n")
        domain = self.vault.discover_domains()["customers"]
        self.assertIsNone(domain.manifest_error)
        self.assertEqual(domain.allowed_types, ["account"])
        self.assertTrue(domain.require_sources)

class TestMultiDomainEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault_dir = Path(self.tmp.name) / "vault"
        self.vault = create_test_vault(
            self.vault_dir,
            subdirs=("wiki/concepts", "wiki/entities", "customers/acme", "projects/apollo", "raw", "log"),
        )

        # Domain manifests
        (self.vault_dir / "customers" / "AGENTS.md").write_text(
            """---
domain: customers
description: Enterprise client profiles
allowed_types:
  - account
  - meeting
require_sources: true
---
# Customer Domain Agent Instructions
Ensure client confidentiality and verify CRM sync.
""",
            encoding="utf-8",
        )

        (self.vault_dir / "projects" / "AGENTS.md").write_text(
            """---
domain: projects
description: Engineering and research initiatives
allowed_types:
  - project
  - rfc
require_sources: false
---
# Project Domain Agent Instructions
Architectural decisions must link to canonical concepts.
""",
            encoding="utf-8",
        )

        # Raw source
        (self.vault_dir / "raw" / "contract-acme.txt").write_text(
            "Acme Master Services Agreement signed 2026.",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_cognitive_domain_lifecycle(self):
        # 1. Scaffold canonical wiki concept
        wiki_note_path = scaffold_note(
            vault=self.vault,
            title="Event Sourcing",
            type_="concept",
            description="Pattern of capturing all changes as events",
            domain="wiki",
            actor="human:architect",
        )
        self.assertTrue(wiki_note_path.exists())

        # 2. Scaffold customer account with required source
        cust_note_path = scaffold_note(
            vault=self.vault,
            title="Acme Corp Account",
            type_="account",
            description="Acme enterprise customer",
            domain="customers",
            path="customers/acme/Account.md",
            sources=["raw/contract-acme.txt"],
            body="# Acme Corp Account\n\nUses [[projects/apollo/README|Apollo Engine]] and implements [[Event-Sourcing]].\n",
            actor="agent:crm-sync",
        )
        self.assertTrue(cust_note_path.exists())

        # 3. Scaffold engineering project RFC
        proj_note_path = scaffold_note(
            vault=self.vault,
            title="Apollo Engine",
            type_="project",
            description="High throughput event processing platform",
            domain="projects",
            path="projects/apollo/README.md",
            body="# Apollo Engine\n\nBuilt on [[Event-Sourcing]] for client [[customers/acme/Account]].\n",
            actor="agent:project-planner",
        )
        self.assertTrue(proj_note_path.exists())

        # 4. Run Epistemic Linter across all domains
        findings = run_vault_lint(self.vault)
        errors = [f for f in findings if f.severity == "error"]
        self.assertEqual(len(errors), 0, f"Lint errors found: {errors}")

        # 5. Cache Scan & FTS Search
        with VaultCache(self.vault) as cache:
            _, _, _, total = cache.scan()
            self.assertEqual(total, 4)  # 3 notes + 1 raw

            # Check status breakdown
            status = cache.get_status()
            self.assertEqual(status["total_notes"], 3)
            self.assertEqual(status["total_wiki"], 1)
            self.assertEqual(status["domains"]["wiki"], 1)
            self.assertEqual(status["domains"]["customers"], 1)
            self.assertEqual(status["domains"]["projects"], 1)

            # Cross-domain search
            all_res = cache.search("Event")
            self.assertGreaterEqual(len(all_res), 2)

            # Filtered domain search
            proj_res = cache.search("Event", domain="projects")
            self.assertEqual(len(proj_res), 1)
            self.assertEqual(proj_res[0].cid, "projects/apollo/README")

            # 6. Note Grounding with Directives Enrichment
            grounded = ground_notes(self.vault, ["projects/apollo/README", "customers/acme/Account"], cache=cache)
            self.assertEqual(len(grounded), 2)
            proj_ground = next(g for g in grounded if g["cid"] == "projects/apollo/README")
            self.assertEqual(proj_ground["domain"], "projects")
            self.assertIn("Architectural decisions must link to canonical concepts", proj_ground["directives"])
            self.assertEqual(len(proj_ground["links"]), 2)

            cust_ground = next(g for g in grounded if g["cid"] == "customers/acme/Account")
            self.assertEqual(cust_ground["domain"], "customers")
            self.assertIn("Ensure client confidentiality", cust_ground["directives"])
            self.assertEqual(len(cust_ground["sources"]), 1)
            self.assertTrue(cust_ground["sources"][0]["exists"])

        # 7. Verification Attestation
        attest_res = verify_note(
            vault=self.vault,
            cid_or_path="projects/apollo/README",
            actor="agent:keiko-auditor",
            method="automated-check",
        )
        self.assertEqual(attest_res["trust_tier"], "machine-confirmed")

        # 8. MCP Protocol Verification
        with McpServer(self.vault) as server:
            tools_resp = server.handle_tools_list()
            self.assertEqual(len(tools_resp["tools"]), 7)  # Strict 7-tool invariant

            res_resp = server.handle_resources_list()
            uris = [r["uri"] for r in res_resp["resources"]]
            self.assertIn("vault://domains", uris)
            self.assertIn("domain://customers/directives", uris)
            self.assertIn("domain://projects/directives", uris)


if __name__ == "__main__":
    unittest.main()

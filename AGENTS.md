# AGENTS.md — Cadabby Development & Maintenance Guide

This document is the canonical constitution and operational handbook for AI agents working on the **Cadabby** codebase. It outlines the architectural invariants, core tenets, development workflows, testing discipline, and component maps required to safely maintain and extend the repository.

---

## 1. Project Identity & Philosophy

**Cadabby** is a zero-dependency, local-first engine for Karpathy-style LLM Wiki vaults with Open Knowledge Format (OKF v0.2) epistemic trust tiers, flat canonical synthesis, autonomous cognitive domains, and multi-harness agent integration (Google Antigravity, Claude Code, Gemini CLI, Obsidian).

### Non-Negotiable Tenets
1. **Zero Runtime Dependencies**: `pyproject.toml` specifies `dependencies = []`. Cadabby runs exclusively on the Python standard library (`sqlite3`, `pathlib`, `json`, `hashlib`, `re`, `argparse`, `dataclasses`, `typing`, `tempfile`, `shutil`, `sys`, `os`). Never introduce third-party runtime dependencies (no PyYAML, no Pydantic, no Click, no Requests).
2. **Python >= 3.11 Standard**: Use modern Python idioms (`from __future__ import annotations`, `typing.Protocol`, structural pattern matching, `Path`, `datetime`). All functions must have strict type annotations.
3. **Specification as Ground Truth**: [`SPECIFICATION.md`](SPECIFICATION.md) (v0.3.1) is the normative specification. If code and specification disagree, the specification is the truth, or the specification must be deliberately updated in concert with code and acceptance tests.
4. **Hexagonal Architecture (Ports and Adapters)**: Core use cases in [`src/cadabby/ops.py`](src/cadabby/ops.py) interact with storage and ledgers via driven `typing.Protocol` interfaces in [`src/cadabby/ports.py`](src/cadabby/ports.py), allowing hermetic unit testing without disk I/O.
5. **Atomic Durability**: Vault files are never written directly in place. All mutations use temporary files in the same directory, `fsync()`, and `os.replace()` to ensure crash-safe atomicity.

---

## 2. Core Architectural Invariants

Any agent modifying Cadabby must strictly preserve the following architectural contracts:

### 2.1. Flat Wiki & Navigation Topology (§2.1, §2.5)
- **`wiki/` is Flat**: All notes in `wiki/` reside strictly at `wiki/*.md`. Any nested subdirectory triggers `WIKI_NESTING_DISALLOWED` in Gate 2 linting.
- **Maps of Content (MOC)**: Topology in `wiki/` is editorial and defined by MOC notes (`type: moc`). Physical folder hierarchies in `wiki/` are forbidden.
- **Cognitive Domains**: Non-reserved top-level folders (`customers/`, `projects/`, etc.) are autonomous cognitive domains self-described by `{domain}/AGENTS.md`. Domains freely support nested subdirectories and never receive engine-generated index files.
- **`index.md` is a Gap Report, Not a Catalog**:
  - `index.md` has exactly 4 sections:
    1. **Entry points**: `type: moc` notes (always listed).
    2. **Unfiled notes**: Active wiki notes not transitively reachable from any MOC via forward links (computed using a recursive CTE). Excludes domain notes and `deprecated` notes.
    3. **Raw queue**: Files in `raw/` not cited by any note's `sources:`.
    4. **Verification debt**: Notes where all verifications are stale relative to `body_hash`.
  - Sections 2–4 are **omitted when empty**. A healthy vault displays entry points and a message that no gaps exist.
  - **Regeneration Rides the Scan**: Any cache scan that detects changes regenerates `index.md`. Unchanged scans skip building the report to guarantee byte and `mtime` stability.

### 2.2. Epistemic Trust Tiers & OKF Frontmatter (§3.1 – §3.4)
- **Restricted YAML Subset (`frontmatter.py`)**: Accepts only a strict, safe subset of YAML (block mappings, single-level nested scalar mappings, scalar sequences, flat mapping sequences).
  - Explicitly rejects flow style (`[a, b]`, `{k: v}`), multiline block scalars (`|`, `>`), anchors/aliases, and tags.
  - Read path fails loudly (`FRONTMATTER_UNPARSEABLE`) and never modifies unparseable files.
  - Write path **raises an error** on unrepresentable data; it must never coerce or stringify arbitrary objects.
- **Normalized Body Hash**: `body_hash` is computed over UTF-8 text with `\n` line endings, stripped trailing whitespace, and stripped leading/trailing blank lines, prefixed with `sha256:`. The body hash **strictly excludes frontmatter**.
- **Trust Tiers**:
  1. `human-reviewed`: Valid attestation with `by: human:*` matching current `body_hash`.
  2. `machine-confirmed`: Valid attestation with `by: agent:*` or `by: process:*` matching current `body_hash`.
  3. `stale-verified`: Verification entries exist, but none match the current `body_hash`.
  4. `unverified`: No verification entries present.
- **Refusal of Human Verifications over MCP**: `vault_verify_note` over MCP must unconditionally refuse `by: human:*`. Human verification is reserved for interactive TTY CLI commands (`cadabby verify --human`).

### 2.3. SQLite Ephemeral Cache & External FTS5 (§4.2 – §4.4)
- **100% Disposable**: `.cadabby/cache.db` can be deleted at any time; any command reconstructs it cleanly on the next scan.
- **Contentless-External FTS5**:
  - Two FTS5 virtual tables over the unified `notes` content table: `notes_fts` (curated notes across all domains) and `raw_fts` (text from `raw/` sources).
  - Keeps raw document statistics from flattening BM25 scores of curated notes.
- **Retraction Invariant**:
  - Before updating or deleting a row in `notes`, the old indexed terms **must be retracted** using `INSERT INTO <fts>(<fts>, rowid, title, description, body, tags) VALUES('delete', ...)`.
  - The retraction must target the exact table corresponding to the row's layer (`_fts_table(layer)`).
  - All writes to `notes` must go through `cache.upsert_note()` or `cache.delete_note()`. Direct SQL writes to `notes` outside these methods are forbidden.
  - Retraction integrity is validated on both tables with `INSERT INTO <fts>(<fts>) VALUES('integrity-check')`.
- **BM25 Epistemic Ranking**:
  - Query multiplies `-bm25(...)` by trust and status multipliers.
  - `-bm25()` is normalized to positive values (higher score is better).
  - Domain filtering via `:domain` filters at SQL level.

### 2.4. Tool Surface & Multi-Harness Architecture (§5.1, §7.1 – §7.6)
- **Strict 7-Tool Invariant**: The MCP server exposes exactly 7 tools:
  `vault_search`, `vault_ground`, `vault_status`, `vault_scaffold_note`, `vault_update_note`, `vault_verify_note`, `vault_lint`.
  Never add new tools to the MCP server. Multi-domain features are folded into existing tool parameters (`domain`, `path`).
- **MCP Resources**: The server advertises `"resources": {}` and serves `vault://domains` (JSON) and `domain://{domain}/directives` (Markdown).
- **Single-Definition Invariant**:
  - Persona behavior lives exclusively in `.agents/skills/<persona>/SKILL.md` (canonical: `librarian` and `technician`).
  - Antigravity subagent manifests (`agents/*/agent.md`) and Claude Code slash commands (`commands/*.md`) are thin non-normative shims pointing to the skills. Never duplicate runbook prose.
- **Assets Inside the Package**: All starter templates, command shims, persona skills, and the Antigravity plugin live in `src/cadabby/assets/` so they are packaged into the distribution wheel.
- **Three-Tier File Ownership**:
  - **Engine-owned** (`.mcp.json`, `.claude/commands/*`, `.agents/skills/*`, `.agents/plugins/cadabby/*`): Refreshed by `cadabby install`.
  - **User-owned** (`.cadabby.json`, `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `STYLE.md`): Written once at `init`, never overwritten by `install`. Overwritten only with `init --force`.
  - **Generated** (`index.md`, `log.md`, `.cadabby/*`): Maintained by engine scans.
- **Vault Resolution Precedence (§2.7)**:
  1. `--vault <path>` (exact).
  2. `$CADABBY_VAULT` (exact, fails loudly if path is invalid).
  3. Working directory walked upward to `.cadabby.json` or `.obsidian/`.

---

## 3. Repository Structure & Component Map

```text
cadabby/
├── pyproject.toml              # Build config (flit_core), zero runtime dependencies, requires-python >= 3.11
├── uv.lock                     # Developer environment lockfile
├── README.md                   # User documentation, quickstart, CLI & harness guide
├── SPECIFICATION.md            # Normative system specification (v0.3.1)
├── AGENTS.md                   # This file (developer & agent constitution)
├── LICENSE                     # MIT
│
├── src/cadabby/                # Core Python package
│   ├── __init__.py             # Exports and __version__
│   ├── __main__.py             # Entry point for `python3 -m cadabby`
│   ├── cli.py                  # CLI subcommands, argument parsing, terminal output formatting
│   ├── constants.py            # Enums, default configs, schemas, ignored directory lists
│   ├── domain.py               # Note entity, cognitive domain definitions, {domain}/AGENTS.md parser
│   ├── vault.py                # Vault root resolution (§2.7), config loading, CID/path mapping
│   ├── frontmatter.py          # Restricted YAML subset parser & canonicalizing writer (§3.2)
│   ├── okf.py                  # OKF validation, body hashing, trust tier derivation (§3.1-§3.4)
│   ├── fsutil.py               # Atomic replace (tempfile + fsync), advisory lock, O_APPEND ledger writes
│   ├── cache.py                # SQLite cache, FTS5 external content, scan, upsert/delete, BM25 search
│   ├── graph.py                # Wikilink parsing, target CID resolution, backlinks
│   ├── indexer.py              # Gap report generator (index.md) and activity ledger rotation (log.md)
│   ├── lint.py                 # Dynamic six-gate epistemic linting across all cognitive domains
│   ├── audit.py                # Git blame provenance audit for human:* attestations (§3.5)
│   ├── ops.py                  # Core use cases (scaffold, update, verify, ground) implementing business logic
│   ├── installer.py            # cadabby init / install harness registration, idempotent config merge
│   ├── mcp.py                  # JSON-RPC 2.0 stdio MCP server (7 tools + domain resources)
│   │
│   ├── ports.py                # Driven ports: NoteStoragePort, LedgerPort, IndexCachePort
│   ├── adapters/
│   │   ├── disk_storage.py     # Production DiskNoteStorage, FileLedger
│   │   └── memory_storage.py   # Hermetic InMemoryNoteStorage, InMemoryLedger for tests
│   │
│   └── assets/                 # Shipped inside the wheel
│       ├── vault/              # Starter templates (.cadabby.json, AGENTS.md, STYLE.md, index.md, etc.)
│       ├── skills/             # Canonical persona runbooks (librarian, technician)
│       ├── commands/           # Thin slash-command shims (ingest, vault-status, vault-lint)
│       └── plugins/cadabby/    # Bundled Antigravity plugin (plugin.json, mcp_config.json, skills, agents)
│
├── examples/
│   └── demo-vault/             # Golden fixture vault used by tests and examples
│
└── tests/                      # Complete test suite (pure unittest, zero test dependencies)
    ├── test_primitives.py      # fsutil atomicity/locking, vault config, ranking
    ├── test_frontmatter.py     # Subset parse/write boundary, round-trip fidelity
    ├── test_okf.py             # Body hashing, actor/timestamp validation, tier derivation
    ├── test_cache.py           # Scan, deletion reconciliation, FTS5 ranking & integrity
    ├── test_graph.py           # Wikilink resolution, anchors, aliases, dead links
    ├── test_indexer.py         # index.md gap report, MOC reachability, regeneration
    ├── test_lint.py            # Six gates, taxonomy closure, warning vs error severities
    ├── test_ops.py             # Scaffolding, attribution, conflict detection, atomicity
    ├── test_domain.py          # Note entity and use cases driven through memory adapters
    ├── test_domains.py         # Multi-domain discovery, cache, linting, ops, MCP resources
    ├── test_mcp.py             # MCP handshake, tool execution, human:* refusal
    ├── test_cli.py             # Command surface, flags, JSON output, install refresh
    ├── test_installer.py       # init scaffolding, idempotency, merge, uninstall, dry-run
    └── test_acceptance.py      # §10 Acceptance criteria C1-C23 and TestAcceptanceTraceability
```

---

## 4. Development & Testing Commands

Always run commands from the repository root (`/home/bookian/Workspaces/cadabby`).

### Running Tests
Cadabby uses Python's standard `unittest` framework. No third-party test runners are required:

```bash
# Run the entire test suite (canonical, ~250 tests in < 1 second)
PYTHONPATH=src python3 -m unittest discover tests

# Run a specific test module
PYTHONPATH=src python3 -m unittest tests/test_indexer.py
PYTHONPATH=src python3 -m unittest tests/test_lint.py
PYTHONPATH=src python3 -m unittest tests/test_acceptance.py

# Run a specific test case class or method
PYTHONPATH=src python3 -m unittest tests.test_acceptance.TestAcceptanceTraceability
PYTHONPATH=src python3 -m unittest tests.test_acceptance.TestAcceptanceCriteria.test_ac2_disposable_cache_reproducibility
```

Or using `uv` if virtual environment execution is desired:
```bash
uv run python3 -m unittest discover tests
```

### Manual CLI Testing
Invoke the CLI locally without installing:
```bash
PYTHONPATH=src python3 -m cadabby --help
PYTHONPATH=src python3 -m cadabby status --vault examples/demo-vault
PYTHONPATH=src python3 -m cadabby lint --vault examples/demo-vault
PYTHONPATH=src python3 -m cadabby search "SQLite" --vault examples/demo-vault
```

### Building & Packaging
Verify package buildability:
```bash
uv build
```

---

## 5. Specification & Traceability Discipline

Cadabby maintains an automated traceability contract between [`SPECIFICATION.md`](SPECIFICATION.md) and the test suite:

1. **Section 10 Acceptance Criteria (C1–C23)**:
   - Criteria must be phrased as **falsifiable observations** (something you can do to a vault to observe whether it complies).
   - Criteria must never duplicate requirement prose from earlier sections. The **Defined in** column in §10's Traceability Table points to the requirement.
2. **`TestAcceptanceTraceability` Guard**:
   - Every criterion in §10 must have a corresponding row in the traceability table.
   - Every test method cited in the table must exist in `tests/`.
   - Every `§` section reference throughout `SPECIFICATION.md` must resolve to an existing heading.
   - All criteria must have verified test citations (zero undeclared gaps).
3. **Version String Parity**:
   Whenever the package version changes:
   - `src/cadabby/__init__.py` (`__version__ = "X.Y.Z"`)
   - `pyproject.toml` (`version = "X.Y.Z"`)
   - `src/cadabby/assets/vault/.cadabby.json` (`"schema": ...`)
   - `src/cadabby/assets/plugins/cadabby/plugin.json` (`"version": "X.Y.Z"`)
   - `uv.lock`
   All must stay strictly in sync.

---

## 6. Guidelines for AI Agents Working on Cadabby

When implementing features, fixing bugs, or refactoring code:

- **Do Not Add Dependencies**: Never modify `pyproject.toml` to add runtime dependencies.
- **Do Not Break Atomicity**: Never write directly to a note or config file with raw `open(..., "w")`. Use `atomic_write_file()` from [`fsutil.py`](src/cadabby/fsutil.py).
- **Do Not Touch `notes` Directly**: Never execute raw `INSERT`, `UPDATE`, or `DELETE` statements against the `notes` table. Always use `cache.upsert_note()` and `cache.delete_note()` to ensure FTS retraction consistency.
- **Do Not Expand MCP Tools**: Keep the tool count at exactly 7. New capabilities must fit within the parameters of existing tools or MCP resources.
- **Do Not Create Domain Index Files**: Never generate `{domain}/index.md` for cognitive domains.
- **Do Not Duplicate Persona Text**: Never inline runbook or persona instructions into subagent manifests, command shims, or plugin configs. Keep them strictly in `.agents/skills/<persona>/SKILL.md`.
- **Always Run the Test Suite**: Run `PYTHONPATH=src python3 -m unittest discover tests` after any edit. All tests must pass before concluding any task.
- **Maintain Traceability**: If you add or modify behavior specified in `SPECIFICATION.md`, ensure the corresponding criteria and traceability rows in §10 are updated, and confirm that `TestAcceptanceTraceability` passes.

# AGENTS.md — Cadabby Development & Maintenance Guide

This document is the canonical constitution and operational handbook for AI agents working on the **Cadabby** codebase. It outlines the architectural invariants, core tenets, development workflows, testing discipline, and component maps required to safely maintain and extend the repository.

---

## 1. Project Identity & Philosophy

**Cadabby** is a zero-dependency, local-first engine for Karpathy-style LLM Wiki vaults with Open Knowledge Format (OKF v0.2) epistemic trust tiers, flat canonical synthesis, autonomous cognitive domains, and multi-harness agent integration (Google Antigravity, Claude Code, Gemini CLI, Obsidian).

### Non-Negotiable Tenets
1. **Zero Runtime Dependencies**: `pyproject.toml` specifies `dependencies = []`. Cadabby runs exclusively on the Python standard library (`sqlite3`, `pathlib`, `json`, `hashlib`, `re`, `argparse`, `dataclasses`, `typing`, `tempfile`, `shutil`, `sys`, `os`). Never introduce third-party runtime dependencies (no PyYAML, no Pydantic, no Click, no Requests).
2. **Python >= 3.11 Standard**: Use modern Python idioms (`from __future__ import annotations`, `typing.Protocol`, structural pattern matching, `Path`, `datetime`). All functions must have strict type annotations.
3. **Specification as Ground Truth**: [`SPECIFICATION.md`](SPECIFICATION.md) (v0.3.1) is the normative specification. If code and specification disagree, the specification is the truth, or the specification must be deliberately updated in concert with code and acceptance tests. It is ~27k tokens: read the `§` you need rather than the whole file. §0 maps every section to the question it answers and gives the one-line `awk` recipe for extracting one, and the §-reference guard means any section number you find in a comment or test docstring is a safe address.
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

The authoritative file-by-file tree lives in [`SPECIFICATION.md` §9](SPECIFICATION.md), and is held true by
`TestSpecRepositoryTree` in [`tests/test_acceptance.py`](tests/test_acceptance.py): a new module or test file
that the tree does not name fails the suite, and a tree entry naming a file that does not exist fails it too.
Read §9 when you need the full map. It is deliberately not copied here -- the copy that used to sit in this
section was guarded by nothing and had already drifted, omitting `errors.py` after the error taxonomy landed.

What follows is orientation only: where to enter the codebase for a given kind of change.

| If you are changing... | Start at | Then check |
| :--- | :--- | :--- |
| Note schema, trust tiers, body hashing | `okf.py`, `frontmatter.py` | §3, `test_okf.py`, `test_frontmatter.py` |
| Search, ranking, the FTS5 tables, scanning | `cache.py` | §4, `test_cache.py` |
| Wikilink parsing, resolution, backlinks | `graph.py` | §2.5, `test_graph.py` |
| `index.md` or `log.md` output | `indexer.py` | §2.5, `test_indexer.py` |
| A lint gate's codes or severities | `lint.py` | §6.3, `test_lint.py` |
| Scaffold / update / verify / ground behavior | `ops.py` (+ `ports.py`, `adapters/`) | §9.1, `test_ops.py`, `test_domain_model.py` |
| The MCP tool surface or resources | `mcp.py` | §5, §7, `test_mcp.py` |
| CLI flags, output, exit codes | `cli.py`, `errors.py` | §5.4, `test_cli.py`, `test_errors.py` |
| `init` / `install` and file ownership | `installer.py` | §7.6, `test_installer.py` |
| Anything shipped into a user's vault | `src/cadabby/assets/` | §7.1, §7.5, `test_templates.py` |

Two structural facts worth holding without a lookup: every port has a production adapter and a hermetic
in-memory twin, so a use case can be tested with no vault on disk; and everything under `src/cadabby/assets/`
is inside the package on purpose, because anything outside it is absent from the built wheel.

---

## 4. Development & Testing Commands

Always run commands from the repository root (`/home/bookian/Workspaces/cadabby`).

### Running Tests
Cadabby uses Python's standard `unittest` framework. No third-party test runners are required:

```bash
# Fast parallel test execution across all CPU cores (zero dependencies, ~5.5s)
python3 tests/run_parallel.py

# Parallel test execution with verbose target reporting or filter
python3 tests/run_parallel.py -v
python3 tests/run_parallel.py test_cache

# Canonical sequential test suite runner
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

1. **Section 10 Acceptance Criteria**:
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
- **Do Not Let Templates Own Frontmatter**: Obsidian templates (§7.5) seed a note's *body* only. `generated`, `verified`, and the body hash they bind to are engine-owned; a template able to set them could mint notes whose trust tier asserts more than the vault can back. The shipped starters in `assets/vault/templates/` obey the same rule, and leave `description` empty on purpose — a `TODO` placeholder would lint clean and quietly seed a required epistemic field with filler, where an empty one fails loudly and names the note.
- **Do Not Reserve `templates/` By Default**: `.obsidian/templates.json` is read everywhere and written by exactly one code path, `init --obsidian-templates`, which is opt-in and writes the setting only together with the folder it names. Defaulting it would silently cost a user who wants `templates/` as a cognitive domain (§2.2). An existing declaration always wins, including under `--force`.
- **Do Not Duplicate Persona Text**: Never inline runbook or persona instructions into subagent manifests, command shims, or plugin configs. Keep them strictly in `.agents/skills/<persona>/SKILL.md`.
- **Always Run the Test Suite**: Run `PYTHONPATH=src python3 -m unittest discover tests` after any edit. All tests must pass before concluding any task.
- **Maintain Traceability**: If you add or modify behavior specified in `SPECIFICATION.md`, ensure the corresponding criteria and traceability rows in §10 are updated, and confirm that `TestAcceptanceTraceability` passes.

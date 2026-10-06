# Cadabby Product Backlog

This backlog records prioritized feature requests, architectural enhancements, and ergonomic improvements derived directly from multi-persona, multi-harness empirical simulations (Student, Researcher, Software Developer, Presales Engineer, Lawyer) across direct CLI and Google Antigravity MCP interactions.

All proposed backlog items strictly adhere to Cadabby's non-negotiable architectural tenets:
1. **Zero Runtime Dependencies** (`CPython >= 3.11` standard library only).
2. **Epistemic Trust Tiers & Tamper Evidence** (SHA-256 body hashes and non-repudiation).
3. **Flat Canonical Synthesis** (`wiki/` is strictly flat; topology is editorial via Maps of Content).
4. **Autonomous Cognitive Domains** (`{domain}/AGENTS.md` without engine-generated domain catalogs).
5. **Disposable FTS5 SQLite Cache** (contentless-external tables and retraction invariants).
6. **Strict 7-Tool MCP Invariant** (no tool surface sprawl).
7. **Atomic Durability** (crash-safe `fsync` + `os.replace`).

---

## Prioritized Feature Requests

### CAD-BL-001: In-Flight Incremental Cache Synchronization & Heading Hygiene in MCP

* **Target Components**: `src/cadabby/mcp.py`, `src/cadabby/domain.py`
* **Priority**: High
* **Impact**: Eliminates read-after-write agent lag and malformed markdown headings in agentic workflows.
* **Effort**: Low (~25 lines of code)

#### 1. Problem Statement
* **Read-After-Write Cache Latency**: When an agent invokes `vault_scaffold_note` or `vault_update_note` over MCP, the file mutation is written atomically to disk storage, but the active in-memory SQLite connection (`self.cache`) is not incrementally updated. If the agent immediately calls `vault_search` or `vault_ground` within the same turn, the query sees stale link graphs or fails to locate newly created CIDs until an explicit `vault_status` (which executes `cache.scan()`) is called.
* **Heading Marker Duplication**: In `Note.append_section()` and `Note.replace_section()`, the engine formats headings with `f"\n\n## {heading}\n\n"`. When LLMs or users supply a markdown-formatted heading like `"## Architecture Decisions"`, it results in malformed markdown: `"## ## Architecture Decisions"`.

#### 2. Evidence from Simulations
* **Persona 1 (Student)**: Attempted to append section `"## Indexed Consensus Protocols"`, producing double-heading markers in `wiki/Distributed-Systems-MOC.md`.
* **Persona 2 (Researcher) & Persona 3 (Developer)**: Scaffolding a note followed immediately by `vault_ground` required a compensatory `vault_status` turn to force a cache scan so 1-hop links would be populated.

#### 3. Proposed Technical Solution
1. **In-Flight Cache Upsert in `mcp.py`**:
   Update `McpServer.handle_tools_call` for `vault_scaffold_note` and `vault_update_note` to perform a single-note incremental cache upsert (`self.cache.upsert_note(...)`) or an in-flight `self.cache.scan()` immediately following the atomic disk write, before returning `_tool_ok`.
2. **Heading Sanitization in `domain.py`**:
   Sanitize the heading parameter in `Note.append_section()` and `Note.replace_section()`:
   ```python
   clean_heading = heading.lstrip("#").strip()
   self.body = self.body.rstrip() + f"\n\n## {clean_heading}\n\n{content.strip()}\n"
   ```

#### 4. Acceptance Criteria
- [ ] `vault_scaffold_note` followed immediately by `vault_search` finds the new note without requiring an intermediate `vault_status` call.
- [ ] `vault_update_note` with `append_section=["## Title", "..."]` produces `## Title`, not `## ## Title`.
- [ ] Zero runtime dependencies; all existing 316 unit tests continue to pass.

---

### CAD-BL-002: Nested Vault Ancestor Boundary Guard in `cadabby init`

* **Target Components**: `src/cadabby/installer.py`, `src/cadabby/vault.py`, `src/cadabby/cli.py`
* **Priority**: High
* **Impact**: Prevents parent vault corruption and Gate 1 parse failures when sub-vaults are initialized inside cognitive domains.
* **Effort**: Low (~35 lines of code)

#### 1. Problem Statement
* **Sub-Vault Domain Collision**: When a user or agent runs `cadabby init --vault <path>` where `<path>` is inside a subdirectory of an existing vault (e.g. `customers/acme-corp/`), `init` successfully initializes a nested vault, dropping `.cadabby.json`, `index.md`, `STYLE.md`, and starter files.
* On subsequent scans of the parent vault, `vault.discover_domains()` walks `customers/` and encounters the un-frontmattered template markdown files (`STYLE.md`, `CLAUDE.md`, etc.), causing Gate 1 to fail with `FRONTMATTER_UNPARSEABLE`.

#### 2. Evidence from Simulations
* **Persona 4 (Presales Engineer)**: Executed `cadabby init --vault customers/acme-corp` to test multi-client isolation. The parent vault's domain scanner immediately crashed on Gate 1 during the next `cadabby lint`, requiring manual file cleanup.

#### 3. Proposed Technical Solution
1. **Ancestor Boundary Check in `installer.py`**:
   Before scaffolding a vault in `init_vault()`, walk upward from `target_path.parent` using `find_vault_root()`.
   If an ancestor `.cadabby.json` or `.obsidian/` root marker is detected, raise an error:
   ```text
   Error: Target path '{target_path}' is nested inside an existing Cadabby vault at '{ancestor}'.
   Nested vaults are disallowed because they break domain discovery and root resolution.
   Use --allow-nested to force.
   ```
2. **Domain Discovery Ignore Rule in `vault.py`**:
   Update `vault.discover_domains()` to ignore any subdirectory that contains its own `.cadabby.json` when `--allow-nested` is used, preventing parent-vault Gate 1 crashes.

#### 4. Acceptance Criteria
- [ ] Running `cadabby init --vault existing-vault/subfolder` fails with exit code 2 and an informative boundary error.
- [ ] Passing `--allow-nested` allows the initialization, and the parent vault's domain discovery ignores the nested vault root.

---

### CAD-BL-003: Epistemic Search Feedback for `raw/` Partition & Attestation History Scoping

* **Target Components**: `src/cadabby/cache.py`, `src/cadabby/mcp.py`, `src/cadabby/lint.py`
* **Priority**: Medium
* **Impact**: Eliminates user confusion regarding raw literature search and aligns Gate 6 linting with active trust tier derivation.
* **Effort**: Low (~30 lines of code)

#### 1. Problem Statement
* **Silent Zero-Results in Raw Search**: Raw sources in `raw/` are indexed exclusively in `raw_fts` to prevent long, unprocessed texts from flattening BM25 corpus frequencies for curated notes (§2.4, §4.4). When users or agents run `cadabby search "term"` or `vault_search("term")` without `--domain raw`, queries matching only `raw/` return `[]`. Users frequently assume the document was not indexed or that search is broken.
* **Confusing Superseded Attestation Warnings**: When a note drifts and is subsequently re-verified, `ops.verify_note` appends the new attestation to `verified: [...]`. The note's derived trust tier is `machine-confirmed` or `human-reviewed` with 0 verification debt. However, Gate 6 in `cadabby lint` checks every entry in the historical list and emits `[WARN] VERIFICATION_STALE` on the older superseded record. Users and agents find it contradictory that a freshly verified note triggers a staleness warning.

#### 2. Evidence from Simulations
* **Persona 2 (Researcher) & Persona 1 (Student)**: Searched for literature citations (e.g. "Lipman") and received 0 results, requiring an explanatory turn to realize `domain="raw"` is mandatory.
* **Persona 5 (Lawyer) & Persona 2 (Researcher)**: Successfully re-verified notes after content mutation, yet `cadabby lint` continued to warn about `VERIFICATION_STALE` due to previous historical entries in `verified: [...]`.

#### 3. Proposed Technical Solution
1. **Raw Partition Search Hint**:
   In `cache.search()`, when a query against `notes_fts` (`domain != 'raw'`) yields 0 results, execute a lightweight probe against `raw_fts`. If matches exist, attach an advisory hint in the result metadata:
   ```json
   {
     "results": [],
     "hint": "0 curated notes matched, but matches were found in raw sources. Query with domain='raw' to search unprocessed literature."
   }
   ```
2. **Gate 6 Attestation Scoping in `lint.py`**:
   Update Gate 6 in `src/cadabby/lint.py` so that `VERIFICATION_STALE` is only emitted if **no entry** in the `verified:` list matches the current `body_hash`. Older superseded entries with non-matching hashes should be treated as historical audit log entries rather than active staleness debt.

#### 4. Acceptance Criteria
- [ ] Searching a term existing only in `raw/` without `--domain raw` returns a helpful diagnostic hint directing the user to `--domain raw`.
- [ ] A note with at least one attestation matching current `body_hash` does not trigger `VERIFICATION_STALE` on its older historical entries in `cadabby lint`.

---

## Summary Matrix of Backlog Items

| ID | Title | Target Subsystems | Impact | Complexity | Tenet Preserved |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **CAD-BL-001** | In-Flight MCP Cache Sync & Heading Hygiene | `mcp.py`, `domain.py` | **High** | Low | Hexagonal Ports, Atomic Durability |
| **CAD-BL-002** | Nested Vault Ancestor Boundary Guard | `installer.py`, `vault.py` | **High** | Low | Cognitive Domains (§2.1), Resolution (§2.7) |
| **CAD-BL-003** | Raw Search Feedback & Attestation Scoping | `cache.py`, `lint.py` | **Medium** | Low | FTS5 Partitioning (§4.4), Trust Tiers (§3.4) |

# Vault Agent Constitution (AGENTS.md)

This document is the **single canonical constitution** governing all AI agents interacting with this vault across every harness (Google Antigravity, Claude Code, Cursor, Zed, Codex). Harness-specific files (`CLAUDE.md`, `GEMINI.md`) are non-normative shims pointing here.

---

## 1. Vault Architecture & Invariants

This vault implements Andrej Karpathy's 3-layer LLM Wiki architecture:

* **`raw/` (Layer 1 — Immutable Ground Truth)**:
  Raw papers, web clips, transcripts, and primary artifacts curated by the human user.
  **Directive**: Agents may read and cite raw sources, but MUST NEVER edit, rename, or delete files in `raw/`.
* **`wiki/` (Layer 2 — Compounded Synthesis)**:
  Compiled knowledge base maintained primarily by AI agents. Organized strictly into 5 directories matching note `type`:
  - `wiki/entities/`: People, organizations, libraries, models, tools.
  - `wiki/concepts/`: Core mechanisms, algorithms, architectural patterns.
  - `wiki/syntheses/`: Overviews and cross-domain topic syntheses.
  - `wiki/comparisons/`: Structured trade-off analyses.
  - `wiki/guides/`: Procedural how-tos and runbooks.
* **Cognitive Domains (Custom Top-Level Directories)**:
  Any non-reserved top-level directory at the vault root functions as an independent Cognitive Domain (e.g. `customers/`, `projects/`, `research/`).
  - **Reserved Directories**: The following directories are reserved by the engine and MUST NOT be used as cognitive domains: `raw/`, `log/`, `.cadabby/`, `.obsidian/`, `.git/`, `.agents/`, `.claude/`, plus build/environment folders (`.venv/`, `node_modules/`, `target/`, etc.).
  - **Domain Manifest (`{domain}/AGENTS.md`)**: A domain defines its governance rules and AI operational constraints via a local `AGENTS.md` file:
    ```yaml
    ---
    description: "Human-readable description of domain purpose"
    searchable: true
    allowed_types:
      - custom_type_a
      - custom_type_b
    require_sources: false
    enforce_layout: false
    ---
    # Domain Directives
    Localized instructions for agents operating on notes in this domain.
    ```
  - **Permissive vs. Strict Domains**:
    - *Permissive* (no `AGENTS.md` or `allowed_types: null`): Accepts any non-empty string for `type:` and allows flexible nested folders.
    - *Strict* (`allowed_types` list provided): Linter gate 1 strictly validates note types. If `enforce_layout: true`, notes must live in subdirectories matching their `type`.
  - **Context Injection**: Calling `vault_ground` on a note in any domain automatically injects that domain's directives into the agent context. Agents can also query `vault://domains` and `domain://{domain}/directives` via MCP resources.
* **Prose & Style Guidelines (`STYLE.md`)**:
  Consult and adhere to `STYLE.md` for objective voice, capitalized human-readable stems, and strict OKF frontmatter requirements across all cognitive domains.
* **`index.md` & `log.md` (System Layer)**:
  - `index.md`: Auto-generated catalog linking all notes and tracking source status. Never hand-edited.
  - `log.md`: Append-only chronological audit ledger.

---

## 2. Epistemic Trust Tiers (OKF v0.2)

Knowledge is classified into four explicit epistemic trust tiers:

1. **`human-reviewed`**: Verified by a human practitioner with `by: human:*` matching current `body_hash`. Represents unquestioned ground truth.
2. **`machine-confirmed`**: Verified by an autonomous agent or tool against sources (`by: agent:*` or `process:*`) matching current `body_hash`. High confidence.
3. **`stale-verified`**: Prior attestation exists, but the note's prose has since drifted. Counted as verification debt.
4. **`unverified`**: Default state for newly scaffolded notes. Requires cross-referencing.

### Verification Binding Invariant
Verification records bind to content (`of: sha256:<body_hash>`), not filenames.
* **Never forge human verification**: The MCP server strictly forbids `by: human:*`.
* **Automatic Agent Attestation**: When calling `vault_verify_note(cid, method=...)`, the MCP server automatically binds the attestation to the active agent session identity (`agent:<client_id>`). Agents only provide `cid` and optional `method`.
* **Preserve verification debt awareness**: When modifying a verified note, be aware that its tier drops to `stale-verified` until re-verified.

---

## 3. Tool Usage Preferences

Always prioritize native Cadabby MCP tools over raw bash commands or filesystem modifications:

1. **Discovery & Retrieval**:
   - `vault_search`: Semantic and keyword BM25 search boosted by epistemic trust.
   - `vault_ground`: Retrieve note content, 1-hop links, backlinks, and sources.
2. **Authoring & Maintenance**:
   - `vault_scaffold_note`: Create new notes with schema-compliant OKF frontmatter in the correct folder or domain.
   - `vault_update_note`: Non-destructive frontmatter patching and section append/replace.
   - `vault_verify_note`: Attest to note factual accuracy against sources.
   - `vault_status`: Check vault health, unprocessed sources, and verification debt.
   - `vault_lint`: Self-heal diagnostics against the six normative gates.

---

## 4. Persona Delegation

* **`librarian`** (`.agents/skills/librarian/SKILL.md`):
  Specializes in reading raw sources, finding connection points, drafting concept notes, and cross-linking wikilinks.
* **`technician`** (`.agents/skills/technician/SKILL.md`):
  Specializes in running `vault_lint`, repairing dead links, resolving orphans, and reporting verification debt.

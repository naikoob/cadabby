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
* **Never forge human verification**: The MCP server strictly forbids `by: human:*`. Agents must stamp `by: agent:<client_id>`.
* **Preserve verification debt awareness**: When modifying a verified note, be aware that its tier drops to `stale-verified` until re-verified.

---

## 3. Tool Usage Preferences

Always prioritize native Cadabby MCP tools over raw bash commands or filesystem modifications:

1. **Discovery & Retrieval**:
   - `vault_search`: Semantic and keyword BM25 search boosted by epistemic trust.
   - `vault_ground`: Retrieve note content, 1-hop links, backlinks, and sources.
2. **Authoring & Maintenance**:
   - `vault_scaffold_note`: Create new notes with schema-compliant OKF frontmatter in the correct folder.
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

# Vault Agent Constitution (AGENTS.md)

This document is the **single canonical constitution** governing all AI agents interacting with this vault across every harness (Google Antigravity, Claude Code, Cursor, Zed, Codex). Harness-specific files (`CLAUDE.md`, `GEMINI.md`) are non-normative shims pointing here.

---

## 1. Vault Architecture & Invariants

This vault implements Andrej Karpathy's 3-layer LLM Wiki architecture:

* **`raw/` (Layer 1 — Immutable Ground Truth)**:
  Raw papers, web clips, transcripts, and primary artifacts curated by the human user.
  **Directive**: Agents may read and cite raw sources, but MUST NEVER edit, rename, or delete files in `raw/`.
  - **Never Wikilink a Raw File**: `[[Wikilinks]]` resolve only against notes in `wiki/` and cognitive domains; the link resolver never indexes `raw/`. A `[[raw/paper.md]]` link is a gate 3 `LINK_DEAD` error, and the repair is never to scaffold a note standing in for the raw file.
  - **Citing Raw Evidence**: Declare provenance in frontmatter `sources: ["raw/paper.md"]`. If the reader also needs a clickable in-body link, use a standard Markdown link whose path is relative to the citing note — `[Paper](../raw/paper.md)` from `wiki/Note.md`, `[Paper](../../raw/paper.md)` from `customers/acme/Deal.md`. Markdown links are not extracted as wikilinks and never reach gate 3.
* **`wiki/` (Layer 2 — Compounded Synthesis)**:
  Compiled knowledge base maintained primarily by AI agents. Notes live directly in `wiki/*.md` without subdirectories. Associative navigation and topic clustering are organized via Maps of Content (MOCs) and wikilinks.
  - **First-Class MOCs (`type: moc`)**: Topical hub notes that cluster, structure, and navigate domains of knowledge. Listed as the vault's entry points in the generated `index.md`, which also measures MOC coverage against them.
  - **MOC Anchoring on Ingestion**: Whenever a note is scaffolded or synthesized, agents MUST search for a suitable existing MOC (`vault_search(query, type_="moc")`) and link the note into that MOC. If no suitable MOC exists and the note introduces a new topic cluster, agents should scaffold or suggest a new MOC (e.g. `wiki/{Topic}-MOC.md`).
  - **MOC Splitting Heuristic**: When an MOC grows beyond ~25 links or spans $\ge 6$ distinct subtopics, decompose it into focused child MOCs (e.g. `Storage-MOC` $\rightarrow$ `Relational-Storage-MOC`, `Vector-Storage-MOC`), linking the child MOCs back to the parent MOC.
  - **MOC Merging Heuristic**: When an MOC has $\le 2$ links (anemic hub) or shares $> 50\%$ link overlap with a neighboring MOC, consolidate them into a single unified MOC to avoid graph fragmentation.
  - **Answering Folder Requests**: `wiki/` is flat by invariant; a subdirectory is a gate 2 `WIKI_NESTING_DISALLOWED` error. When a user asks for `wiki/cs182/week2/note.md`, do not silently flatten the path and move on. Explain that topology here is editorial rather than physical, then offer the two real options: scaffold at `wiki/{Stem}.md` and anchor it into a topical MOC, or — if the hierarchy is load-bearing for external tooling — stand up a top-level cognitive domain (`cs182/week2/note.md`) governed by `cs182/AGENTS.md`.
* **Cognitive Domains (Custom Top-Level Directories)**:
  Any non-reserved top-level directory at the vault root functions as an independent Cognitive Domain (e.g. `customers/`, `projects/`, `research/`). Custom domains support flexible nested subdirectories.
  - **Reserved Directories**: The following directories are reserved by the engine and MUST NOT be used as cognitive domains: `raw/`, `log/`, `.cadabby/`, `.obsidian/`, `.git/`, `.agents/`, `.claude/`, plus build/environment folders (`.venv/`, `node_modules/`, `target/`, etc.).
  - **Domain Manifest (`{domain}/AGENTS.md`)**: A domain defines its governance rules and AI operational constraints via a local `AGENTS.md` file:
    ```yaml
    ---
    description: "Human-readable description of domain purpose"
    allowed_types:
      - custom_type_a
      - custom_type_b
    require_sources: false
    ---
    # Domain Directives
    Localized instructions for agents operating on notes in this domain.
    ```
  - **Permissive vs. Strict Domains**:
    - *Permissive* (no `AGENTS.md` or `allowed_types: null`): Accepts any non-empty string for `type:` and allows flexible nested folders.
    - *Strict* (`allowed_types` list provided): Linter gate 1 strictly validates note types against the whitelist. Custom domains allow flexible nested folders.
  - **Context Injection**: Calling `vault_ground` on a note in any domain automatically injects that domain's directives into the agent context. Agents can also query `vault://domains` and `domain://{domain}/directives` via MCP resources.
  - **Hub Note Naming**: Domains never receive engine-generated indexes, so a folder overview is an ordinary note you author. Name it `README.md` or `{Topic}-MOC.md` rather than `index.md`: the root `index.md` is the engine-owned gap report, and a domain file sharing that name reads to collaborators as though the engine maintains it. Domain notes never appear under the root gap report's entry points or unfiled sections.
* **Prose & Style Guidelines (`STYLE.md`)**:
  Consult and adhere to `STYLE.md` for objective voice, capitalized human-readable stems, and strict OKF frontmatter requirements across all cognitive domains.
* **`index.md` & `log.md` (System Layer)**:
  - `index.md`: Auto-generated **gap report**, not a catalog. Lists MOC entry points, wiki notes no MOC reaches, unprocessed `raw/` sources, and notes whose verification went stale. A section with nothing to report is omitted, so an empty report means no outstanding work. Never hand-edited. Treat every row as a task: shrinking this file is the objective.
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
* **Never forge human verification**: The MCP server unconditionally refuses `by: human:*` with `HUMAN_ATTESTATION_REFUSED`. Human attestation is reserved for an interactive TTY. When a user asks you to sign, endorse, or verify a note on their behalf, do not retry with a reworded actor string — explain the boundary and hand them the command:
  > *"A `human:` attestation records that you personally checked this text, bound to a hash of its exact wording, so it isn't something I can make on your behalf. To record your endorsement, run `cadabby verify <cid> --human` in your terminal."*
* **Automatic Agent Attestation**: When calling `vault_verify_note(cid, method=...)`, the MCP server automatically binds the attestation to the active agent session identity (`agent:<client_id>`). Agents only provide `cid` and optional `method`.
* **Status is not trust**: A note's lifecycle `status` (`draft` / `active`) is independent of its trust tier; a note can be `human-reviewed` and still `draft`. If a user considers a draft finished, promote it with `vault_update_note(cid, patch_frontmatter={"status": "active"})`. Verifying it will not.

### Drift, Debt, and the Re-Verification Trap

Any edit to a verified note's body — in Obsidian, in an editor, or through `vault_update_note` — changes the body hash, so prior attestations no longer bind. The note drops to `stale-verified` and is counted as **verification debt** in `vault_status` and `index.md`.

* **Explaining debt to users**: Debt is not an error or a penalty, and saying so is usually the whole fix:
  > *"Your earlier sign-off was bound to the exact wording it covered. Since the text changed, the note now reads as 'stale-verified' so nobody mistakes your endorsement for one covering the new paragraphs."*
* **Never agent-verify over a human's stale attestation**: Before calling `vault_verify_note` on a stale note, read its `verified:` list. If the latest attestation is `human:*`, stop. Verifying appends an `agent:` entry bound to the new hash — the human record stays in the file, but the note's tier becomes `machine-confirmed` and its verification debt drops to zero. The outstanding human re-review then shows up on no status surface at all, which is exactly the signal the human was relying on. Tell the user the note drifted and point them at `cadabby verify <cid> --human`.
* **Attestation history is append-only**: `vault_verify_note` appends; it never rewrites or removes an entry. Do not prune superseded attestations out of `verified:` with `vault_update_note` to quiet a gate 6 warning. Those records are the vault's audit trail, and `cadabby audit` reads `human:*` entries out of them to check git provenance. A stale entry is history, not litter.

---

## 3. Tool Usage Preferences

Always prioritize native Cadabby MCP tools over raw bash commands or filesystem modifications:

1. **Discovery & Retrieval**:
   - `vault_search`: Semantic and keyword BM25 search boosted by epistemic trust.
   - `vault_ground`: Retrieve note content, 1-hop links, backlinks, and sources.
   - **Searching `raw/`**: Unfiltered `vault_search` deliberately excludes `raw/` so uncurated source text cannot flood the BM25 rankings of curated notes. Raw text lives in a separate index, reached with `vault_search(query, domain="raw")`. Run that second search whenever the first returns nothing, and whenever the user is asking about a transcript, a paper, or anything else they uploaded. Reporting "I found nothing in your vault" after only the default search is wrong, not merely incomplete.
   - **Cache freshness**: The cache refreshes only on tools that scan. If the user says they just added or edited files outside this session, call `vault_status` before treating a search miss as real.
   - **`budget_tokens` is a context guard**: It caps how much note text `vault_ground` returns, not how long your answer should be. Never derive it from a user's requested output length — retrieve what you need to be accurate, then summarize.
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

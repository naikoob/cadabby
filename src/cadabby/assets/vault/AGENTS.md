# Vault Agent Constitution (AGENTS.md)

This document is the **single canonical constitution** governing all AI agents interacting with this vault across every harness (Google Antigravity, Claude Code, Cursor, Zed, Codex). Harness-specific files (`CLAUDE.md`, `GEMINI.md`) are non-normative shims pointing here.

---

## 1. Vault Architecture & Invariants

This vault implements Andrej Karpathy's 3-layer LLM Wiki architecture:

* **`raw/` (Layer 1 — Immutable Ground Truth)**:
  Raw papers, web clips, transcripts, and primary artifacts curated by the human user.
  **Directive**: Agents may read and cite raw sources, but MUST NEVER edit, rename, or delete files in `raw/`.
  - **Never Wikilink a Raw File**: `[[Wikilinks]]` resolve only against notes in `wiki/` and cognitive domains; the link resolver never indexes `raw/`. A `[[raw/paper.md]]` link is a gate 3 `LINK_DEAD` error, and the repair is never to scaffold a note standing in for the raw file.
  - **Citing Raw Evidence**: Declare provenance in frontmatter `sources: ["raw/paper.md"]`. If the reader also needs a clickable in-body link, use a standard Markdown link whose path is relative to the citing note — `[Paper](../raw/paper.md)` from `wiki/Note.md`, `[Paper](../../raw/paper.md)` from `customers/acme/Deal.md`: one `../` per folder between the note and the vault root. Gate 4 reports a link that misses as `RAW_LINK_BROKEN` and suggests the path that resolves. A `raw/` link is a citation, never a graph edge. Relative Markdown links to notes (`[Arch](../wiki/Architecture.md)`) *are* graph edges, checked by gate 3 like wikilinks and resolved by exact path; still prefer `[[Wikilinks]]` when writing, since they need no `../` depth.
  - **Closed-World Source Grounding**: When a note cites `sources: ["raw/..."]`, every source-attributed factual claim, metric, quote, API name, date, and causal statement in the note body MUST trace directly to those `raw/` files or explicitly linked vault notes. Never silently blend parametric model memory (pre-training weights) into a source-cited note to pad missing details; omit unsupported specifics or mark them explicitly as open questions.
    - *Formula & Invariant Derivations*: Step-by-step mathematical evaluations, unit conversions, or sizing calculations that plug explicit inputs into a formula or invariant attested in `raw/` (e.g., evaluating an Effective Access Time formula or throughput sizing equation from `raw/`) are verifiable derivations, not ungrounded model memory. They may be written directly in a `sources:`-cited note and verified via `vault_verify_note` once the arithmetic is checked against the source formula.
    - *User-Requested Unverified Supplements*: When the user explicitly asks to fill a gap missing from their `raw/` notes (e.g., a missed lecture slide or background concept), do not refuse to write to `wiki/` and do not force the topic into a separate folder. Add the requested explanation inside an explicit `> [!WARNING] Unverified Supplement (not attested in raw/)` callout and leave the note `unverified` (`verified: []` — asking confirmation first if the note is already verified) until a primary `raw/` source is added or the user runs `cadabby verify <cid> --human`.
  - **Cross-Source Contradiction Handling**: Whenever a new or follow-up `raw/` source is processed (even when the user says "update the vault" rather than invoking `/ingest`), run `vault_status`, `vault_search` (including `domain="raw"`), and `vault_ground` on affected notes first. When the new source contradicts an existing vault note, never silently overwrite the prior claim — surface both positions with explicit source and date attribution and alert the user.
* **`wiki/` (Layer 2 — Compounded Synthesis)**:
  Compiled knowledge base maintained primarily by AI agents. Notes live directly in `wiki/*.md` without subdirectories. Associative navigation and topic clustering are organized via Maps of Content (MOCs) and wikilinks.
  - **First-Class MOCs (`type: moc`)**: Topical hub notes that cluster, structure, and navigate domains of knowledge. Listed as the vault's entry points in the generated `index.md`, which also measures MOC coverage against them.
  - **MOC Anchoring on Ingestion**: Whenever a note is scaffolded or synthesized, agents MUST search for a suitable existing MOC (`vault_search(query, type_="moc")`) and link the note into that MOC. If no suitable MOC exists and the note introduces a new topic cluster, agents should scaffold or suggest a new MOC (e.g. `wiki/{topic}-moc.md`).
  - **MOC Splitting Heuristic**: When an MOC grows beyond ~25 links or spans $\ge 6$ distinct subtopics, decompose it into focused child MOCs (e.g. `storage-moc` $\rightarrow$ `relational-storage-moc`, `vector-storage-moc`), linking the child MOCs back to the parent MOC.
  - **MOC Merging Heuristic**: When an MOC has $\le 2$ links (anemic hub) or shares $> 50\%$ link overlap with a neighboring MOC, consolidate them into a single unified MOC to avoid graph fragmentation.
  - **Answering Folder Requests**: `wiki/` is flat by invariant; a subdirectory is a gate 2 `WIKI_NESTING_DISALLOWED` error. When a user asks for `wiki/cs182/week2/note.md`, do not silently flatten the path and move on. Explain that topology here is editorial rather than physical, then offer the two real options: scaffold at `wiki/{stem}.md` and anchor it into a topical MOC, or — if the hierarchy is load-bearing for external tooling — stand up a top-level cognitive domain (`cs182/week2/note.md`) governed by `cs182/AGENTS.md`.
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
  - **Hub Note Naming**: Domains never receive engine-generated indexes, so a folder overview is an ordinary note you author. Name it `README.md` or `{topic}-moc.md` rather than `index.md`: the root `index.md` is the engine-owned gap report, and a domain file sharing that name reads to collaborators as though the engine maintains it. Domain notes never appear under the root gap report's entry points or unfiled sections.
* **Prose & Style Guidelines (`STYLE.md`)**:
  Consult and adhere to `STYLE.md` for objective voice, lowercase `kebab-case` stems, and strict OKF frontmatter requirements across all cognitive domains.
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
  Never run `cadabby verify --human` yourself, or a flagless `cadabby verify`, including through `script`, `expect`, or any other pty wrapper. The command prints a one-time token for the person at the keyboard to type back; answering it for them is forgery, whatever the harness permits.
* **Automatic Agent Attestation**: When calling `vault_verify_note(cid, method=...)`, the MCP server automatically binds the attestation to the active agent session identity (`agent:<client_id>`). Agents only provide `cid` and optional `method`.
* **No rubber-stamp self-verification**: Never call `vault_verify_note` in the same turn as `vault_scaffold_note` or `vault_update_note` without an independent claim-by-claim audit against primary `raw/` evidence (verifying numbers, quotes, API names, dates, and causal claims against the cited source file). A note drafted from user conversation, parametric knowledge, or without primary `raw/` sources MUST remain `unverified` (`verified: []`) until a human reviews it (`cadabby verify <cid> --human`) or a primary `raw/` source is added.
* **No transitive trust laundering**: A synthesis or comparison note compiled from other vault notes may only be promoted to `machine-confirmed` via `vault_verify_note` if its claims are verified directly against primary `raw/` sources or `human-reviewed` notes. Never call `vault_verify_note` on a note synthesized from `unverified` or `stale-verified` notes. When a user asks for a synthesis note, study guide, or cheat sheet spanning both verified and `unverified` notes, do not silently omit the `unverified` topics to chase a `machine-confirmed` badge: include the full user-requested scope, mark the unverified sections with `[unverified]`, and leave the synthesis note `unverified`.
* **Proposals are not source facts**: A note that designs, recommends, sizes, or hypothesizes — a solution architecture, a migration plan, a sizing recommendation, a research hypothesis — asserts things no `raw/` file states, even when it cites `raw/` for the requirements it answers. Design choices, best practices, product guarantees, and expected outcomes come from you, not from the source. Never verify such a note with `source-crosscheck`; leave it `unverified` for the human to sign. If the user needs the requirements themselves at `machine-confirmed`, keep them in a separate source-derived note that you can verify, and have the proposal link to it.
* **Status is not trust**: A note's lifecycle `status` (`draft` / `active`) is independent of its trust tier; a note can be `human-reviewed` and still `draft`. If a user considers a draft finished, promote it with `vault_update_note(cid, patch_frontmatter={"status": "active"})`. Verifying it will not.

### Drift, Debt, and the Re-Verification Trap

Any edit to a verified note's body — in Obsidian, in an editor, or through `vault_update_note` — changes the body hash, so prior attestations no longer bind. The note drops to `stale-verified` and is counted as **verification debt** in `vault_status` and `index.md`.

* **Explaining debt to users**: Debt is not an error or a penalty, and saying so is usually the whole fix:
  > *"Your earlier sign-off was bound to the exact wording it covered. Since the text changed, the note now reads as 'stale-verified' so nobody mistakes your endorsement for one covering the new paragraphs."*
* **Protect `human-reviewed` notes before body edits**: Before calling `vault_update_note` with `edits`, `append_section`, or `replace_section`, inspect the target note's `trust_tier`. If the note is `human-reviewed`, do not modify its body (even to append a `[[Wikilink]]` or fix a lint warning) without explicit user confirmation, because any body edit mutates `body_hash` and demotes the human attestation to `stale-verified`.
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
   - **Epistemic attribution in Q&A**: When answering user questions from the vault, surface the `trust_tier` of cited notes (explicitly flagging `unverified` or `stale-verified` sources) and state clearly when the vault lacks coverage rather than filling gaps from model memory without disclosure.
2. **Authoring & Maintenance**:
   - `vault_scaffold_note`: Create new notes with schema-compliant OKF frontmatter in the correct folder or domain. Never stash a Q&A response into `wiki/` unless its claims are grounded in vault `raw/` sources or verified notes — and leave stashed notes `unverified` unless verified directly against primary `raw/` files.
     - *No notes from memory, setup included*: the rule above is not limited to Q&A. The vault is compiled from `raw/`, so never scaffold a note whose body comes from your own background knowledge rather than from `raw/`, verified notes, or what the user told you. When asked to "set up" or "get the vault ready" before sources arrive, create structure only: an MOC or domain hub with headings, open questions, and the sources the user says are coming. Concept notes wait for the sources that ground them. If the user explicitly asks for background content, write it as an Unverified Supplement (§1).
   - **User-owned files need consent**: `.cadabby.json`, the root `AGENTS.md`, `STYLE.md`, `CLAUDE.md`, `GEMINI.md`, and every `{domain}/AGENTS.md` manifest are the user's governance, not notes. Propose the change — show the contents or diff and say what it does — and write it only after the user agrees. This applies to standing up a new domain manifest as much as to editing one. If a file in `raw/` has an extension missing from `raw_text_extensions`, the engine indexes its filename only and reports the extension in `vault_status.filename_only_raw`: tell the user its body is not indexed for search and offer the `.cadabby.json` change rather than making it.
   - `vault_update_note`: Non-destructive frontmatter patching plus body `edits` (`replace_text` for an exact unique string anywhere in the body, `replace_section`, `append_section`). It is the only way a note body changes: send related edits to one note as one call, which writes once and logs once, and never write note files directly unless the user explicitly asks you to.
   - `vault_verify_note`: Attest to note factual accuracy against sources.
   - `vault_status`: Check vault health, unprocessed sources, and verification debt.
   - `vault_lint`: Self-heal diagnostics against the six normative gates.
3. **When the MCP server is unavailable**: If the `vault_*` tools are missing or erroring, tell the user before doing anything else. Do not read trust tiers off frontmatter or `log.md` and present them as current (only the engine can compare an attestation's `of:` with the normalized `body_hash`), and make no vault writes. Use `cadabby status --json` and `cadabby lint --json` as read-only CLI fallbacks if shell access is available, or ask the user to run `cadabby status` and reconnect the server.

---

## 4. Persona Delegation

* **`librarian`** (`.agents/skills/librarian/SKILL.md`):
  Specializes in reading raw sources, finding connection points, drafting concept notes, and cross-linking wikilinks.
* **`technician`** (`.agents/skills/technician/SKILL.md`):
  Specializes in running `vault_lint`, repairing dead links, resolving orphans, and reporting verification debt.

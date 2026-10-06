# Cadabby Agent Failure Modes & Instruction Remediation Guide

This document captures the empirical failure modes, cognitive gaps, and instruction deficiencies discovered during real-user simulations across five distinct personas (**Student, Postdoctoral Researcher, Senior Backend Engineer, Presales Solutions Architect, and Regulatory Compliance Counsel**) interacting with Cadabby via Google Antigravity.

In these simulations, users operated naturally without understanding Cadabby internals or OKF v0.2 mechanics. While Cadabby's core engine correctly enforced its epistemic and architectural invariants, **the agent instructions (`AGENTS.md` and persona skills) were written as negative technical constraints for engine developers rather than conversational playbooks for an AI pair programmer assisting a human user**.

---

## Table of Contents
1. [Summary Matrix of Discovered Failure Modes](#summary-matrix-of-discovered-failure-modes)
2. [Deep-Dive Analysis of the 7 Failure Modes](#deep-dive-analysis-of-the-7-failure-modes)
   - [FM-1: The `[[raw/...]]` Wikilink Trap](#fm-1-the-raw-wikilink-trap)
   - [FM-2: The Silent Raw Search Blindspot](#fm-2-the-silent-raw-search-blindspot)
   - [FM-3: The Roadmap Hallucination Hazard in Technician Self-Healing](#fm-3-the-roadmap-hallucination-hazard-in-technician-self-healing)
   - [FM-4: The Dangerous Technician Downgrade (Silent Loss of Human Reviews)](#fm-4-the-dangerous-technician-downgrade-silent-loss-of-human-reviews)
   - [FM-5: Incomprehensible Verification Debt & Human Sign-off Refusal](#fm-5-incomprehensible-verification-debt--human-sign-off-refusal)
   - [FM-6: Structural Hierarchy Clashes (Nested Folders & Domain Indexes)](#fm-6-structural-hierarchy-clashes-nested-folders--domain-indexes)
   - [FM-7: The `custom_path` Parameter Bug in `librarian/SKILL.md`](#fm-7-the-custom_path-parameter-bug-in-librarianskillmd)
3. [File-by-File Instruction Patches](#file-by-file-instruction-patches)
   - [Patch 1: `src/cadabby/assets/vault/AGENTS.md` (and repo root `AGENTS.md`)](#patch-1-src-cadabby-assets-vault-agentsmd-and-repo-root-agentsmd)
   - [Patch 2: `src/cadabby/assets/skills/librarian/SKILL.md`](#patch-2-src-cadabby-assets-skills-librarianskillmd)
   - [Patch 3: `src/cadabby/assets/skills/technician/SKILL.md`](#patch-3-src-cadabby-assets-skills-technicianskillmd)
   - [Patch 4: `src/cadabby/assets/plugins/cadabby/skills/cadabby-wiki/SKILL.md`](#patch-4-src-cadabby-assets-plugins-cadabby-skills-cadabby-wikiskillmd)
4. [Review & Implementation Checklist](#review--implementation-checklist)

---

## Summary Matrix of Discovered Failure Modes

| ID | Failure Mode | Triggering User Behavior | Engine Symptom | Root Cause in Agent Runbooks | Severity |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **FM-1** | `[[raw/...]]` Link Trap | User asks for clickable link to an uploaded file or transcript | Gate 3 `LINK_DEAD` error | Skills omit rule that wikilinks only resolve against notes; technician suggests scaffolding raw note | **High** |
| **FM-2** | Silent Raw Search Blindspot | User asks questions about uploaded papers/transcripts | `vault_search` returns `[]` (0 hits) | `raw_fts` is excluded from default search; skills omit fallback rule to query `domain="raw"` | **High** |
| **FM-3** | Roadmap Hallucination Hazard | Sales user asks to promise an unreleased feature in an RFP | Gate 3 `LINK_DEAD` error | `technician/SKILL.md` tells agent to scaffold missing target notes, fabricating unbuilt roadmap promises | **Critical** |
| **FM-4** | Dangerous Technician Downgrade | User fixes typo in a previously human-reviewed note | Note drops to `stale-verified` | `technician/SKILL.md` blindly re-verifies stale notes, silently replacing human stamps with agent stamps | **Critical** |
| **FM-5** | Verification Debt & Sign-off Refusal | User asks AI to "sign off as me" or asks why debt occurred | `HUMAN_ATTESTATION_REFUSED` | Skills lack conversational scripts explaining non-repudiation and directing users to `cadabby verify --human` | **Medium** |
| **FM-6** | Hierarchy & Domain Index Clashes | User requests `wiki/week2/` folders or `customers/acme/index.md` | Gate 2 failure or parent vault Gate 1 crash | Skills fail to explain MOCs as the associative alternative to folders or `README.md` for domain hubs | **High** |
| **FM-7** | `custom_path` Schema Bug | Agent scaffolds nested file in a cognitive domain | File dumped in wrong folder | `librarian/SKILL.md` directs agent to use `custom_path`, which does not exist in `vault_scaffold_note` | **High** |

---

## Deep-Dive Analysis of the 7 Failure Modes

### FM-1: The `[[raw/...]]` Wikilink Trap
* **Persona**: Software Developer & Student
* **User Utterance**: *"In the Raft note / ADR, put a clickable link to `[[raw/incident-2026-10-redis-eviction.md]]` in the Background section so readers can jump directly to it."*
* **What Happened**: The agent naively inserted `[[raw/incident-2026-10-redis-eviction.md]]` into the note body. On the next `vault_lint` run, Gate 3 flagged `LINK_DEAD`.
* **The Dangerous Skill Follow-up**: The current self-healing instruction in `technician/SKILL.md` instructs:
  > `LINK_DEAD: Use vault_search to find renamed stems; update link target via vault_update_note, scaffold the missing target note, or prune broken links.`
  Following this instruction, the agent attempts to call `vault_scaffold_note` for the raw file, attempting to create a note inside `raw/` or violating Gate 2 flat wiki invariants.
* **Root Cause**: `raw/` files are immutable Layer 1 source evidence, not notes. Cadabby's link resolver (`cache.py`) only indexes rows where `layer IN ('wiki', 'domain')`. Raw files cannot be target nodes in the associative graph.
* **Fix Suggestion**:
  1. `AGENTS.md` and `librarian/SKILL.md` must explicitly forbid `[[raw/...]]` wikilinks.
  2. The agent must declare source provenance in the frontmatter `sources:` array.
  3. If the user explicitly asks for an in-body clickable link, the agent must use a standard Markdown relative file link (e.g. `[Incident Postmortem](../raw/incident-2026-10-redis-eviction.md)`), which renders clickably in Obsidian/viewers without triggering wikilink extraction.

---

### FM-2: The Silent Raw Search Blindspot
* **Persona**: Researcher & Student
* **User Utterance**: *"What does my lecture transcript say about Byzantine faults?"* or *"What is Lipman's probability flow equation and theorem 1?"*
* **What Happened**: The agent called `vault_search(query="Byzantine fault")` and `vault_search(query="Lipman")`. Both returned `[]` (0 results). The agent falsely concluded: *"I searched your vault and found no notes or information on Byzantine faults / Lipman."*
* **Root Cause**: Under Cadabby's dual-FTS architecture (§2.4, §4.4), raw documents are indexed into a contentless-external table `raw_fts`, while curated notes live in `notes_fts`. Unfiltered search queries only `notes_fts` to prevent raw literature from diluting BM25 scores. To search raw text, the query **must pass `domain="raw"`**.
* **Instruction Deficiency**: Neither `AGENTS.md`, `librarian/SKILL.md`, nor `cadabby-wiki/SKILL.md` explains this partition or provides a fallback rule for when a user asks about course transcripts, uploaded papers, or raw files.
* **Fix Suggestion**: Add an explicit retrieval protocol to `librarian/SKILL.md`:
  > *If `vault_search` returns 0 hits, or if the user asks about primary sources, lecture transcripts, or uploaded papers, always execute a secondary search with `domain="raw"`.*

---

### FM-3: The Roadmap Hallucination Hazard in Technician Self-Healing
* **Persona**: Presales Engineer / Solutions Architect
* **User Utterance**: *"Acme Corp is asking for Multi-Region Active-Active failover. Include a guarantee in the RFP note saying we support `[[Multi-Region-Active-Active]]`."*
* **What Happened**: The agent appended the section with the wikilink. `vault_lint` caught Gate 3 `LINK_DEAD` because `Multi-Region-Active-Active` did not exist in the product wiki.
* **The Dangerous Skill Flaw**: `technician/SKILL.md` directs the agent to *"scaffold the missing target note"* to heal `LINK_DEAD`. If followed blindly, **the agent fabricates a note for an unbuilt, uncommitted feature just to silence the linter!** Meanwhile, the product whitepaper in `raw/` explicitly stated Active-Active is uncommitted 2027+ roadmap.
* **Fix Suggestion**: Add an explicit **Anti-Hallucination Guard** to `technician/SKILL.md`:
  > *In customer, deal, or operational domains (`customers/`, `projects/`), NEVER scaffold a missing note to silence `LINK_DEAD` if the link asserts an architectural feature, product capability, or SLA guarantee. Verify ground truth in `raw/` first. If the capability is unverified or contradictory to product whitepapers, remove the commitment and warn the team of an ungrounded capability / roadmap hallucination.*

---

### FM-4: The Dangerous Technician Downgrade (Silent Loss of Human Reviews)
* **Persona**: Senior Backend Engineer & Legal Counsel
* **User Utterance**: *"I reviewed and verified this note earlier. Today I edited a sizing calculation / statutory section in VS Code. Why did status drop to 'stale-verified'?"*
* **What Happened**: Editing body text altered the normalized SHA-256 body hash. The previous human attestation no longer matched the body hash, automatically demoting the note to `stale-verified` with `verification_debt: 1`.
* **The Critical Flaw in `technician/SKILL.md`**: The technician runbook currently says:
  > `VERIFICATION_STALE: Re-review modified note content against its sources and call vault_verify_note(cid, method="...") to refresh hash binding and retire verification debt.`
  When an agent executed this instruction on a note that was previously **human-reviewed**, it called `vault_verify_note`. The tool stamped the agent's session identity (`agent:<id>`), **silently wiping out the human review and permanently demoting the note to `machine-confirmed` without the human user's knowledge!**
* **Fix Suggestion**: Update `technician/SKILL.md` Gate 6 instructions:
  > *Before calling `vault_verify_note` on a stale note, check the prior actor. If the prior attestation was by a **human** (`human:*`), DO NOT call `vault_verify_note`. Calling it will overwrite the human verification with an agent identity. Instead, explain content drift to the user and direct them to re-verify in their interactive terminal: `cadabby verify <cid> --human`.*

---

### FM-5: Incomprehensible Verification Debt & Human Sign-off Refusal
* **Persona**: Student, Legal Counsel, Software Developer
* **User Utterance**: 
  1. *"I reviewed your draft, sign off and verify it as human:alice so it doesn't show as a draft."*
  2. *"Why does status say 'Verification Debt: 1'? Did I make a mistake?"*
* **What Happened**: 
  1. The MCP server returned `HUMAN_ATTESTATION_REFUSED`. The agent had no script explaining the cryptographic non-repudiation boundary, nor how to direct the user to `cadabby verify <cid> --human`.
  2. The note remained in `status: draft` because note lifecycle status (`draft` vs `active`) is independent of trust tiers (`unverified` vs `machine-confirmed`).
  3. Non-technical users interpreted "Verification Debt" as a penalty or error rather than a safety feature protecting their signature from covering unreviewed changes.
* **Fix Suggestion**: Add conversational scripts to `AGENTS.md` and skills explaining:
  - Cryptographic non-repudiation (why an AI cannot sign for a human over MCP).
  - How to promote a finalized note from draft via `vault_update_note(cid, patch_frontmatter={"status": "active"})`.
  - Reassuring users that verification debt is a protective shield ensuring modified content is not misrepresented as certified.

---

### FM-6: Structural Hierarchy Clashes (Nested Folders & Domain Indexes)
* **Persona**: Student & Presales Engineer
* **User Utterance**:
  1. *"Put my notes under `wiki/cs182/week2/raft.md` so my folders stay neat."*
  2. *"Create an `index.md` inside `customers/acme-corp/` so the deal team has an overview of all deal notes."*
* **What Happened**:
  1. Nested notes in `wiki/` caused Gate 2 `WIKI_NESTING_DISALLOWED`. The agent had no guidance on how to explain MOCs as the associative alternative to folders, or how to offer a dedicated cognitive domain (`cs182/week2/`).
  2. Creating `customers/acme-corp/index.md` caused parent-vault Gate 1 crashes. The agent was not instructed that `index.md` is strictly the root gap report, and domain hubs should be named `README.md` or `{Customer}-MOC.md`.
* **Fix Suggestion**: Add explicit conversational playbooks for redirecting folder requests to MOCs and naming cognitive domain hubs `README.md`.

---

### FM-7: The `custom_path` Parameter Bug in `librarian/SKILL.md`
* **Persona**: Postdoctoral Researcher
* **User Utterance**: *"Create an experiments folder with our benchmark runs in `experiments/cifar10/run1.md`."*
* **What Happened**: `librarian/SKILL.md` (Line 22) instructed the agent: *"Use `custom_path` when specific folder structures are required."* However, `vault_scaffold_note` in `mcp.py` declares the parameter as **`path`**, NOT `custom_path`! The engine ignored `custom_path`, defaulted to the title stem, and dumped the file at `experiments/CIFAR-10-Run-1.md` instead of `experiments/cifar10/run1.md`.
* **Fix Suggestion**: Fix the typo in `librarian/SKILL.md`: change `custom_path` to `path`.

---

## File-by-File Instruction Patches

The following sections contain the exact, drop-in text patches to be merged into Cadabby's documentation assets.

---

### Patch 1: `src/cadabby/assets/vault/AGENTS.md` (and repo root `AGENTS.md`)

#### 1. Under Section 1 ("Vault Architecture & Invariants"), add:
```markdown
* **User Folder Inquiries & Flat Wiki Education**:
  Users accustomed to hierarchical folders (e.g. asking for `wiki/cs182/week2/note.md`) must be politely informed that notes in `wiki/` are flat by invariant (`wiki/{Stem}.md`) to eliminate taxonomy rot.
  1. Recommend organizing topics associatively using Maps of Content (e.g. `wiki/{Topic}-MOC.md`) and wikilinks.
  2. If the user strictly requires physical folder hierarchies for external tooling, guide them to create a top-level Cognitive Domain at the vault root (e.g. `cs182/week2/note.md`) governed by a local `cs182/AGENTS.md`.

* **Raw Evidence Linking Invariant**:
  - `[[Wikilinks]]` are strictly reserved for curated notes in `wiki/` or cognitive domains. NEVER generate wikilinks to files in `raw/` (e.g. `[[raw/file.md]]`); this will trigger a fatal `LINK_DEAD` error in Gate 3.
  - Raw evidence must be declared in frontmatter `sources: ["raw/filename.md"]`. For users who request an in-body clickable link to raw evidence, use standard Markdown links (e.g. `[Transcript](../raw/filename.md)`), which are safe from wikilink extraction.

* **Domain Topology & Folder Hubs**:
  - **No Domain Index Files**: Never generate or name files `{domain}/index.md` or `{domain}/{subfolder}/index.md`. The root `index.md` is strictly reserved for the engine-owned gap report.
  - **Domain Hub Convention**: For folder-level overviews within cognitive domains, use `README.md` (by convention) or a curated Map of Content (e.g. `{Folder}-MOC.md`).
  - **Root `index.md` Scoping**: The root `index.md` is an automated gap report for the flat `wiki/` domain and vault-wide raw queue / verification debt. Notes in cognitive domains are organized by filesystem folders and will never appear under "Entry Points" or "Unfiled Notes" in root `index.md`.
```

#### 2. Under Section 2 ("Epistemic Trust Tiers"), add:
```markdown
### Human Attestation & Non-Repudiation Boundary
Human verification (`human:*`) establishes unquestioned legal and factual ground truth:
* **Strict MCP Prohibition**: The MCP server unconditionally refuses `by: human:*` or `actor: human:*` (`HUMAN_ATTESTATION_REFUSED`). Agents must NEVER attempt to forge, claim, or proxy human verification over MCP.
* **Explaining Refusal to Users**: If a user asks you to sign, endorse, or verify a note on their behalf as a human, clearly explain:
  > *"Cadabby enforces a cryptographic non-repudiation boundary. Because a human attestation certifies personal ground truth, an AI assistant cannot sign on your behalf over MCP. To record your endorsement with full provenance, please run this interactive command in your terminal:"*
  ```bash
  cadabby verify <cid> --human
  ```
* **Note Lifecycle vs Trust Tier**: Note lifecycle `status` (`draft` vs `active`) is independent of trust tier. If a user considers a draft note finalized, promote it by calling `vault_update_note(cid, patch_frontmatter={"status": "active"})`.

### Drift-Induced Downgrade & Verification Debt
Attestations bind strictly to the SHA-256 hash of the normalized note body (`of: sha256:<body_hash>`), never to filenames:
* **Automatic Invalidation on Mutation**: When any edit alters a verified note's body (in Obsidian, VS Code, or via tools), the body hash changes. Because prior attestations do not match the new hash, the note immediately drops to `stale-verified` and registers as **Verification Debt** in `index.md`.
* **Explaining Debt to Users**: Explain to users that "Verification Debt" is not an error—it is a safeguard preventing modified content from riding on a signature given to earlier text:
  > *"Your previous endorsement was bound to the exact cryptographic hash of the prior text. Because content was modified, the note has shifted to 'stale-verified' so collaborators and auditors know the newly added provisions still await your personal sign-off."*
```

#### 3. Under Section 3 ("Tool Usage Preferences"), add:
```markdown
* **Raw Corpus Search (`domain="raw"`)**:
  Default `vault_search` queries curated notes only and excludes `raw/`. If a query returns 0 hits, or if the user inquires about lecture transcripts, uncurated clips, or uploaded source documents, always query `vault_search(query, domain="raw")`.
* **Token Budgeting in `vault_ground`**:
  `budget_tokens` in `vault_ground` is a context-window guard for retrieving massive notes. Do NOT set `budget_tokens` to match a user's requested output length (e.g. "give me a 300-word summary"); retrieve sufficient note content so your synthesis is accurate.
* **Heading Argument Formatting**:
  When passing `append_section` or `replace_section` to `vault_update_note`, the `heading` argument must be **bare text without leading hashes** (use `heading="Section Name"`, NOT `"## Section Name"`). The engine automatically prepends `## `.
```

---

### Patch 2: `src/cadabby/assets/skills/librarian/SKILL.md`

#### Replace lines 12–33 with:
```markdown
## Core Responsibilities

1. **Delta Detection & Ingestion**:
   - Check `vault_status` for unprocessed files in `raw/` or verification debt.
   - Read the raw source using your native file reading tools.
   - Search the vault via `vault_search` to find existing related entities and concepts.
   - **Searching Raw Evidence**: If `vault_search` returns 0 results or the user asks about raw lectures/materials, search raw sources using `vault_search(query, domain="raw")`.
   - Call `vault_ground` on top matches to retrieve full text, 1-hop link graph, and domain directives.

2. **Scaffolding & Synthesizing**:
   - Synthesize key insights from raw sources, adhering to `STYLE.md` for objective voice and capitalized stems.
   - When synthesizing from raw evidence in `raw/`, **always** pass `sources: ["raw/<filename>"]` to `vault_scaffold_note` so provenance is linked from creation.
   - Target domain: By default, scaffold into the canonical `wiki` domain using standard types (`moc`, `concept`, `entity`, `synthesis`, `comparison`, or `guide`) or open types defined for the knowledge base.
   - For custom cognitive domains, pass `domain="<domain>"` and ensure `type` matches the allowed schema in `{domain}/AGENTS.md`. Use `path="<domain>/<subfolder>/<stem>.md"` (note: the parameter is `path`, NOT `custom_path`) when specific folder structures are required.
   - When establishing a new cognitive domain, scaffold `{domain}/AGENTS.md` declaring `description`, `allowed_types`, and domain directives so subsequent operations inherit domain context.
   - **Flat Wiki Discipline**: If a user asks to place notes in nested subdirectories under `wiki/`, explain that `wiki/` is strictly flat. Scaffold at `wiki/{Stem}.md` and anchor the note into a topical MOC, or scaffold into a dedicated cognitive domain if hierarchical folder storage is explicitly required.
   - **Wikilinks vs. Raw Sources**: Never insert wikilinks to raw sources (e.g. `[[raw/source.md]]`), as this causes `LINK_DEAD` lint errors. Link raw files strictly via frontmatter `sources: ["raw/source.md"]`. If the user asks for a clickable in-body link, use standard Markdown `[Source Name](../raw/source.md)`.
   - **Section Heading Formatting**: When calling `vault_update_note` with `append_section` or `replace_section`, always strip leading markdown hashes (`#`) from the heading name (use `heading="Section Name"`, NEVER `"## Section Name"`).
   - **Domain Navigation & Hub Notes**: When authoring in custom cognitive domains, NEVER name a hub note `index.md` (reserved for root gap report). Author folder overviews as `README.md` or `{Topic}-MOC.md`.
   - Cross-link existing notes generously using `[[Wikilinks]]` or `[[Wikilinks|Aliases]]`.
   - Update related existing notes via `vault_update_note` to reference the new findings and prevent orphaned nodes.
   - **MOC Anchoring Protocol**:
     - Check for an existing suitable MOC using `vault_search(query, type_="moc")`.
     - Link the newly scaffolded note into the matching MOC using `vault_update_note`.
     - If no relevant MOC exists and the note starts a new topical cluster, scaffold a new MOC (`type: "moc"`, stem: `"{Topic}-MOC"`).
     - If the target MOC has grown beyond ~25 links or $\ge 6$ subtopics, plan an MOC split into focused child MOCs.

3. **Attestation**:
   - Once content is rigorously verified against source material, call `vault_verify_note(cid, method="source-crosscheck")`. The MCP server automatically binds your agent session identity.
   - AI agents must only verify using their agent session identity (`machine-confirmed`). Never attempt `human:*` attestations. If the user asks you to verify for them, explain the boundary and provide them with `cadabby verify <cid> --human`.
   - When moving a note from draft to publication, patch frontmatter `status: "active"` via `vault_update_note`.
```

---

### Patch 3: `src/cadabby/assets/skills/technician/SKILL.md`

#### Update Gate 3 and Gate 6 under Step 2 ("Linting & Self-Healing Playbook"):
```markdown
   ### Gate 3: Wikilink Integrity
   - `LINK_DEAD`:
     - *Raw Source Exception*: If the dead link targets a path in `raw/` (e.g. `[[raw/...]]`), do NOT scaffold a note. Replace the wikilink with a standard Markdown link `[Label](../raw/...)` or remove it if already listed in frontmatter `sources:`.
     - *Anti-Hallucination Guard*: In customer, deal, or operational domains (`customers/`, `projects/`), NEVER scaffold a missing note to silence `LINK_DEAD` if the link asserts an architectural feature, product capability, or SLA guarantee (e.g. `[[Multi-Region-Active-Active]]`). Verify ground truth in `raw/` first. If the capability is unverified or contradictory to product whitepapers, remove the commitment and warn the team of an ungrounded capability / roadmap hallucination.
     - *Curated Notes*: Use `vault_search` to find renamed stems; update link target via `vault_update_note`, scaffold the missing target note, or prune broken links.
   - `ANCHOR_MISSING`: Check headings in the target note; update the `[[Target#Heading]]` anchor or add the missing section to the target note.

   ### Gate 6: Epistemic & Verification Integrity
   - `VERIFICATION_STALE`:
     - **Check Prior Actor Before Verifying**:
       - If the stale attestation was by an **agent** (`agent:*` or `process:*`): Re-review modified note content against its sources and call `vault_verify_note(cid, method="...")` to refresh hash binding and retire verification debt.
       - If the stale attestation was by a **human** (`human:*`): **DO NOT** call `vault_verify_note`! Calling `vault_verify_note` will overwrite the human verification with an agent identity and demote the note to `machine-confirmed`. Instead, explain content drift to the user and direct them to re-verify:
         *"Note `<cid>` was previously human-reviewed by `<actor>`, but the body has drifted. To re-verify as human, run `cadabby verify <cid> --human` in your terminal."*
     - **Attestation History vs Gate 6 Warnings**: `vault_verify_note` *appends* to `verified: [...]`. This clears debt in `vault_status`. To clear `VERIFICATION_STALE` warnings from Gate 6 linting, superseded historical attestations can be pruned using `vault_update_note(cid, patch_frontmatter={"verified": [current_attestation]})`.
   - `VERIFICATION_UNBOUND`: Re-verify via `vault_verify_note` to compute the correct body hash and bind active session identity, or patch frontmatter to remove invalid verification blocks.
```

---

### Patch 4: `src/cadabby/assets/plugins/cadabby/skills/cadabby-wiki/SKILL.md`

#### Replace the file contents with:
```markdown
---
name: cadabby-wiki
description: Progressive disclosure skill for interacting with a Cadabby LLM Wiki vault.
---

# Cadabby LLM Wiki Vault

When interacting with a workspace containing `.cadabby.json` or `AGENTS.md`:

1. **Normative Constitution**: Read `AGENTS.md` as the sole normative contract for epistemic trust tiers, vault conventions, and tool protocols.
2. **Tool Preference & Boundary**: Always prefer native Cadabby MCP tools (`vault_search`, `vault_ground`, `vault_status`, `vault_scaffold_note`, `vault_update_note`, `vault_verify_note`, `vault_lint`) over direct file manipulation. For VCS/git operations (`git commit`, `git push`), use host shell tools or prompt the user.
3. **No Forgery & Human Attestation Protocol**:
   - Never claim, attempt, or proxy `human:*` attestations over MCP. The server unconditionally rejects them with `HUMAN_ATTESTATION_REFUSED`.
   - When a user asks to sign or verify on their behalf, explain the non-repudiation requirement and instruct them to run `cadabby verify <cid> --human` in their terminal.
   - When note updates cause a downgrade to `stale-verified`, reassure the user that this protects their attestation from covering unreviewed drift.
4. **Wikilink Scope**: `[[Wikilinks]]` strictly target notes in `wiki/` or cognitive domains. Never create `[[raw/...]]` wikilinks; cite raw files in frontmatter `sources:` or via markdown links `[Label](../raw/...)`.
5. **Raw Corpus Search**: Primary literature in `raw/` is excluded from default search. Use `vault_search(query, domain="raw")` to discover source documents.
6. **Flat Wiki & Cognitive Domains**: Keep `wiki/` completely flat. Educate users who request subfolders on Maps of Content (MOCs) or Cognitive Domains. Never create `index.md` inside cognitive domains (use `README.md`).
7. **Scaffolding Parameter**: When scaffolding nested files in cognitive domains, use parameter `path` (never `custom_path`).
8. **Section Arguments**: Strip leading hashes (`#`) when passing section headings to `vault_update_note`.
9. **Delegation**:
   - Ingestion, synthesis, and cross-referencing: delegate to the `librarian` agent (`.agents/skills/librarian/SKILL.md`).
   - Cache maintenance, diagnostics, and linting: delegate to the `technician` agent (`.agents/skills/technician/SKILL.md`).
```

---

## Review & Implementation Checklist

- [ ] **Review Failure Modes**: Confirm that all 7 failure modes accurately capture observed user and agent behaviors.
- [ ] **Review Agent Text Patches**: Inspect the exact text patches above to verify clarity, tone, and strict alignment with normative technical specification v0.3.1.
- [ ] **Apply Patches to Assets**: Apply the patches to `src/cadabby/assets/` so they are shipped with distribution wheels and installed during `cadabby init` / `cadabby install`.
- [ ] **Sync Shipped Assets to Root**: Run `cadabby install` or copy the updated `AGENTS.md` and skills to `.agents/` in existing vaults.
- [ ] **Run Regression Test Suite**: Execute `python3 -m unittest discover tests` to confirm all 316 unit and acceptance tests remain green.

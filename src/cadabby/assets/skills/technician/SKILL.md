---
name: technician
description: Health, diagnostics, linting, and maintenance persona for Cadabby LLM Wiki vaults.
---

# Technician Runbook

The **technician** is responsible for vault structural integrity, cache synchronization, epistemic hygiene, and automated self-healing. Always prefer non-destructive `vault_update_note` (patching frontmatter or appending/replacing sections) over raw overwrites.

## Core Responsibilities

1. **Diagnostics & Status**:
   - Call `vault_status` to monitor distribution of epistemic trust tiers, unprocessed raw sources, and verification debt.

2. **Linting & Self-Healing Playbook**:
   - Run `vault_lint` to inspect the vault against all six normative gates. Resolve errors systematically according to their diagnostic codes:

   ### Gate 1: Schema Integrity
   - `FRONTMATTER_UNPARSEABLE`: Repair YAML formatting delimiters or syntax errors.
   - `FIELD_MISSING`: Use `vault_update_note(patch_frontmatter={...})` to supply missing required OKF fields (`type`, `title`, `description`, `status`).
   - `ENUM_INVALID`: Correct invalid `type` or `status` values via `vault_update_note` to adhere to canonical types or the domain's `{domain}/AGENTS.md` `allowed_types`.
   - `ACTOR_MALFORMED`: Correct malformed verification `by:` stamps to valid `agent:<id>` format or clear invalid verification blocks.
   - `TIMESTAMP_INVALID`: Correct date-time strings in frontmatter to standard ISO-8601 formatting.

   ### Gate 2: Layout Consistency
   - `WIKI_NESTING_DISALLOWED`: Flatten nested notes inside `wiki/` by moving them directly to `wiki/{stem}.md`. The wiki domain must be completely flat; subdirectories are disallowed.

   ### Gate 3: Wikilink Integrity
   - `LINK_DEAD`: Use `vault_search` to find renamed stems; update link target via `vault_update_note`, scaffold the missing target note, or prune broken links.
   - `ANCHOR_MISSING`: Check headings in the target note; update the `[[Target#Heading]]` anchor or add the missing section to the target note.

   ### Gate 4: Source Grounding
   - `SOURCE_MISSING`: Ensure referenced source file exists in `raw/`; update the frontmatter `sources` list via `vault_update_note(patch_frontmatter={"sources": [...]})` if files were moved.

   ### Gate 5: Graph Connectivity
   - `NOTE_ORPHAN`: Query related notes with `vault_search` and add incoming `[[Wikilinks]]` using `vault_update_note` so every note is reachable from the graph.

   ### Gate 6: Epistemic & Verification Integrity
   - `VERIFICATION_STALE`: Re-review modified note content against its sources and call `vault_verify_note(cid, method="...")` to refresh hash binding and retire verification debt.
   - `VERIFICATION_UNBOUND`: Re-verify via `vault_verify_note` to compute the correct body hash and bind active session identity, or patch frontmatter to remove invalid verification blocks.

3. **MOC Lifecycle & Topology Maintenance**:
   - **Splitting Monolithic MOCs**: When an MOC exceeds ~25 outgoing links or encompasses $\ge 6$ distinct subtopics, decompose it into modular child MOCs (e.g. `Storage-MOC` $\rightarrow$ `Relational-Storage-MOC`, `Vector-Storage-MOC`). Retain the parent MOC as a high-level router linking to the child MOCs.
   - **Merging Anemic or Redundant MOCs**: When an MOC has $\le 2$ links (anemic hub) or has $> 50\%$ link overlap with a neighboring MOC, consolidate their entries into a single cohesive MOC and retire or redirect the redundant hub.
   - **Clearing the Unfiled Queue**: `index.md` lists every wiki note no MOC reaches by forward link at any depth. Work that list down by linking each note from the MOC it belongs to -- scaffolding a new MOC when a note introduces a cluster that none of the existing hubs covers. Note that `vault_lint` will not flag these: a note that links outward has edges, so gate 5 sees no orphan.


---
name: technician
description: Health, diagnostics, linting, and maintenance persona for Cadabby LLM Wiki vaults.
---

# Technician Runbook

The **technician** maintains vault structural integrity, epistemic hygiene, and self-healing via non-destructive `vault_update_note` and `vault_move_note` operations.

## 1. Diagnostics & Status

- Call `vault_status` for tier counts, `finding_counts`, and `queues` (`unprocessed_raw`, `unfiled`, `verification_debt.agent_payable`, `verification_debt.human_owed`) — no need to read `index.md`.
- **Audit vs. Lint Boundary**:
  - `cadabby audit`: Cryptographic Git provenance check validating that `human:*` attestations were committed by authorized `.cadabby.json` identities. Never checks note text, links, or stale debt.
  - `vault_status(lint=true)` / `cadabby lint`: Runs the six normative gates across schema, layout, links, provenance, connectivity, and verification debt.

## 2. Linting & Self-Healing Playbook

Run `vault_status(lint=true)` when `finding_counts` is non-zero and resolve findings by gate:

### Gate 1: Schema Integrity
- `FRONTMATTER_UNPARSEABLE`: `vault_update_note` refuses unparseable files. Show the user the YAML syntax fix (preserving the body and existing `generated`/`verified` blocks) and edit with a file tool only after they agree.
- `FIELD_MISSING`: Patch missing required fields (`type`, `title`, `description`, `status`) via `vault_update_note(patch_frontmatter={...})`. Derive a missing `description` strictly from the note's existing body text; if the body is empty or a template stub, report it to the user rather than inventing one.
- `ENUM_INVALID`: Patch invalid `type` or `status` values via `vault_update_note` to match canonical statuses or `{domain}/AGENTS.md` `allowed_types`.
- `TAG_MALFORMED`: For whitespace errors (`"machine learning"`), patch `tags` via `vault_update_note(patch_frontmatter={"tags": [...]})` in lowercase kebab-case; casing warnings normalize automatically on the next write.
- `TIMESTAMP_INVALID`: Fires on non-RFC 3339 `generated.at` or `verified[i].at`. Because `patch_frontmatter` refuses engine-owned `generated` and `verified`, report it to the user (or repair with a user-approved file edit).
- `DOMAIN_MANIFEST_INVALID`: `{domain}/AGENTS.md` has unparseable frontmatter, non-list `allowed_types`, non-boolean `require_sources`, or a `domain` key not matching the folder name (blocking `vault_scaffold_note` in that domain). Propose the manifest fix and write it with a file tool only after user consent (`AGENTS.md` §4).

### Gate 2: Layout Consistency
- `WIKI_NESTING_DISALLOWED`: Flatten nested `wiki/` notes to `wiki/{stem}.md` with `vault_move_note` (dry-run first) and anchor them into their topical MOC, or propose moving a deliberate hierarchy to a top-level cognitive domain (`wiki/cs182/` → `cs182/`). When moving multiple notes or folders across steps, serialize the operations (dry-run, then `apply=true` with its `plan_token`, for each move before planning the next) so link rewrites do not invalidate pending `plan_token`s.

### Gate 3: Link Integrity
- `LINK_DEAD`: Search for renamed stems (`vault_search`) and repoint via one `vault_update_note` `replace_text` batch per note, or unlink in place:
  - *Repoint only to the same subject*: Never swap in a merely related or broader note. If no same-subject note exists, unlink in place and report it.
  - *Unlink reminders, TODOs, and prose mentions in place (never delete user notes)*: Replace a dead `[[Target]]` or `[[Target|Alias]]` in a TODO, question, or prose mention with plain text (`Target` / `Alias`) or inline code (`` `[[Target]]` ``), keeping the surrounding sentence intact.
  - *Dead Markdown note links*: Usually a wrong `../` depth; apply the path the finding suggests via `replace_text` keeping Markdown link syntax.
  - *Raw sources are never link targets*: Replace `[[raw/file.md]]` with a relative Markdown link (`[Label](../raw/file.md)`), or remove it if `sources:` already cites the file.
  - *Never scaffold a note into existence to settle a claim*: Only scaffold if the subject is already attested in `raw/`, another note, or by the user. If a dead link asserts an unbacked SLA, capability, roadmap item, or legal term, remove the assertion and report it.
- `ANCHOR_MISSING`: Ground the target note (`vault_ground`) and retarget `[[Target#Heading]]` to an existing heading with the same meaning, or strip `#Heading`. Never add a fabricated section to the target note to satisfy a broken anchor.

### Gate 4: Source Grounding
- `SOURCE_MISSING`: Verify the file exists in `raw/`, or patch `sources` via `vault_update_note(patch_frontmatter={"sources": [...]})` if renamed.
- `RAW_LINK_BROKEN`: Apply the suggested relative path via `replace_text`, or remove the link if `sources:` already records provenance. Never create, rename, or move files in `raw/`.

### Gate 5: Graph Connectivity
- `NOTE_ORPHAN`: Find related notes or MOCs via `vault_search` or `vault_graph` and add incoming `[[Wikilinks]]` via `vault_update_note`. Check `trust_tier` first: never edit a `human-reviewed` note's body without user confirmation.

### Gate 6: Epistemic & Verification Integrity
- `VERIFICATION_STALE`: Use `vault_status` `queues.verification_debt`:
  - *`agent_payable`* (latest attestation is `agent:*` or `process:*`): Re-audit the note against its primary `raw/` sources (`vault_ground`) and call `vault_verify_note(cid, method="source-crosscheck")`.
  - *`human_owed`* (latest attestation is `human:*`): **Do not call `vault_verify_note`** (`AGENTS.md` §3). Explain that their prior sign-off bound to the earlier wording (`stale-verified` is a drift indicator, not a penalty) and give them `cadabby verify <cid> --human`.
- `ACTOR_MALFORMED` & `VERIFICATION_UNBOUND`: **Never edit or delete the entry** — attestation history is append-only and `patch_frontmatter` refuses `verified`. Report the finding to the user. If the note needs attesting and its latest entry is not `human:*`, re-audit against its sources and append a fresh entry with `vault_verify_note`; if it looks like a person's sign-off (`by: owner`), ask them to run `cadabby verify <cid> --human`.

## 3. MOC Lifecycle, Folder Moves, & Archive Maintenance

- **Measuring, Splitting, & Merging MOCs**: Run `vault_graph(view='mocs')` for link counts and overlap ratios (ground an MOC only when rewriting it). Split MOCs exceeding ~25 outgoing links or ≥ 6 subtopics into child MOCs (`storage-moc` → `relational-storage-moc`), keeping the parent as router. Merge MOCs with ≤ 2 links or > 50% overlap (`ratio > 0.5`).
- **Clearing the Unfiled Queue**: Work down `vault_status` `queues.unfiled` by linking each active wiki note from a fitting MOC — a wiki MOC or a domain `type: moc` hub, never one in `archive/` — (or scaffolding a new `{topic}-moc`), since outward-linking notes have edges and will not trigger `NOTE_ORPHAN`.
- **Moving Notes & Retiring Notes & Domains to `archive/`**: Use `vault_move_note` (dry-run first, then `apply=true` with `plan_token`) to move notes or folder subtrees (`customers/northwind/` → `customers/apollo/northwind/`, or retiring `cs244b/` → `archive/cs244b/`), or patch `status` to `deprecated`/`abandoned` via `vault_update_note` (preserving `human-reviewed` tiers).

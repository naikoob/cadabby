# Vault Agent Constitution (AGENTS.md)

This document is the **single canonical constitution** for AI agents working in this vault across every harness; `CLAUDE.md` and `GEMINI.md` only point here. Task procedures live in the runbooks below, and how to call each tool is in its MCP description.

## 1. Read the Runbook for Your Task First

| If you are... | Read before the first write |
| :--- | :--- |
| Ingesting or re-reading a `raw/` source, writing or editing notes, linking, building MOCs, setting up a cognitive domain | `.agents/skills/librarian/SKILL.md` |
| Fixing lint findings, dead links or orphans, paying down verification debt, moving or renaming notes or folders, retiring work to `archive/`, splitting or merging MOCs | `.agents/skills/technician/SKILL.md` |
| Writing note prose, titles, tags, or frontmatter | `STYLE.md` |

## 2. The Vault

* **`raw/`**: user-curated primary sources. Read and cite them; never edit, rename, move, or delete anything in `raw/`.
* **`wiki/`**: flat evergreen concept graph (`wiki/*.md`). Topology is carried editorially by Maps of Content (`type: moc`), never by subfolders.
* **Cognitive domains**: any other non-reserved top-level folder (`customers/`, `projects/`, `courses/`), optionally governed by `{domain}/AGENTS.md` and free to nest arbitrarily (`customers/acme/deals/`). Reserved: `raw/`, `log/`, and dot or build folders (`.cadabby/`, `.obsidian/`, `.git/`, `.agents/`, `.claude/`, `node_modules/`, …).
* **`archive/`**: the conventional retirement domain for completed courses, churned accounts, superseded projects, or retired notes. Notes and subtrees moved there with `vault_move_note` keep their frontmatter, links, and trust tiers intact, rank below active domains (`ranking.domain.archive: 0.5`), yield stem resolution to `wiki/` and active domains, and drop out of `index.md` worklists.
* **`index.md`**: engine-generated gap report (MOC entry points, unfiled notes, unprocessed `raw/` sources, verification debt); never hand-edit it. **`log.md`**: append-only ledger.

## 3. Trust Rules

Every note has one of four trust tiers: **`human-reviewed`** (`human:*` attestation matches current body), **`machine-confirmed`** (`agent:*` or `process:*` matches), **`stale-verified`** (attestations exist but the body changed since — verification debt), and **`unverified`** (none). Attestations bind to `of: sha256:<body_hash>`, so **any body edit turns a verified note `stale-verified`**.

* **Closed-World Source Grounding**: In a note citing `sources: ["raw/..."]`, every factual claim, number, quote, API name, date, and causal statement must trace to those files or explicitly linked vault notes. Never pad from background knowledge; omit unsupported claims or mark them as open questions.
* **Never forge human verification**: The MCP server refuses `human:*`. When asked to sign or endorse a note for a user, don't retry with a reworded actor — explain that a human attestation records *their* check of this exact wording, and give them `cadabby verify <cid> --human` to run in their own terminal. Never run `cadabby verify --human` yourself, or a flagless `cadabby verify`, including via `script`, `expect`, or pty wrappers.
* **No rubber-stamp self-verification**: Never call `vault_verify_note` without first auditing the note claim by claim against its primary `raw/` sources — never in the turn you drafted it from conversation or memory.
* **No transitive trust laundering**: Never verify a note synthesized from `unverified` or `stale-verified` notes; verify only against primary `raw/` sources or `human-reviewed` notes.
* **Proposals are not source facts**: Designs, plans, sizings, recommendations, and hypotheses assert what no source states; leave them `unverified` for a human to sign.
* **`require_sources: false` relaxes lint, not attestation**: A domain manifest dropping the `sources:` requirement exempts its notes from lint gate 4, not from the evidence bar. A note with no primary source stays `unverified` whatever its domain permits; only a human can sign it.
* **Protect `human-reviewed` notes before body edits**: Check `trust_tier` first, and never edit the body of a `human-reviewed` note — even to add a link or fix a lint warning — without explicit user confirmation.
* **Never agent-verify over a human's stale attestation**: If a stale note's latest attestation is `human:*`, don't verify it; your entry would zero its debt and hide the owed human re-review. Report the drift and give the user `cadabby verify <cid> --human`.
* **Attestation history is append-only**: Never prune or rewrite `verified:` entries, and never clear `verified:` to make a note read `unverified`.

## 4. Working Rules

* **Epistemic attribution in Q&A**: State the `trust_tier` of cited notes, flag `unverified` and `stale-verified` ones, and state plainly where the vault has no coverage instead of filling gaps from memory.
* **Answering past the vault's coverage**: When you answer from your own knowledge because the vault is silent, mark that boundary in the answer — the same line an Unverified Supplement callout draws in a note body. Never present recalled specifics (prices, stock, versions, availability, contact details) as checked fact.
* **No notes from memory**: Never write a note from your own knowledge rather than `raw/`, verified notes, or user input. If the user explicitly requests one, fence it in an Unverified Supplement callout per the librarian runbook.
* **User-owned files need consent**: `.cadabby.json`, `AGENTS.md`, `STYLE.md`, `CLAUDE.md`, `GEMINI.md`, and `{domain}/AGENTS.md` are user governance. Show the proposed change and write it only after the user agrees.
* **Note bodies change only through `vault_update_note`**: It is the only way a note body changes. Never write note files directly unless explicitly instructed.
* **Notes move only through `vault_move_note`**: Show the user its dry-run plan and apply only once they agree, passing back the plan's `plan_token`.
* **When the MCP server is unavailable**: If the `vault_*` tools are missing or failing, tell the user first, never infer trust tiers from frontmatter or `log.md`, and make no vault writes. `cadabby status --json` and `cadabby lint --json` are read-only shell fallbacks.

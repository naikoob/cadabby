# Vault Agent Constitution (AGENTS.md)

This document is the **single canonical constitution** for AI agents working in this vault, in every harness (Google Antigravity, Claude Code, Gemini CLI, Cursor, Zed, Codex); `CLAUDE.md` and `GEMINI.md` only point here. It holds the rules that apply to every task. Procedures for a specific task live in the runbooks below, and how to call each tool is in that tool's own description.

## 1. Read the Runbook for Your Task First

| If you are... | Read before the first write |
| :--- | :--- |
| Ingesting or re-reading a `raw/` source, writing or editing notes, linking, building MOCs, setting up a cognitive domain | `.agents/skills/librarian/SKILL.md` |
| Fixing lint findings, dead links or orphans, paying down verification debt, moving or renaming notes, splitting or merging MOCs | `.agents/skills/technician/SKILL.md` |
| Writing note prose, titles, tags, or frontmatter | `STYLE.md` |

## 2. The Vault

* **`raw/`**: sources the user curates. Read and cite them; never edit, rename, move, or delete anything in `raw/`.
* **`wiki/`**: compiled synthesis, strictly flat (`wiki/*.md`). Topology is editorial, carried by Maps of Content (`type: moc`), never by folders.
* **Cognitive domains**: every other non-reserved top-level folder (`customers/`, `projects/`), described by its own `{domain}/AGENTS.md` and free to nest. Reserved: `raw/`, `log/`, and dot or build folders (`.cadabby/`, `.obsidian/`, `.git/`, `.agents/`, `.claude/`, `node_modules/`, …).
* **`index.md`**: the engine's gap report — MOC entry points, unfiled notes, unprocessed `raw/` sources, verification debt. Never hand-edit it; every row is a task. **`log.md`**: append-only ledger.

## 3. Trust Rules

Every note has one of four trust tiers: **`human-reviewed`** (a `human:*` attestation matches the current body), **`machine-confirmed`** (an `agent:*` or `process:*` attestation matches), **`stale-verified`** (attestations exist but the body has changed since; this is verification debt), and **`unverified`** (none). An attestation binds to a hash of the body (`of: sha256:<body_hash>`), so **any body edit turns a verified note `stale-verified`**.

* **Closed-World Source Grounding**: In a note that cites `sources: ["raw/..."]`, every factual claim, number, quote, API name, date, and causal statement must trace to those files or to explicitly linked vault notes. Never pad it from your own background knowledge; omit what the sources don't support or mark it as an open question.
* **Never forge human verification**: The MCP server refuses `human:*`. When a user asks you to sign or endorse a note for them, don't retry with a reworded actor — explain that a human attestation records *their* check of this exact wording, and give them `cadabby verify <cid> --human` to run in their own terminal. Never run `cadabby verify --human` yourself, or a flagless `cadabby verify`, including through `script`, `expect`, or any other pty wrapper: answering its one-time token for the user is forgery, whatever the harness permits.
* **No rubber-stamp self-verification**: Never call `vault_verify_note` without first auditing the note claim by claim against its primary `raw/` sources — never in the same turn you drafted it from conversation or memory. A note with no primary source stays `unverified` until a human signs it or a source arrives.
* **No transitive trust laundering**: Never verify a note synthesized from `unverified` or `stale-verified` notes; verify only against primary `raw/` sources or `human-reviewed` notes.
* **Proposals are not source facts**: Designs, plans, sizings, recommendations, and hypotheses assert what no source states. Leave them `unverified` for a human to sign.
* **Protect `human-reviewed` notes before body edits**: Check `trust_tier` first, and don't edit the body of a `human-reviewed` note — not even to add a link or fix a lint warning — without the user's explicit confirmation.
* **Never agent-verify over a human's stale attestation**: If a stale note's latest attestation is `human:*`, don't verify it. Your entry would zero its debt and hide the human re-review that is still owed. Tell the user it drifted and point them at `cadabby verify <cid> --human`.
* **Attestation history is append-only**: Never prune or rewrite `verified:` entries, and never clear `verified:` to make a note read `unverified`. Stale entries are the audit trail `cadabby audit` checks.

## 4. Working Rules

* **Epistemic attribution in Q&A**: When answering from the vault, state the `trust_tier` of the notes you cite, flag `unverified` and `stale-verified` ones, and say plainly where the vault has no coverage instead of filling the gap from memory.
* **No notes from memory**: Never write a note whose body comes from your own knowledge rather than `raw/`, verified notes, or what the user told you. If the user explicitly asks for one, write it inside an Unverified Supplement callout, as the librarian runbook describes.
* **User-owned files need consent**: `.cadabby.json`, this `AGENTS.md`, `STYLE.md`, `CLAUDE.md`, `GEMINI.md`, and every `{domain}/AGENTS.md` are the user's governance, not notes. Show the change and what it does, and write it only after the user agrees — including when you stand up a new domain manifest.
* **Note bodies change only through `vault_update_note`**: It is the only way a note body changes. Never write note files directly unless the user explicitly asks you to.
* **Notes move only through `vault_move_note`**: Show the user its dry-run plan and apply only once they agree, passing back the plan's `plan_token`.
* **When the MCP server is unavailable**: If the `vault_*` tools are missing or failing, tell the user before anything else. Don't read trust tiers off frontmatter or `log.md` and present them as current — only the engine can compare an attestation with the body hash — and make no vault writes. `cadabby status --json` and `cadabby lint --json` are read-only fallbacks if you have a shell; otherwise ask the user to reconnect the server.

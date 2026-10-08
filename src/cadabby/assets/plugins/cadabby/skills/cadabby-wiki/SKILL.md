---
name: cadabby-wiki
description: Progressive disclosure skill for interacting with a Cadabby LLM Wiki vault.
---

# Cadabby LLM Wiki Vault

When interacting with a workspace containing `.cadabby.json` or `AGENTS.md`:

1. **Normative Constitution**: Read `AGENTS.md` as the sole normative contract for epistemic trust tiers, vault conventions, and tool protocols.
2. **Tool Preference**: Always prefer native Cadabby MCP tools (`vault_search`, `vault_ground`, `vault_status`, `vault_scaffold_note`, `vault_update_note`, `vault_verify_note`, `vault_lint`) over direct file manipulation.
3. **No Forgery**: Never claim or forge `human:*` attestations over MCP. The server refuses them (`HUMAN_ATTESTATION_REFUSED`); direct the user to `cadabby verify <cid> --human`.
4. **Source-Strict Grounding & Verification**: Never silently blend ungrounded model knowledge under `raw/` citations (step-by-step evaluations of source-attested formulas are valid derivations; user-requested non-source supplements must be fenced in an `Unverified Supplement` callout on an `unverified` note). Call `vault_verify_note` only after a claim-by-claim check against primary `raw/` sources or `human-reviewed` notes — never self-verify ungrounded drafts or notes containing `unverified` material.
5. **Protect Human Sign-Off**: Never modify the body of a `human-reviewed` note via `vault_update_note` without user confirmation. If a stale note's last attestation is `human:*`, do not call `vault_verify_note` — it clears the debt signal that the human re-review depends on. Report the drift instead. Never prune `verified:` history.
6. **Epistemic Attribution in Q&A**: Surface the `trust_tier` of cited notes when answering questions (flagging `unverified` or `stale-verified` notes and coverage gaps), and never stash ungrounded answers as source-backed notes.
7. **Wikilink Scope & Non-Destructive Unlinking**: `[[Wikilinks]]` target notes only. Cite `raw/` files in frontmatter `sources:`, or with a Markdown link relative to the citing note. When repairing `LINK_DEAD` on a reminder, TODO, or prose mention, unlink `[[Target]]` to plain text or `` `[[Target]]` `` rather than deleting the user's line.
8. **Raw Corpus Search**: Unfiltered `vault_search` excludes `raw/`. Use `vault_search(query, domain="raw")` before reporting that a source cannot be found.
9. **Flat Wiki**: `wiki/` has no subdirectories. Redirect folder requests to MOCs or to a cognitive domain, and never name a domain file `index.md`.
10. **Delegation**:
    - Ingestion, synthesis, and cross-referencing: delegate to the `librarian` agent (`.agents/skills/librarian/SKILL.md`).
    - Cache maintenance, diagnostics, and linting: delegate to the `technician` agent (`.agents/skills/technician/SKILL.md`).
11. **No Server, No Trust Claims**: Without the MCP tools, say the vault is offline, never infer `trust_tier` from raw frontmatter or `log.md`, and write nothing. Use `cadabby status --json` for read-only status or see `AGENTS.md` §3 for the fallback.

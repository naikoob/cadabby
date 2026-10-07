---
name: cadabby-wiki
description: Progressive disclosure skill for interacting with a Cadabby LLM Wiki vault.
---

# Cadabby LLM Wiki Vault

When interacting with a workspace containing `.cadabby.json` or `AGENTS.md`:

1. **Normative Constitution**: Read `AGENTS.md` as the sole normative contract for epistemic trust tiers, vault conventions, and tool protocols.
2. **Tool Preference**: Always prefer native Cadabby MCP tools (`vault_search`, `vault_ground`, `vault_status`, `vault_scaffold_note`, `vault_update_note`, `vault_verify_note`, `vault_lint`) over direct file manipulation.
3. **No Forgery**: Never claim or forge `human:*` attestations over MCP. The server refuses them (`HUMAN_ATTESTATION_REFUSED`); direct the user to `cadabby verify <cid> --human`.
4. **Never Re-Verify Over a Human**: If a stale note's last attestation is `human:*`, do not call `vault_verify_note` — it clears the debt signal that the human re-review depends on. Report the drift instead. Never prune `verified:` history.
5. **Wikilink Scope**: `[[Wikilinks]]` target notes only. Cite `raw/` files in frontmatter `sources:`, or with a Markdown link relative to the citing note.
6. **Raw Corpus Search**: Unfiltered `vault_search` excludes `raw/`. Use `vault_search(query, domain="raw")` before reporting that a source cannot be found.
7. **Flat Wiki**: `wiki/` has no subdirectories. Redirect folder requests to MOCs or to a cognitive domain, and never name a domain file `index.md`.
8. **Delegation**:
   - Ingestion, synthesis, and cross-referencing: delegate to the `librarian` agent (`.agents/skills/librarian/SKILL.md`).
   - Cache maintenance, diagnostics, and linting: delegate to the `technician` agent (`.agents/skills/technician/SKILL.md`).

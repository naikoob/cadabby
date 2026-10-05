---
name: cadabby-wiki
description: Progressive disclosure skill for interacting with a Cadabby LLM Wiki vault.
---

# Cadabby LLM Wiki Vault

When interacting with a workspace containing `.cadabby.json` or `AGENTS.md`:

1. **Normative Constitution**: Read `AGENTS.md` as the sole normative contract for epistemic trust tiers, vault conventions, and tool protocols.
2. **Tool Preference**: Always prefer native Cadabby MCP tools (`vault_search`, `vault_ground`, `vault_status`, `vault_scaffold_note`, `vault_update_note`, `vault_verify_note`, `vault_lint`) over direct file manipulation.
3. **No Forgery**: Never claim or forge `human:*` attestations over MCP.
4. **Delegation**:
   - Ingestion, synthesis, and cross-referencing: delegate to the `librarian` agent (`.agents/skills/librarian/SKILL.md`).
   - Cache maintenance, diagnostics, and linting: delegate to the `technician` agent (`.agents/skills/technician/SKILL.md`).

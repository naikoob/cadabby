---
name: cadabby-wiki
description: Progressive disclosure skill for interacting with a Cadabby LLM Wiki vault.
---

# Cadabby LLM Wiki Vault

When working in a folder that contains `.cadabby.json`:

1. **Constitution**: `AGENTS.md` holds the trust rules for every task. Read it first; its routing table says which runbook to load for the task at hand.
2. **Tools**: Use the Cadabby MCP tools (`vault_search`, `vault_ground`, `vault_status`, `vault_scaffold_note`, `vault_update_note`, `vault_move_note`, `vault_verify_note`) rather than editing files directly. Each tool's description says how to call it.
3. **No forgery**: Never attempt a `human:*` attestation; give the user `cadabby verify <cid> --human` instead.
4. **Unverified Supplements**: Content the sources don't support goes in a `> [!WARNING] Unverified Supplement (not attested in raw/)` callout; do not verify a note while such a callout stands, and never clear its existing `verified:` entries.
5. **Delegation**:
   - Ingestion, synthesis, and cross-referencing: the `librarian` agent (`.agents/skills/librarian/SKILL.md`).
   - Diagnostics, linting, debt, and moves: the `technician` agent (`.agents/skills/technician/SKILL.md`).
6. **No Server, No Trust Claims**: Without the MCP tools, say the vault is offline, never infer `trust_tier` from frontmatter or `log.md`, and write nothing. `cadabby status --json` is a read-only fallback.

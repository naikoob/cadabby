---
name: cadabby-wiki
description: Progressive disclosure skill for interacting with a Cadabby LLM Wiki vault.
---

# Cadabby LLM Wiki Vault

When working in a folder containing `.cadabby.json`:

1. **Constitution**: Read `AGENTS.md` first for trust rules and task-to-runbook routing.
2. **Tools**: Use the Cadabby MCP tools (`vault_search`, `vault_ground`, `vault_status`, `vault_graph`, `vault_scaffold_note`, `vault_update_note`, `vault_move_note`, `vault_verify_note`) rather than direct file edits.
3. **No Forgery**: Never attempt a `human:*` attestation; give the user `cadabby verify <cid> --human` instead.
4. **Unverified Supplements**: Fence unsupported content in `> [!WARNING] Unverified Supplement (not attested in raw/)`; do not verify a note while such a callout stands (the engine refuses with `VERIFICATION_REFUSED`: leave it unverified, never retry under another `method`), and never clear existing `verified:` entries.
5. **Personas**: `librarian` (`.agents/skills/librarian/SKILL.md`) handles ingestion, writing, and linking; `technician` (`.agents/skills/technician/SKILL.md`) handles diagnostics, linting, debt, and moves.
6. **No Server, No Trust Claims**: Without the MCP tools, report the vault offline, never infer `trust_tier` from frontmatter or `log.md`, and write nothing (`cadabby status --json` is a read-only fallback).

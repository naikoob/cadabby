# Cadabby Vault Rules

When interacting with a workspace containing a `.cadabby.json` file or Cadabby vault:

1. **Follow `AGENTS.md`**: Respect the Karpathy 3-layer architecture (`raw/` immutable, `wiki/` mutable).
2. **Prioritize Native MCP Tools**: Use `vault_search`, `vault_ground`, `vault_scaffold_note`, `vault_update_note`, `vault_verify_note`, and `vault_lint` instead of direct shell commands.
3. **No Forgery**: Never forge `human:*` verifications. Use `agent:<name>`.
4. **Content-Bound Attestations**: Verification bindings must target the body hash (`notes.body_hash`).

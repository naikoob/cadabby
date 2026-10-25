# AGENTS.md — Working on Cadabby

Cadabby is a zero-dependency, local-first engine (CLI + MCP server) for LLM Wiki vaults with OKF epistemic trust tiers.

[`SPECIFICATION.md`](SPECIFICATION.md) (v0.5.5) is normative: where code and spec disagree, the spec wins, or both change
together with their acceptance tests. Don't read the whole spec. §0 maps each section to the question it answers and
shows how to extract one, and every `§` reference in this repo resolves to a real heading.

This file deliberately restates no spec detail. A copy here would have no test keeping it true, and it would drift.
Follow the `§` pointers instead.

## Hard Rules

Each rule is cheap to break and hard to see once broken.

- **No runtime dependencies.** `dependencies = []` in `pyproject.toml`, standard library only, Python ≥ 3.11 with full type annotations.
- **Atomic writes only.** Never `open(..., "w")` a note or config; use `atomic_write()` / `atomic_replace_checked()` from `fsutil.py` (§8).
- **`notes` is written only through `cache.upsert_note()` / `cache.delete_note()`.** They retract the old FTS terms from the right table first; raw SQL against `notes` leaves search silently stale (§4.2).
- **The MCP tool count is fixed by §5.1.** New capability goes into an existing tool's parameters or an MCP resource; adding a tool is a spec change first, never a code-only change.
- **Persona behavior lives only in `src/cadabby/assets/skills/<persona>/SKILL.md`** (installed as `.agents/skills/` in a vault). Agent manifests, slash commands, and plugin files are thin pointers (§7.1).
- **Anything shipped into a vault lives under `src/cadabby/assets/`.** Outside it, the file is missing from the wheel (§9).
- **Use cases go through ports.** `ops.py` depends on `ports.py` protocols; the storage, layout, and ledger ports each have a disk adapter and an in-memory twin for hermetic tests, while the cache port deliberately has none (§9.1). `isinstance` against these protocols proves only that the member *names* exist — parameters are checked by `test_every_adapter_takes_the_parameters_its_port_declares`, and return types by `mypy`.
- **`paths.py` and `markdown.py` are leaves.** Pure string work, no filesystem, no `cadabby` imports beyond `constants`/`errors`. `graph`, `ground`, `move`, `resolve` and `search` reach the path algebra there and must not import `cadabby.vault` (§9.1).
- **No human attestation without a human.** MCP refuses `human:*` outright, and CLI writes default to `process:cli` even on a TTY (§3.5, §5.3).
- **No engine-generated files inside cognitive domains**, including no `{domain}/index.md` (§2.2, §2.5).
- **Templates seed the body only, and `templates/` is never reserved by default.** The reasons are in §7.5; read it before touching either.
- **Review skills, runbooks, and docs against the code, not in isolation.** When asked to review a skill (`SKILL.md`), constitution (`AGENTS.md`), or spec section, trace every tool call, parameter default, error code, and state transition it mentions into the implementation and tests. Prioritize finding code/doc divergence, unstated edge cases (e.g., empty fields, default arguments that bypass a guard), and untested branches over praising prose structure.

## Where to Start

| If you are changing... | Start at | Then check |
| :--- | :--- | :--- |
| Note schema, trust tiers, body hashing | `okf.py`, `frontmatter.py` | §3, `test_okf.py`, `test_frontmatter.py` |
| Search, ranking, the FTS5 tables, scanning | `cache.py`, `search.py` | §4, `test_cache.py` |
| Queues, graph views, status counts, lint row queries | `queries.py` | §2.5, §4.5, `test_cache.py`, `test_indexer.py` |
| Wikilink / Markdown-link parsing, resolution, backlinks | `graph.py` | §2.5, `test_graph.py` |
| Path/CID mapping, or which paths may hold a note or a source | `paths.py` | §2.1, §2.2, `test_vault.py`, `test_architecture.py` |
| Vault discovery, `.cadabby.json` config, domain manifests | `vault.py` | §2.2, §2.7, `test_vault.py`, `test_domains.py` |
| `index.md` or `log.md` output | `indexer.py` | §2.5, `test_indexer.py` |
| A lint gate's codes or severities | `lint.py` | §6.3, `test_lint.py` |
| Scaffold / update / move / verify / ground, or the search / status / graph facades | `ops.py`, `move.py`, `ground.py`, `resolve.py` (+ `ports.py`, `adapters/`) | §9.1, `test_ops.py`, `test_move.py`, `test_domain_model.py` |
| The MCP tool surface or resources | `mcp.py` | §5, §7, `test_mcp.py` |
| CLI flags, output, exit codes | `cli.py`, `errors.py` | §5.4, `test_cli.py`, `test_errors.py` |
| `init` / `install` and file ownership | `installer.py` | §7.6, `test_installer.py` |
| A module's imports, layering, or a function's size | that module | §9, §9.1, `test_architecture.py` |
| Anything shipped into a user's vault | `src/cadabby/assets/` | §7.1, §7.5, `test_templates.py` |

The full file tree is §9, and `TestSpecRepositoryTree` keeps it true.

## Commands

Run from the repository root. All tests must pass before a task is done.

```bash
python3 tests/run_parallel.py                               # full suite, parallel
python3 tests/run_parallel.py test_cache                    # filter by module
PYTHONPATH=src python3 -m unittest tests.test_acceptance.TestAcceptanceTraceability  # one class or method

uvx ruff check src/                                         # must be clean; CI runs it
uvx mypy                                                    # strict, configured in pyproject.toml

PYTHONPATH=src python3 -m cadabby status --vault examples/demo-vault   # CLI without installing
uv build                                                    # verify the wheel builds
```

Run `ruff` before you call a task done. The suite cannot see a name that is
never imported on a branch no test takes — that is how a `NameError` reached
`update_note` in 0.5.4 — and `ruff` reports it as `F821` in under a second.
Both tools run through `uvx`, so neither is a declared dependency.

## Traceability and Releases

§10's criteria are falsifiable observations, and each traces to a test. If you change specified behavior, update the
criterion and its row in the traceability table. `TestAcceptanceTraceability` fails on unmapped criteria, missing
tests, or dangling `§` references.

The version lives in several files, and `test_every_version_string_in_the_repo_agrees` fails on drift. Bump
procedure, readiness checks, and tagging are in [`.agents/skills/release/SKILL.md`](.agents/skills/release/SKILL.md).
Pushing a `v*` tag publishes to PyPI.

# AGENTS.md — Working on Cadabby

Cadabby is a zero-dependency, local-first engine (CLI + MCP server) for LLM Wiki vaults with OKF epistemic trust tiers.

[`SPECIFICATION.md`](SPECIFICATION.md) (v0.5.0) is normative: where code and spec disagree, the spec wins, or both change
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
- **Use cases go through ports.** `ops.py` depends on `ports.py` protocols; the storage, layout, and ledger ports each have a disk adapter and an in-memory twin for hermetic tests, while the cache port deliberately has none (§9.1).
- **No human attestation without a human.** MCP refuses `human:*` outright, and CLI writes default to `process:cli` even on a TTY (§3.5, §5.3).
- **No engine-generated files inside cognitive domains**, including no `{domain}/index.md` (§2.2, §2.5).
- **Templates seed the body only, and `templates/` is never reserved by default.** The reasons are in §7.5; read it before touching either.

## Where to Start

| If you are changing... | Start at | Then check |
| :--- | :--- | :--- |
| Note schema, trust tiers, body hashing | `okf.py`, `frontmatter.py` | §3, `test_okf.py`, `test_frontmatter.py` |
| Search, ranking, the FTS5 tables, scanning | `cache.py`, `search.py` | §4, `test_cache.py` |
| Wikilink / Markdown-link parsing, resolution, backlinks | `graph.py` | §2.5, `test_graph.py` |
| `index.md` or `log.md` output | `indexer.py` | §2.5, `test_indexer.py` |
| A lint gate's codes or severities | `lint.py` | §6.3, `test_lint.py` |
| Scaffold / update / move / verify / ground | `ops.py`, `move.py`, `ground.py`, `resolve.py` (+ `ports.py`, `adapters/`) | §9.1, `test_ops.py`, `test_move.py`, `test_domain_model.py` |
| The MCP tool surface or resources | `mcp.py` | §5, §7, `test_mcp.py` |
| CLI flags, output, exit codes | `cli.py`, `errors.py` | §5.4, `test_cli.py`, `test_errors.py` |
| `init` / `install` and file ownership | `installer.py` | §7.6, `test_installer.py` |
| Anything shipped into a user's vault | `src/cadabby/assets/` | §7.1, §7.5, `test_templates.py` |

The full file tree is §9, and `TestSpecRepositoryTree` keeps it true.

## Commands

Run from the repository root. All tests must pass before a task is done.

```bash
python3 tests/run_parallel.py                               # full suite, parallel
python3 tests/run_parallel.py test_cache                    # filter by module
PYTHONPATH=src python3 -m unittest tests.test_acceptance.TestAcceptanceTraceability  # one class or method

PYTHONPATH=src python3 -m cadabby status --vault examples/demo-vault   # CLI without installing
uv build                                                    # verify the wheel builds
```

## Traceability and Releases

§10's criteria are falsifiable observations, and each traces to a test. If you change specified behavior, update the
criterion and its row in the traceability table. `TestAcceptanceTraceability` fails on unmapped criteria, missing
tests, or dangling `§` references.

The version lives in several files, and `test_every_version_string_in_the_repo_agrees` fails on drift. Bump
procedure, readiness checks, and tagging are in [`.agents/skills/release/SKILL.md`](.agents/skills/release/SKILL.md).
Pushing a `v*` tag publishes to PyPI.

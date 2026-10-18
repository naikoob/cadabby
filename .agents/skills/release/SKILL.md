---
name: release
description: >-
  Cut and publish a Cadabby release to PyPI. Covers the version bump across every
  version marker, pre-release readiness checks, release notes in the annotated tag,
  pushing main, gating on CI, tagging, and verifying the published package. Use
  when the user asks to release, publish, tag, or bump the version of Cadabby.
  Don't use for ordinary commits or for releasing anything other than this repo.
---

# Releasing Cadabby

A release is one annotated tag `vX.Y.Z` on a commit of `main`. Pushing the tag runs
[`publish.yml`](../../../.github/workflows/publish.yml): it calls
[`tests.yml`](../../../.github/workflows/tests.yml) (Python 3.11 / 3.12 / 3.13) and
uploads to PyPI with trusted publishing only if that passes. The notes live in the tag
message. There is no CHANGELOG file and no release branch.

> [!IMPORTANT]
> Pushing `main`, pushing a tag, and publishing are visible to everyone, and PyPI
> versions can never be re-uploaded. Get the user's explicit go-ahead before step 6
> and again before step 7, unless they have already asked for the push and tag.

## 1. Preconditions

```bash
git switch main && git fetch origin
git status --short                      # must be empty
git rev-list --count HEAD..origin/main  # must be 0 (else pull first)
git describe --tags --abbrev=0          # previous release, e.g. v0.3.2
```

Check that the new version is free on both the tag list and PyPI. A `404` from PyPI
means it is free. Read the `/json` endpoint: the `/simple/` index can be cached and stale.

```bash
git tag -l vX.Y.Z
curl -s -o /dev/null -w "%{http_code}\n" https://pypi.org/pypi/cadabby/X.Y.Z/json
```

**Choosing the number:** while the package is on 0.x, a breaking change to the MCP
tool surface, the CLI, the frontmatter or the cache bumps the minor version
(0.3 → 0.4). Fixes only bump the patch version.

## 2. Bump the version

The version appears in the following places. The test
`test_every_version_string_in_the_repo_agrees` checks every one except `uv.lock`:

| File | Marker |
| :--- | :--- |
| `src/cadabby/__init__.py` | `__version__ = "X.Y.Z"` |
| `pyproject.toml` | `version = "X.Y.Z"` (static, not flit-dynamic) |
| `src/cadabby/assets/plugins/cadabby/plugin.json` | `"version": "X.Y.Z"` |
| `SPECIFICATION.md` | `**Version:** X.Y.Z`, §7.2 `Verified Architecture (vX.Y.Z)`, §10 `### Criteria for X.Y.Z` |
| `AGENTS.md` | `` `SPECIFICATION.md` `` … `(vX.Y.Z)` |
| `uv.lock` | the `name = "cadabby"` package entry's `version` (run `uv lock`, or edit it) |

Find any stragglers with `git grep -n -F "<old version>"`. Review each hit before
changing it, because historical mentions (e.g. "added in 0.3.2") must stay.

## 3. Readiness checks

Run all of these. Stop on any failure.

1. **Tests, both runners.** Run them from the repo root:
   ```bash
   python3 tests/run_parallel.py
   PYTHONPATH=src python3 -m unittest discover tests   # require "Ran N tests ... OK"
   ```
   The suite also enforces spec §10 traceability, the §9 repository tree, version
   parity, and that no machine-specific paths appear in published files.
2. **Build and inspect the wheel.** Clear out stale artifacts first:
   ```bash
   rm -rf dist && uv build
   python3 -m zipfile -l dist/cadabby-X.Y.Z-py3-none-any.whl | grep -c assets/
   ```
   Compare the file list with the previous wheel (`pip download cadabby==<prev>
   --no-deps -d /tmp/prev`). Anything added or removed should be intentional, and
   everything under `src/cadabby/assets/` must be inside the wheel.
3. **Smoke-test the built wheel in a clean venv:**
   ```bash
   python3 -m venv /tmp/rc && /tmp/rc/bin/pip install -q dist/cadabby-X.Y.Z-py3-none-any.whl
   /tmp/rc/bin/cadabby init /tmp/rc-vault && /tmp/rc/bin/cadabby status --vault /tmp/rc-vault
   printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
                 '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
     | /tmp/rc/bin/cadabby mcp --vault /tmp/rc-vault
   ```
   Expect `serverInfo.version` to be X.Y.Z and exactly 8 tools.
4. **Upgrade path.** Do this only when the tool surface, the shipped assets, or the
   cache schema changed:
   - Create a vault with the previous release: `uvx --from cadabby==<prev> cadabby init /tmp/old-vault`.
   - Run the new build's `cadabby install --all --vault /tmp/old-vault`, then `status`.
   - `install` refreshes only engine-owned files. User-owned files (`AGENTS.md`,
     `CLAUDE.md`, `STYLE.md`, `.cadabby.json`) keep the old text. If they mention a
     renamed or removed tool, the README needs an **Upgrading from X.Y** section, and
     a removed MCP tool needs an entry in `mcp._RETIRED_TOOLS`. That entry is only an
     error message, not an alias, so the surface stays at 8 tools. A removed or renamed
     argument gets an entry in `mcp._RETIRED_ARGUMENTS` on the same terms.
5. **Docs.** Check the README test counts, and that every new CLI flag, MCP parameter
   and error code is described in the spec.

## 4. Release notes

Draft the notes from `git log <prev-tag>..HEAD` and the diff. Write them to a file
outside the repo, e.g. `/tmp/tag-vX.Y.Z.txt`. Leave out any empty section:

```text
Cadabby X.Y.Z

Breaking
- <what changed, and what the caller must now do>

New
- <feature>: <one line on what it does>

Fixes
- <user-visible symptom that no longer happens>

Upgrading from <prev minor>
- Run `cadabby install --all` in each vault to refresh skills, commands and the plugin
- <manual edits to user-owned files, if any>

Zero runtime dependencies; <N> tests across <M> targets.
```

## 5. Commit

```bash
git add -A && git status --short   # review: no dist/, no scratch files
git commit -m "feat: release X.Y.Z with <two or three headline changes>" -m "<bullets, may reuse the notes>"
```

## 6. Push `main` and wait for CI

```bash
git push origin main
python3 .agents/skills/release/scripts/gh_run_status.py --sha "$(git rev-parse HEAD)" --workflow Tests
```

The script polls the public GitHub Actions API and exits 0 only on `success`. You
must pass the **full** SHA, because the API returns no runs for a short one. With the
GitHub CLI, `gh run watch` does the same job. **Do not tag if CI fails.** Fix forward
with a new commit and repeat this step. This run is the only one that exercises the
Python versions you don't have locally.

## 7. Tag and publish

```bash
SHA=$(git rev-parse HEAD)
git tag -a vX.Y.Z -F /tmp/tag-vX.Y.Z.txt "$SHA"
git push origin vX.Y.Z
python3 .agents/skills/release/scripts/gh_run_status.py --sha "$SHA" --workflow "Publish to PyPI"
```

## 8. Verify

```bash
curl -s https://pypi.org/pypi/cadabby/X.Y.Z/json | python3 -c "import json,sys; print(json.load(sys.stdin)['info']['version'])"
uvx --from cadabby==X.Y.Z cadabby --help
```

Optional: run `gh release create vX.Y.Z --notes-from-tag` to add a GitHub Release page.
Pushing the tag alone doesn't create one.

## Recovery

| Situation | Action |
| :--- | :--- |
| `Tests` fails on `main` (no tag yet) | Fix forward with a new commit; rerun step 6. |
| `Publish` fails and PyPI has **no** `X.Y.Z` | Fix on `main`, then move the tag: `git tag -d vX.Y.Z && git push origin :refs/tags/vX.Y.Z`, then redo step 7 on the new commit. |
| `Publish` fails after PyPI accepted `X.Y.Z`, or the release is bad | PyPI never accepts the same version twice. Release `X.Y.(Z+1)`; if needed, yank the bad version on PyPI. Never move a tag that was published. |
| Need to patch an old line after `main` moved on | `git switch -c release/X.Y vX.Y.Z`, then fix, bump the patch version, and tag from that branch. |

## Don'ts

- Don't `uv publish` from a laptop. Publishing goes through CI with trusted publishing
  (environment `pypi`).
- Don't create release branches ahead of time; branch from the tag only when you need a patch.
- Don't add a CHANGELOG file. The annotated tag is the record.
- Don't skip step 6 because the publish workflow also runs the tests. A failure at that
  point leaves a tag that has to be deleted.

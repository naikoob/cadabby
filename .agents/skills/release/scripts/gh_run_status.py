#!/usr/bin/env python3
"""Wait for a GitHub Actions workflow run on a commit and report its conclusion.

Stdlib only. Polls the public Actions API, so it needs network access but no token
for a public repository (set GITHUB_TOKEN to raise the rate limit).

Exit codes: 0 success, 1 the run concluded with anything else, 2 timed out or no run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def _origin_repo() -> str:
    url = subprocess.run(
        ["git", "remote", "get-url", "origin"], capture_output=True, text=True, check=True
    ).stdout.strip()
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    if match is None:
        raise SystemExit(f"origin is not a GitHub remote: {url}")
    return match.group(1)


def _runs(repo: str, sha: str) -> list[dict]:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/runs?head_sha={sha}&per_page=50",
        headers={"Accept": "application/vnd.github+json"},
    )
    if token := os.environ.get("GITHUB_TOKEN"):
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response).get("workflow_runs", [])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sha", required=True, help="full 40-character commit SHA")
    parser.add_argument("--workflow", required=True, help='workflow name, e.g. "Tests"')
    parser.add_argument("--repo", help="owner/name (default: parsed from origin)")
    parser.add_argument("--timeout", type=int, default=1200, help="seconds to wait (default 1200)")
    parser.add_argument("--interval", type=int, default=20, help="seconds between polls (default 20)")
    args = parser.parse_args()

    if not _FULL_SHA.match(args.sha):
        parser.error("--sha must be the full 40-character SHA; the API returns no runs for a short one")
    repo = args.repo or _origin_repo()

    deadline = time.monotonic() + args.timeout
    while True:
        matching = [r for r in _runs(repo, args.sha) if r.get("name") == args.workflow]
        if matching:
            run = max(matching, key=lambda r: r.get("run_attempt", 1))
            print(f"{args.workflow}: {run['status']} {run.get('conclusion') or ''} {run['html_url']}".rstrip())
            if run["status"] == "completed":
                return 0 if run.get("conclusion") == "success" else 1
        else:
            print(f"{args.workflow}: no run yet for {args.sha[:12]}")
        if time.monotonic() >= deadline:
            print("timed out", file=sys.stderr)
            return 2
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())

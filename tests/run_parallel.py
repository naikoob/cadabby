"""Zero-dependency parallel test runner for Cadabby test suite.

Dispatches TestCase classes across parallel worker processes using
Python's standard library `concurrent.futures.ProcessPoolExecutor`.
Provides isolated process execution, real-time progress, and clean
error reporting.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import importlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple


class TargetResult(NamedTuple):
    target: str
    returncode: int
    duration: float
    test_count: int
    stdout: str
    stderr: str


def discover_targets(
    repo_root: Path,
    patterns: list[str] | None = None,
    granularity: str = "class",
) -> list[tuple[str, int]]:
    """Discover test targets (modules or classes) matching patterns."""
    tests_dir = repo_root / "tests"
    test_files = sorted([f.stem for f in tests_dir.glob("test_*.py")])

    if repo_root not in sys.path:
        sys.path.insert(0, str(repo_root))
    src_dir = repo_root / "src"
    if src_dir not in sys.path:
        sys.path.insert(0, str(src_dir))

    targets: list[tuple[str, int]] = []

    for mod_name in test_files:
        if patterns:
            # Check if module itself matches pattern
            mod_matched = any(p.lower() in mod_name.lower() for p in patterns)
        else:
            mod_matched = True

        try:
            mod = importlib.import_module(f"tests.{mod_name}")
        except Exception as exc:
            print(f"Warning: Failed to import tests.{mod_name}: {exc}", file=sys.stderr)
            continue

        if granularity == "module":
            if mod_matched:
                # Count total tests in module
                cnt = 0
                for attr in dir(mod):
                    obj = getattr(mod, attr)
                    if isinstance(obj, type) and hasattr(obj, "__mro__"):
                        if any("TestCase" in b.__name__ for b in obj.__mro__):
                            cnt += len([m for m in dir(obj) if m.startswith("test_")])
                targets.append((f"tests.{mod_name}", cnt))
            continue

        # Granularity == "class"
        for attr in dir(mod):
            obj = getattr(mod, attr)
            if not isinstance(obj, type) or not hasattr(obj, "__mro__"):
                continue

            base_names = [b.__name__ for b in obj.__mro__]
            if not any("TestCase" in b for b in base_names):
                continue
            if obj.__name__ in ("TestCase", "IndexerFixture", "TemplateVaultCase"):
                continue
            if getattr(obj, "__module__", "") != f"tests.{mod_name}":
                continue

            test_methods = [m for m in dir(obj) if m.startswith("test_")]
            if not test_methods:
                continue

            target_name = f"tests.{mod_name}.{obj.__name__}"
            if patterns:
                matched = mod_matched or any(
                    p.lower() in target_name.lower() or p.lower() in obj.__name__.lower()
                    for p in patterns
                )
                if not matched:
                    continue

            targets.append((target_name, len(test_methods)))

    return sorted(targets, key=lambda t: t[0])


def run_single_target(repo_root: Path, target: str, expected_count: int) -> TargetResult:
    """Run a single test target in a dedicated subprocess."""
    t0 = time.perf_counter()
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{repo_root / 'src'}:{repo_root}"

    proc = subprocess.run(
        [sys.executable, "-m", "unittest", target],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
    )
    duration = time.perf_counter() - t0

    # Extract test count from output if possible
    # e.g., "Ran 12 tests in 0.234s"
    m = re.search(r"Ran (\d+) tests?", proc.stderr)
    test_count = int(m.group(1)) if m else expected_count

    return TargetResult(
        target=target,
        returncode=proc.returncode,
        duration=duration,
        test_count=test_count,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run Cadabby tests in parallel with zero runtime dependencies."
    )
    parser.add_argument(
        "patterns",
        nargs="*",
        help="Optional module, class, or pattern filters (e.g. 'cache', 'TestCli', 'fsutil').",
    )
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=os.cpu_count() or 4,
        help="Number of parallel worker processes (default: all available CPU cores).",
    )
    parser.add_argument(
        "-g",
        "--granularity",
        choices=["class", "module"],
        default="class",
        help="Parallel granularity: 'class' (default, ~5s) or 'module' (~16s).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print verbose output for each completing target.",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Only display errors and the final summary.",
    )
    parser.add_argument(
        "-f",
        "--fail-fast",
        action="store_true",
        help="Stop execution upon first target failure.",
    )

    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent
    targets = discover_targets(
        repo_root=repo_root,
        patterns=args.patterns,
        granularity=args.granularity,
    )

    if not targets:
        print("No matching test targets discovered.", file=sys.stderr)
        return 1

    total_targets = len(targets)
    total_planned_tests = sum(cnt for _, cnt in targets)
    workers = max(1, min(args.workers, total_targets))

    if not args.quiet:
        target_kind = "TestCase classes" if args.granularity == "class" else "modules"
        print(
            f"Cadabby Parallel Test Runner\n"
            f"Dispatching {total_targets} {target_kind} (~{total_planned_tests} tests) "
            f"across {workers} worker processes..."
        )
        print("-" * 70)

    start_time = time.perf_counter()
    completed_count = 0
    total_run_tests = 0
    total_cpu_time = 0.0
    failures: list[TargetResult] = []

    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(run_single_target, repo_root, target, count): target
            for target, count in targets
        }

        for future in concurrent.futures.as_completed(future_map):
            res = future.result()
            completed_count += 1
            total_run_tests += res.test_count
            total_cpu_time += res.duration

            if res.returncode != 0:
                failures.append(res)
                if args.verbose and not args.quiet:
                    print(f"[{completed_count:>2}/{total_targets}] FAIL  {res.target} ({res.duration:.2f}s)")
                elif not args.quiet:
                    print("F", end="", flush=True)
                if args.fail_fast:
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
            else:
                if args.verbose and not args.quiet:
                    print(f"[{completed_count:>2}/{total_targets}]  OK   {res.target} ({res.test_count} tests, {res.duration:.2f}s)")
                elif not args.quiet:
                    print(".", end="", flush=True)

    wall_clock = time.perf_counter() - start_time
    if not args.quiet and not args.verbose:
        print()

    # Print summary
    speedup = total_cpu_time / wall_clock if wall_clock > 0 else 1.0
    print("-" * 70)

    if failures:
        print(f"\nFAILURES / ERRORS ({len(failures)} target{'s' if len(failures) > 1 else ''} failed):\n")
        for f in failures:
            print(f"=== {f.target} (Exit code {f.returncode}) ===")
            if f.stderr:
                print(f.stderr.strip())
            if f.stdout:
                print(f.stdout.strip())
            print()
        print("-" * 70)
        print(
            f"FAILED: {len(failures)} failed, {completed_count - len(failures)} passed in "
            f"{wall_clock:.2f}s wall-clock (Cumulative CPU: {total_cpu_time:.2f}s)"
        )
        return 1

    print(
        f"SUCCESS: All {total_run_tests} tests passed across {completed_count} targets!\n"
        f"Wall-clock: {wall_clock:.2f}s | Cumulative CPU: {total_cpu_time:.2f}s | Speedup: {speedup:.1f}x"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

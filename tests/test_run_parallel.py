"""Unit tests for the zero-dependency parallel test runner (tests/run_parallel.py)."""

from __future__ import annotations

import unittest
from pathlib import Path

from tests.run_parallel import discover_targets, main, run_single_target


class TestParallelRunner(unittest.TestCase):
    def setUp(self):
        self.repo_root = Path(__file__).resolve().parent.parent

    def test_discover_targets_class_granularity(self):
        targets = discover_targets(self.repo_root, granularity="class")
        self.assertGreaterEqual(len(targets), 50)
        # Verify format: "tests.<module>.<Class>"
        for target, count in targets:
            self.assertTrue(target.startswith("tests.test_"))
            self.assertGreater(count, 0)

    def test_discover_targets_module_granularity(self):
        targets = discover_targets(self.repo_root, granularity="module")
        self.assertGreaterEqual(len(targets), 18)
        for target, count in targets:
            self.assertTrue(target.startswith("tests.test_"))
            self.assertGreater(count, 0)

    def test_discover_targets_pattern_filter(self):
        from tests.test_audit import TestAudit

        expected_count = unittest.defaultTestLoader.loadTestsFromTestCase(TestAudit).countTestCases()
        targets = discover_targets(self.repo_root, patterns=["test_audit"], granularity="class")
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0][0], "tests.test_audit.TestAudit")
        self.assertEqual(targets[0][1], expected_count)

    def test_run_single_target(self):
        target = "tests.test_frontmatter.TestTagGrammar"
        result = run_single_target(self.repo_root, target, expected_count=4)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.test_count, 4)
        self.assertGreater(result.duration, 0.0)

    def test_run_parallel_main_success(self):
        exit_code = main(["--quiet", "-g", "class", "test_frontmatter"])
        self.assertEqual(exit_code, 0)

    def test_run_parallel_main_unknown_pattern_fails(self):
        exit_code = main(["--quiet", "non_existent_target_filter_xyz123"])
        self.assertEqual(exit_code, 1)

    def _import_failing_for(self, broken: str):
        """Patch the runner's importer so `tests.<broken>` raises like a syntax/import error would."""
        import importlib
        from unittest import mock

        real_import = importlib.import_module

        def fake_import(name: str, *args, **kwargs):
            if name == f"tests.{broken}":
                raise ImportError(f"No module named 'definitely_missing_dependency' (in {name})")
            return real_import(name, *args, **kwargs)

        return mock.patch("tests.run_parallel.importlib.import_module", side_effect=fake_import)

    def test_import_failure_is_recorded_not_skipped(self):
        errors: list[str] = []
        with self._import_failing_for("test_audit"):
            targets = discover_targets(self.repo_root, granularity="class", import_errors=errors)
        self.assertEqual(len(errors), 1)
        self.assertIn("tests.test_audit", errors[0])
        self.assertFalse(any(t.startswith("tests.test_audit.") for t, _ in targets))

    def test_import_failure_fails_the_run_even_when_other_targets_pass(self):
        """A module that cannot be imported must not vanish from the gate while the run reports success."""
        import contextlib
        import io

        stderr = io.StringIO()
        with self._import_failing_for("test_audit"), contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            exit_code = main(["--quiet", "test_audit", "test_frontmatter"])
        self.assertEqual(exit_code, 1)
        self.assertIn("IMPORT ERRORS", stderr.getvalue())
        self.assertIn("tests.test_audit", stderr.getvalue())

    def test_import_failure_outside_the_filter_does_not_fail_the_run(self):
        with self._import_failing_for("test_audit"):
            exit_code = main(["--quiet", "test_frontmatter"])
        self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()


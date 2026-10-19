"""Architecture guards: layering, imports, and complexity (§9, §9.1).

Each test walks src/cadabby with `ast` and pins a structural rule the
spec states but no behavioral test would notice breaking: which modules may
touch SQLite, which may import which, that no cycle hides behind a deferred
import, and that no function grows past the complexity ceiling.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

import tests.helpers  # noqa: F401 -- importing helpers scrubs CADABBY_VAULT (T1)


class TestArchitecture(unittest.TestCase):
    """Structural invariants of src/cadabby, checked statically."""

    def test_sqlite3_and_raw_connections_stay_inside_cache_and_search(self):
        src_root = Path(__file__).resolve().parent.parent / "src" / "cadabby"
        sqlite_importers: set[str] = set()
        get_conn_callers: set[str] = set()
        for path in sorted(src_root.rglob("*.py")):
            rel = path.relative_to(src_root).as_posix()
            tree = ast.parse(path.read_text("utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import) and any(a.name == "sqlite3" for a in node.names):
                    sqlite_importers.add(rel)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get_connection":
                    get_conn_callers.add(rel)
        self.assertEqual(sqlite_importers, {"cache.py", "errors.py", "queries.py", "search.py"})
        self.assertEqual(get_conn_callers, {"cache.py"})

    def test_cache_and_queries_have_no_function_level_imports(self):
        src_root = Path(__file__).resolve().parent.parent / "src" / "cadabby"
        for name in ("cache.py", "queries.py"):
            tree = ast.parse((src_root / name).read_text("utf-8"), filename=name)
            nested_imports = [
                node.lineno
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                for child in ast.walk(node)
                if isinstance(child, (ast.Import, ast.ImportFrom))
            ]
            with self.subTest(module=name):
                self.assertEqual(nested_imports, [])

    def test_no_deferred_internal_imports_hide_a_cycle(self):
        """A function-level `from cadabby...` import is how a cycle hides from import time.

        graph.py once re-exported cache queries through a module `__getattr__`
        that imported cadabby.cache lazily: graph -> cache -> graph, invisible
        until called. Only the CLI may defer, and only for leaf commands whose
        import cost every other subcommand should not pay.
        """
        allowed = {("cli.py", "cadabby.installer"), ("cli.py", "cadabby.mcp")}
        src_root = Path(__file__).resolve().parent.parent / "src" / "cadabby"
        deferred: list[str] = []
        for path in sorted(src_root.rglob("*.py")):
            rel = path.relative_to(src_root).as_posix()
            tree = ast.parse(path.read_text("utf-8"), filename=str(path))
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(fn):
                    if isinstance(node, ast.ImportFrom):
                        modules = [node.module or ""]
                    elif isinstance(node, ast.Import):
                        modules = [a.name for a in node.names]
                    else:
                        continue
                    for module in modules:
                        if module.startswith("cadabby") and (rel, module) not in allowed:
                            deferred.append(f"{rel}:{node.lineno}:{module}")
        self.assertEqual(deferred, [])

    def test_markdown_is_a_leaf_module(self):
        """markdown.py is pure text scanning: graph, ground, lint and Note all consume it.

        graph.py used to import its fence and code-span scanners from domain.py,
        tying link extraction to the Note aggregate. A cadabby import here would
        put that coupling back, or worse, a cycle through any of its consumers.
        """
        path = Path(__file__).resolve().parent.parent / "src" / "cadabby" / "markdown.py"
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        internal = sorted(
            {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("cadabby")}
            | {a.name for node in ast.walk(tree) if isinstance(node, ast.Import) for a in node.names if a.name.startswith("cadabby")}
        )
        self.assertEqual(internal, [])

    def test_queries_never_import_the_cache_or_vault(self):
        """queries.py is the read side: it takes a connection and knows no config.

        Importing VaultCache would let it open its own connection (and make the
        two modules circular); importing Vault would let config-dependent logic
        drift in, which belongs in the VaultCache facade (§4.5, §9).
        """
        path = Path(__file__).resolve().parent.parent / "src" / "cadabby" / "queries.py"
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
        self.assertFalse(imported & {"cadabby.cache", "cadabby.vault"}, sorted(imported))

    def test_driving_adapters_read_through_the_use_case_facades(self):
        """cli.py and mcp.py read the vault only through ops (§9.1).

        Each adapter handler used to call `cache.scan()` before searching,
        grounding, or graphing, and status was built in lint.py: six copies of
        "scan first" and a use case outside the use-case layer. The facades in
        ops scan for themselves now. Only `cadabby sync` scans directly, since
        that scan, with its own index regeneration, is the whole command (§4.3).
        """
        src_root = Path(__file__).resolve().parent.parent / "src" / "cadabby"
        violations: list[str] = []
        for name in ("cli.py", "mcp.py"):
            tree = ast.parse((src_root / name).read_text("utf-8"), filename=name)
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(fn):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                        attr = node.func.attr
                        if attr == "search" or (attr == "scan" and (name, fn.name) != ("cli.py", "cmd_sync")):
                            violations.append(f"{name}:{node.lineno} {fn.name} calls .{attr}()")
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "cadabby.lint":
                    leaked = sorted({a.name for a in node.names} - {"run_vault_lint"})
                    if leaked:
                        violations.append(f"{name}:{node.lineno} imports {leaked} from cadabby.lint")
        self.assertEqual(violations, [])

    def test_no_function_exceeds_the_complexity_ceiling(self):
        """No function in src/cadabby has cyclomatic complexity above 30.

        `parse_frontmatter` and `install_antigravity` both reached 40, and the
        parser hid a reachable `assert` (an INTERNAL that failed every scan)
        inside one of its forty paths. Both were split; the most complex
        function left is at 29. Count: 1, plus one per branch, loop, handler,
        `with`, conditional expression, comprehension, assert, match case,
        and each extra operand of `and`/`or`.
        """
        ceiling = 30
        branch_nodes = (
            ast.If, ast.For, ast.While, ast.ExceptHandler, ast.With, ast.IfExp,
            ast.comprehension, ast.Assert, ast.match_case,
        )
        src_root = Path(__file__).resolve().parent.parent / "src" / "cadabby"
        over: list[str] = []
        for path in sorted(src_root.rglob("*.py")):
            tree = ast.parse(path.read_text("utf-8"), filename=str(path))
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                score = 1
                for node in ast.walk(fn):
                    if isinstance(node, branch_nodes):
                        score += 1
                    elif isinstance(node, ast.BoolOp):
                        score += len(node.values) - 1
                if score > ceiling:
                    over.append(f"{path.relative_to(src_root)}:{fn.lineno} {fn.name} = {score}")
        self.assertEqual(over, [], f"split these below {ceiling}")


if __name__ == "__main__":
    unittest.main()

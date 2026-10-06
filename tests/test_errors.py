"""The error taxonomy and its two boundaries (§5.4).

Before this existed, every failure crossing MCP became the string
`Error executing {tool}: {e}`, so an agent could not tell "wait and retry"
from "fix your input" from "stop and ask a human", and the CLI answered
every one of them with exit 1. These pin the codes, the retryable flag that
prose cannot carry, and the fact that all three lists of codes agree.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from cadabby.errors import (
    ALL_CODES,
    EXIT_ENVIRONMENT,
    EXIT_TRANSIENT,
    EXIT_UNEXPECTED,
    EXIT_USAGE,
    CadabbyError,
    LockTimeoutError,
    VaultConfigError,
    VaultConflictError,
    classify,
    exit_code_for,
)
from cadabby.frontmatter import FrontmatterParseError, FrontmatterSerializeError
from cadabby.mcp import TOOLS, McpServer
from cadabby.vault import Vault
from tests.helpers import copy_demo_vault


class TestClassification(unittest.TestCase):
    """classify() is the single place a failure acquires a code."""

    def test_cadabby_errors_carry_their_own_code(self):
        for exc, code, retryable in (
            (VaultConflictError("raced"), "VAULT_CONFLICT", True),
            (LockTimeoutError("held"), "LOCK_TIMEOUT", True),
            (VaultConfigError("bad json"), "CONFIG_INVALID", False),
            (FrontmatterParseError("bad", 3), "FRONTMATTER_UNPARSEABLE", False),
        ):
            with self.subTest(code=code):
                info = classify(exc)
                self.assertEqual(info.code, code)
                self.assertEqual(info.retryable, retryable)

    def test_builtins_resolve_to_the_narrowest_code(self):
        """OSError is last in the table because the others subclass it.

        Reordering silently turns every missing file into IO_ERROR, which is
        both wrong and unactionable, and nothing else would notice.
        """
        self.assertEqual(classify(FileNotFoundError("gone")).code, "NOT_FOUND")
        self.assertEqual(classify(FileExistsError("there")).code, "ALREADY_EXISTS")
        self.assertEqual(classify(PermissionError("nope")).code, "PERMISSION_DENIED")
        self.assertEqual(classify(OSError("disk")).code, "IO_ERROR")
        self.assertEqual(classify(ValueError("huh")).code, "INVALID_ARGUMENT")

    def test_classification_is_total(self):
        """Anything unrecognized is INTERNAL rather than an escaping exception."""

        class Surprise(Exception):
            pass

        info = classify(Surprise("unexpected"))
        self.assertEqual(info.code, "INTERNAL")
        self.assertFalse(info.retryable)

    def test_only_races_are_retryable(self):
        """Retryable means 'the request was fine and lost'; nothing else qualifies."""
        retryable = {c for c in ALL_CODES if _sample_for(c) and classify(_sample_for(c)).retryable}
        self.assertEqual(retryable, {"VAULT_CONFLICT", "LOCK_TIMEOUT"})

    def test_legacy_value_error_handlers_still_catch(self):
        """vault.py and installer.py catch ValueError; the move must not reroute them."""
        self.assertIsInstance(VaultConfigError("x"), ValueError)
        self.assertIsInstance(FrontmatterParseError("x", 1), ValueError)
        self.assertIsInstance(VaultConfigError("x"), CadabbyError)

    def test_conflict_is_not_a_value_error(self):
        """A lost race is not a bad argument, and must not be caught as one."""
        self.assertNotIsInstance(VaultConflictError("raced"), ValueError)


def _sample_for(code: str) -> Exception | None:
    """One exception instance per code, or None for codes raised at a boundary."""
    samples: dict[str, Exception] = {
        "VAULT_CONFLICT": VaultConflictError("raced"),
        "LOCK_TIMEOUT": LockTimeoutError("held"),
        "CONFIG_INVALID": VaultConfigError("bad"),
        "FRONTMATTER_UNPARSEABLE": FrontmatterParseError("bad", 1),
        "FRONTMATTER_UNSERIALIZABLE": FrontmatterSerializeError("bad"),
        "NOT_FOUND": FileNotFoundError("gone"),
        "ALREADY_EXISTS": FileExistsError("there"),
        "INVALID_ARGUMENT": ValueError("huh"),
        "PERMISSION_DENIED": PermissionError("nope"),
        "IO_ERROR": OSError("disk"),
        "INTERNAL": RuntimeError("boom"),
    }
    return samples.get(code)


class TestTaxonomyIsClosed(unittest.TestCase):
    """The spec's table, errors.py, and the exit map must name one set.

    Same shape as the lint taxonomy guard: the risk is not a wrong code but
    a code that exists in one list and not the others, which no per-case
    test notices.
    """

    @staticmethod
    def _codes_named_by_spec():
        spec = (Path(__file__).resolve().parent.parent / "SPECIFICATION.md").read_text("utf-8")
        body = spec.split("### 5.4.", 1)[1].split("\n### ", 1)[0]
        rows = re.findall(r"^\| `([A-Z_]+)` \|", body, flags=re.MULTILINE)
        return set(rows)

    def test_spec_table_matches_the_module(self):
        self.assertEqual(self._codes_named_by_spec(), set(ALL_CODES))

    def test_every_code_has_an_exit_status(self):
        for code in ALL_CODES:
            with self.subTest(code=code):
                self.assertIn(exit_code_for(code), (EXIT_USAGE, EXIT_ENVIRONMENT, EXIT_TRANSIENT, EXIT_UNEXPECTED))

    def test_spec_table_agrees_with_the_exit_map(self):
        """The documented exit column is the contract a script reads."""
        spec = (Path(__file__).resolve().parent.parent / "SPECIFICATION.md").read_text("utf-8")
        body = spec.split("### 5.4.", 1)[1].split("\n### ", 1)[0]
        for code, documented in re.findall(r"^\| `([A-Z_]+)` \|.*\| (\d) \|$", body, flags=re.MULTILINE):
            with self.subTest(code=code):
                self.assertEqual(exit_code_for(code), int(documented))

    def test_spec_table_agrees_on_which_codes_retry(self):
        spec = (Path(__file__).resolve().parent.parent / "SPECIFICATION.md").read_text("utf-8")
        body = spec.split("### 5.4.", 1)[1].split("\n### ", 1)[0]
        documented = {
            code for code, yes in re.findall(r"^\| `([A-Z_]+)` \| (yes|no) \|", body, flags=re.MULTILINE) if yes == "yes"
        }
        self.assertEqual(documented, {"VAULT_CONFLICT", "LOCK_TIMEOUT"})


class TestMCPErrorPayload(unittest.TestCase):
    """What an agent actually receives when a tool fails."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.vault_root = copy_demo_vault(Path(self.tmp_dir.name) / "demo-vault")
        self.server = McpServer(Vault(self.vault_root))

    def tearDown(self):
        self.server.close()
        self.tmp_dir.cleanup()

    def _error(self, tool, args):
        res = self.server.handle_tools_call(tool, args)
        self.assertTrue(res["isError"], f"{tool} was expected to fail")
        return json.loads(res["content"][0]["text"])["error"]

    def test_a_failure_is_json_not_prose(self):
        err = self._error("vault_verify_note", {"cid": "wiki/Does-Not-Exist"})
        self.assertEqual(set(err), {"code", "message", "retryable", "tool"})
        self.assertEqual(err["tool"], "vault_verify_note")
        self.assertEqual(err["code"], "NOT_FOUND")

    def test_a_missing_required_argument_blames_the_caller(self):
        """Without the dispatch guard this is a KeyError, hence INTERNAL.

        "INTERNAL: 'cids'" tells an agent the server is broken and there is
        nothing to do; INVALID_ARGUMENT naming the field tells it what to send.
        """
        err = self._error("vault_ground", {})
        self.assertEqual(err["code"], "INVALID_ARGUMENT")
        self.assertIn("cids", err["message"])

    def test_the_required_guard_tracks_the_published_schema(self):
        """Both lists come from TOOLS, so a new required field cannot drift."""
        for tool in TOOLS:
            required = tool["inputSchema"].get("required", [])
            if not required:
                continue
            with self.subTest(tool=tool["name"]):
                err = self._error(tool["name"], {})
                self.assertEqual(err["code"], "INVALID_ARGUMENT")
                for field in required:
                    self.assertIn(field, err["message"])

    def test_optional_arguments_are_not_demanded(self):
        """vault_status requires nothing; the guard must not invent a field."""
        res = self.server.handle_tools_call("vault_status", {})
        self.assertFalse(res["isError"])

    def test_unknown_tool_is_coded(self):
        err = self._error("vault_teleport", {})
        self.assertEqual(err["code"], "UNKNOWN_TOOL")
        self.assertFalse(err["retryable"])

    def test_human_attestation_refusal_is_categorical(self):
        """§3.4. Not INVALID_ARGUMENT: there is no argument that would work."""
        for key in ("actor", "by"):
            with self.subTest(key=key):
                err = self._error("vault_verify_note", {"cid": "wiki/Flash-Attention", key: "human:attacker"})
                self.assertEqual(err["code"], "HUMAN_ATTESTATION_REFUSED")
                self.assertFalse(err["retryable"])

    def test_a_conflict_tells_the_caller_to_retry(self):
        """The one case where the flag changes what a competent agent does."""
        err = self._error(
            "vault_update_note",
            {
                "cid": "wiki/Flash-Attention",
                "patch_frontmatter": {"status": "deprecated"},
                "expected_hash": "sha256:" + "0" * 64,
            },
        )
        self.assertEqual(err["code"], "VAULT_CONFLICT")
        self.assertTrue(err["retryable"])

    def test_a_successful_call_carries_no_error_envelope(self):
        res = self.server.handle_tools_call("vault_status", {})
        self.assertFalse(res["isError"])
        self.assertNotIn("error", json.loads(res["content"][0]["text"]))


class TestExitCodes(unittest.TestCase):
    """Six statuses, because a script reacts differently to each (§5.4)."""

    def test_findings_and_failures_are_distinguishable(self):
        """The whole point: exit 1 must not mean 'your vault path is wrong'."""
        self.assertEqual(exit_code_for("NOT_FOUND"), EXIT_ENVIRONMENT)
        self.assertEqual(exit_code_for("CONFIG_INVALID"), EXIT_ENVIRONMENT)
        self.assertNotEqual(EXIT_ENVIRONMENT, 1)

    def test_transient_failures_are_their_own_status(self):
        self.assertEqual(exit_code_for("VAULT_CONFLICT"), EXIT_TRANSIENT)
        self.assertEqual(exit_code_for("LOCK_TIMEOUT"), EXIT_TRANSIENT)

    def test_argparse_status_is_not_reused(self):
        """argparse exits 2 on a bad invocation; only UNKNOWN_TOOL may share it."""
        sharing = {c for c in ALL_CODES if exit_code_for(c) == EXIT_USAGE}
        self.assertEqual(sharing, {"UNKNOWN_TOOL"})

    def test_an_unmapped_code_degrades_to_unexpected(self):
        self.assertEqual(exit_code_for("NOT_A_REAL_CODE"), EXIT_UNEXPECTED)


if __name__ == "__main__":
    unittest.main()

"""Shared test helpers and test fixtures for Cadabby test suites.

Pure Python standard library; zero external dependencies.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from cadabby.constants import FILE_CONFIG
from cadabby.vault import Vault

DEMO_VAULT_SRC = Path(__file__).resolve().parent.parent / "examples" / "demo-vault"


class DummyArgs:
    """Mock argparse.Namespace for CLI subcommand tests."""

    def __init__(self, **kwargs: Any):
        for k, v in kwargs.items():
            setattr(self, k, v)



# T1: a developer's exported CADABBY_VAULT must never be the default vault of a
# test. With it set, install tests that pass no vault wrote shims and plugin
# files into the developer's real vault. Every test module imports this one.
os.environ.pop("CADABBY_VAULT", None)

def copy_demo_vault(target_dir: Path | str) -> Path:
    """Create an isolated temporary copy of examples/demo-vault.

    Ignores ephemeral .cadabby cache, git metadata, and Python cache directories.
    """
    dst = Path(target_dir).resolve()
    shutil.copytree(
        DEMO_VAULT_SRC,
        dst,
        ignore=shutil.ignore_patterns(".cadabby", "*.pyc", "__pycache__", ".git"),
    )
    return dst


def create_test_vault(
    target_dir: Path | str,
    subdirs: Sequence[str] = ("wiki/concepts", "customers/acme", "projects/apollo", "raw", "log"),
    config: dict[str, Any] | None = None,
) -> Vault:
    """Create an isolated test vault directory tree with default or custom config."""
    dst = Path(target_dir).resolve()
    dst.mkdir(parents=True, exist_ok=True)
    for sub in subdirs:
        (dst / sub).mkdir(parents=True, exist_ok=True)
    if config is not None:
        (dst / FILE_CONFIG).write_text(json.dumps(config, indent=2) + "\n", "utf-8")
    return Vault(dst)


def temp_dir(case: unittest.TestCase) -> Path:
    """A fresh temporary directory, removed after the test.

    Registered with the test rather than cleaned in tearDown, so it is removed
    even when setUp fails partway (tearDown never runs then). Cleanups run
    after tearDown, so a tearDown that closes a cache or server still does so
    before the directory goes.
    """
    return Path(case.enterContext(tempfile.TemporaryDirectory()))


def demo_vault(case: unittest.TestCase, name: str = "vault") -> Vault:
    """A private copy of examples/demo-vault, removed after the test."""
    return Vault(copy_demo_vault(temp_dir(case) / name))


def note_text(
    title: str,
    *,
    type_: str = "concept",
    description: str = "d",
    status: str = "active",
    extra: Sequence[str] = (),
    quote_title: bool = False,
    quote_description: bool = False,
    body: str = "",
) -> str:
    """One valid note: the four required fields, then any `extra` frontmatter lines, then `body`.

    The quoting switches exist so the suite's older writers keep their exact
    bytes: lint line numbers, audit blame lines and body hashes depend on them.
    `body` is everything after the closing `---` line, verbatim.
    """
    title_text = f'"{title}"' if quote_title else title
    description_text = f'"{description}"' if quote_description else description
    lines = [
        "---",
        f"type: {type_}",
        f"title: {title_text}",
        f"description: {description_text}",
        f"status: {status}",
        *extra,
        "---",
    ]
    return "\n".join(lines) + "\n" + body


def write_note(root: Path, rel: str, text: str) -> Path:
    """Write `text` to root/rel, creating parent folders; return the path."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")
    return path

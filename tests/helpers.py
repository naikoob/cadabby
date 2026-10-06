"""Shared test helpers and test fixtures for Cadabby test suites.

Pure Python standard library; zero external dependencies.
"""

from __future__ import annotations

import json
import shutil
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

"""Vault root discovery, configuration loading, and path/CID mapping.

Conforms to Cadabby Technical Specification §2.1-§2.3.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cadabby.constants import (
    DEFAULT_INTEGRITY,
    DEFAULT_LOG_ROTATE_BYTES,
    DEFAULT_RAW_TEXT_EXTENSIONS,
    DEFAULT_STATUS_MULTIPLIERS,
    DEFAULT_TRUST_MULTIPLIERS,
    DIR_DOT_CADABBY,
    DIR_LOG,
    DIR_RAW,
    DIR_WIKI,
    FILE_CACHE_DB,
    FILE_CONFIG,
    FILE_INDEX,
    FILE_LOCK,
    FILE_LOG,
    SCHEMA_VERSION,
)


def find_vault_root(start_path: Path | str | None = None) -> Path | None:
    """Walk up the directory hierarchy to locate the vault root.

    Precedence:
    1. CADABBY_VAULT environment variable (if valid directory).
    2. Directory containing .cadabby.json.
    3. Directory containing .obsidian.
    """
    env_vault = os.environ.get("CADABBY_VAULT")
    if env_vault:
        p = Path(env_vault).resolve()
        if p.is_dir():
            return p

    current = Path(start_path or Path.cwd()).resolve()

    # If start path is a file, start from its parent directory
    if current.is_file():
        current = current.parent

    # Traverse upward looking for root markers
    for parent in [current, *current.parents]:
        if (parent / FILE_CONFIG).exists():
            return parent
        if (parent / ".obsidian").is_dir():
            return parent

    return None


class VaultConfigError(ValueError):
    """Raised when .cadabby.json is missing required structure or contains malformed JSON."""


def default_vault_config(vault_name: str = "vault") -> dict[str, Any]:
    """Return default vault configuration dictionary."""
    return {
        "schema": SCHEMA_VERSION,
        "vault_name": vault_name,
        "raw_text_extensions": list(DEFAULT_RAW_TEXT_EXTENSIONS),
        "integrity": DEFAULT_INTEGRITY,
        "ranking": {
            "trust": dict(DEFAULT_TRUST_MULTIPLIERS),
            "status": dict(DEFAULT_STATUS_MULTIPLIERS),
        },
        "identities": {},
        "obsidian": {
            "materialize_trust_tags": False,
        },
        "log_rotate_bytes": DEFAULT_LOG_ROTATE_BYTES,
    }


def load_vault_config(vault_root: Path) -> dict[str, Any]:
    """Load and merge .cadabby.json configuration with standard defaults."""
    cfg = default_vault_config(vault_root.name)
    cfg_file = vault_root / FILE_CONFIG

    if cfg_file.exists():
        try:
            content = cfg_file.read_text("utf-8")
            user_data = json.loads(content)
        except json.JSONDecodeError as e:
            raise VaultConfigError(f"Malformed configuration file at {cfg_file}: {e}") from e
        except OSError as e:
            raise OSError(f"Could not read configuration file at {cfg_file}: {e}") from e

        if not isinstance(user_data, dict):
            raise VaultConfigError(
                f"Invalid configuration format in {cfg_file}: root must be a JSON object, got {type(user_data).__name__}"
            )

        # Deep merge top-level keys and nested dictionaries
        for k, v in user_data.items():
            if k in ("ranking", "obsidian") and isinstance(v, dict):
                for sub_k, sub_v in v.items():
                    if isinstance(sub_v, dict) and isinstance(cfg[k].get(sub_k), dict):
                        cfg[k][sub_k].update(sub_v)
                    else:
                        cfg[k][sub_k] = sub_v
            else:
                cfg[k] = v

    return cfg


def path_to_cid(rel_path: str | Path) -> str:
    """Map a vault-relative path to its canonical CID.

    Examples:
        'wiki/concepts/Epistemic-Trust-Tiers.md' -> 'wiki/concepts/Epistemic-Trust-Tiers'
        'raw/paper.pdf' -> 'raw/paper.pdf'
    """
    clean_path = str(rel_path).replace("\\", "/").strip("/")
    if clean_path.startswith(f"{DIR_WIKI}/") and clean_path.endswith(".md"):
        return clean_path[:-3]
    return clean_path


def cid_to_path(cid: str) -> str:
    """Map a canonical CID back to its vault-relative path."""
    clean_cid = str(cid).replace("\\", "/").strip("/")
    if clean_cid.startswith(f"{DIR_WIKI}/") and not clean_cid.endswith(".md"):
        return f"{clean_cid}.md"
    return clean_cid


def path_to_layer(rel_path: str | Path) -> str:
    """Determine layer ('wiki' or 'raw') from relative path."""
    clean = str(rel_path).replace("\\", "/").strip("/")
    if clean.startswith(f"{DIR_WIKI}/"):
        return "wiki"
    return "raw"


def path_to_stem(rel_path: str | Path) -> str:
    """Extract the note or file stem."""
    return Path(rel_path).stem


@dataclass
class Vault:
    """Encapsulates a local Cadabby vault directory and configuration."""

    root: Path | str | None = None
    config: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.root is None:
            discovered = find_vault_root()
            self.root = discovered if discovered is not None else Path.cwd().resolve()
        else:
            self.root = Path(self.root).resolve()

        if not self.config:
            self.config = load_vault_config(self.root)

    @classmethod
    def open(cls, start_path: Path | str | None = None) -> Vault:
        """Discover and open a vault from start_path or current working directory."""
        root = find_vault_root(start_path)
        if root is None:
            raise FileNotFoundError(
                f"No Cadabby vault found at {start_path or Path.cwd()} (missing {FILE_CONFIG} or marker)"
            )
        return cls(root=root)

    @property
    def wiki_dir(self) -> Path:
        return self.root / DIR_WIKI

    @property
    def raw_dir(self) -> Path:
        return self.root / DIR_RAW

    @property
    def log_dir(self) -> Path:
        return self.root / DIR_LOG

    @property
    def dot_cadabby_dir(self) -> Path:
        return self.root / DIR_DOT_CADABBY

    @property
    def cache_db_path(self) -> Path:
        return self.dot_cadabby_dir / FILE_CACHE_DB

    @property
    def lock_path(self) -> Path:
        return self.dot_cadabby_dir / FILE_LOCK

    @property
    def index_path(self) -> Path:
        return self.root / FILE_INDEX

    @property
    def log_path(self) -> Path:
        return self.root / FILE_LOG

    def rel_path(self, path: Path | str) -> str:
        """Convert an absolute or relative path to a vault-relative path with POSIX separators."""
        p = Path(path).resolve()
        try:
            return p.relative_to(self.root).as_posix()
        except ValueError:
            return Path(path).as_posix()

    def abs_path(self, rel_path: str | Path) -> Path:
        """Resolve a vault-relative path to an absolute Path."""
        return (self.root / rel_path).resolve()

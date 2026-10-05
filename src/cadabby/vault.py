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
    DEFAULT_IGNORED_DIRS,
    DEFAULT_INTEGRITY,
    DEFAULT_LOG_ROTATE_BYTES,
    DEFAULT_RAW_TEXT_EXTENSIONS,
    DEFAULT_STATUS_MULTIPLIERS,
    DEFAULT_TRUST_MULTIPLIERS,
    DIR_DOT_CADABBY,
    DIR_LOG,
    DIR_RAW,
    DIR_WIKI,
    FILE_AGENTS,
    FILE_CACHE_DB,
    FILE_CONFIG,
    FILE_INDEX,
    FILE_LOCK,
    FILE_LOG,
    NOTE_TYPES,
    SCHEMA_VERSION,
)
from cadabby.domain import DomainDefinition
from cadabby.frontmatter import FrontmatterParseError, parse_frontmatter


def is_vault_root(path: Path | str) -> bool:
    """Return True if path is a directory carrying a vault root marker."""
    p = Path(path)
    return (p / FILE_CONFIG).exists() or (p / ".obsidian").is_dir()


def env_vault_root() -> Path | None:
    """Return the vault root designated by CADABBY_VAULT, if the variable is set.

    Like --vault, the variable names a root exactly: it is never used as a
    walk-up starting point, so it cannot silently resolve to an ancestor vault.
    """
    env_vault = os.environ.get("CADABBY_VAULT")
    if not env_vault:
        return None
    root = Path(env_vault).resolve()
    if not is_vault_root(root):
        raise FileNotFoundError(
            f"CADABBY_VAULT is set to {root}, which is not a Cadabby vault "
            f"(missing {FILE_CONFIG} or marker). Unset it or point it at a vault root."
        )
    return root


def vault_search_start(start_path: Path | str | None = None) -> Path:
    """Resolve where cwd-relative vault discovery begins."""
    if start_path is not None:
        return Path(start_path).resolve()
    return Path.cwd().resolve()


def find_vault_root(start_path: Path | str | None = None) -> Path | None:
    """Locate the vault root, in order of precedence.

    1. Explicit start_path (e.g. a caller-supplied search origin), walked upward.
    2. CADABBY_VAULT environment variable, taken as an exact root.
    3. The current working directory, walked upward.

    CADABBY_VAULT is consulted only when start_path is None, since an explicit
    start point is the more specific request. Walking upward looks for a root
    marker (.cadabby.json or .obsidian); a start point with no marker above it
    yields None rather than being accepted as a root. Raises FileNotFoundError
    if CADABBY_VAULT is set but does not name a vault.

    Note that --vault does not reach here: it designates a root exactly, via
    Vault.at(), which never walks up.
    """
    if start_path is None:
        env_root = env_vault_root()
        if env_root is not None:
            return env_root

    current = vault_search_start(start_path)

    # If start path is a file, start from its parent directory
    if current.is_file():
        current = current.parent

    # Traverse upward looking for root markers
    for parent in [current, *current.parents]:
        if is_vault_root(parent):
            return parent

    return None


def _no_vault_message(location: Path | str) -> str:
    """Build the canonical 'not a vault' error message."""
    return (
        f"No Cadabby vault found at {location} (missing {FILE_CONFIG} or marker). "
        "Run 'cadabby init' to create one."
    )


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


def _type_name(value: Any) -> str:
    """Describe a JSON value's type using JSON vocabulary."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _check_config_type(cfg_file: Path, key: str, value: Any, default: Any) -> None:
    """Reject a user-supplied value whose JSON type differs from the default's.

    Keys absent from the defaults are unconstrained, so forward-compatible
    additions still pass through. Without this, a null or mistyped value reaches
    consumers far from the config file and fails as an opaque TypeError.
    """
    if isinstance(default, bool):
        ok = isinstance(value, bool)
    elif isinstance(default, (int, float)):
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    else:
        ok = isinstance(value, type(default))
    if not ok:
        raise VaultConfigError(
            f"Invalid configuration value in {cfg_file}: '{key}' must be "
            f"{_type_name(default)}, got {_type_name(value)}"
        )


def load_vault_config(vault_root: Path) -> dict[str, Any]:
    """Load and merge .cadabby.json configuration with standard defaults.

    Raises VaultConfigError if the file is malformed or if a known key carries a
    value of the wrong JSON type.
    """
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

        # Deep merge top-level keys and nested dictionaries, type-checking every
        # value that has a default so mistyped config fails here and not deep
        # inside an unrelated consumer.
        defaults = default_vault_config(vault_root.name)
        for k, v in user_data.items():
            if k in defaults:
                _check_config_type(cfg_file, k, v, defaults[k])
            if k in ("ranking", "obsidian"):
                for sub_k, sub_v in v.items():
                    if sub_k in defaults[k]:
                        _check_config_type(cfg_file, f"{k}.{sub_k}", sub_v, defaults[k][sub_k])
                    if isinstance(sub_v, dict) and isinstance(cfg[k].get(sub_k), dict):
                        cfg[k][sub_k].update(sub_v)
                    else:
                        cfg[k][sub_k] = sub_v
            else:
                cfg[k] = v

    return cfg


def path_to_cid(rel_path: str | Path) -> str:
    """Map a vault-relative path to its canonical CID across all domains.

    Examples:
        'wiki/concepts/Epistemic-Trust-Tiers.md' -> 'wiki/concepts/Epistemic-Trust-Tiers'
        'customers/acme/README.md' -> 'customers/acme/README'
        'projects/apollo/rfc-001.md' -> 'projects/apollo/rfc-001'
        'raw/paper.pdf' -> 'raw/paper.pdf'
        'raw/notes.md' -> 'raw/notes.md'
    """
    clean_path = str(rel_path).replace("\\", "/").strip("/")
    first_seg = clean_path.split("/")[0]
    if first_seg in (DIR_RAW, DIR_LOG) or not clean_path.endswith(".md"):
        return clean_path
    return clean_path[:-3]


def cid_to_path(cid: str) -> str:
    """Map a canonical CID back to its vault-relative path across all domains.

    Examples:
        'wiki/concepts/Epistemic-Trust-Tiers' -> 'wiki/concepts/Epistemic-Trust-Tiers.md'
        'customers/acme/README' -> 'customers/acme/README.md'
        'raw/paper.pdf' -> 'raw/paper.pdf'
    """
    clean_cid = str(cid).replace("\\", "/").strip("/")
    first_seg = clean_cid.split("/")[0]
    if first_seg in (DIR_RAW, DIR_LOG) or clean_cid.endswith(".md"):
        return clean_cid
    return f"{clean_cid}.md"


def path_to_layer(rel_path: str | Path) -> str:
    """Determine domain or layer name from the relative path.

    Examples:
        'wiki/concepts/Foo.md' -> 'wiki'
        'customers/acme/README.md' -> 'customers'
        'projects/apollo/rfc-001.md' -> 'projects'
        'raw/data.csv' -> 'raw'
    """
    clean = str(rel_path).replace("\\", "/").strip("/")
    parts = clean.split("/")
    return parts[0] if len(parts) > 1 else "root"


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
        """Discover and open a vault, per find_vault_root's precedence.

        With no start_path, CADABBY_VAULT names the root exactly if set;
        otherwise discovery walks up from the cwd.
        """
        root = find_vault_root(start_path)
        if root is None:
            raise FileNotFoundError(_no_vault_message(vault_search_start(start_path)))
        return cls(root=root)

    @classmethod
    def at(cls, path: Path | str) -> Vault:
        """Open the vault rooted exactly at path, without walking up the hierarchy.

        Used for explicitly designated roots (e.g. --vault) so that a typo'd or
        non-vault path fails instead of silently resolving to an ancestor vault.
        """
        root = Path(path).resolve()
        if not is_vault_root(root):
            raise FileNotFoundError(_no_vault_message(root))
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
    def cache_path(self) -> Path:
        """Alias for cache_db_path."""
        return self.cache_db_path

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
        path_obj = Path(path)
        p = path_obj.resolve() if path_obj.is_absolute() else (self.root / path_obj).resolve()
        try:
            return p.relative_to(self.root).as_posix()
        except ValueError:
            return path_obj.as_posix()

    def abs_path(self, rel_path: str | Path) -> Path:
        """Resolve a vault-relative path to an absolute Path."""
        return (self.root / rel_path).resolve()

    def discover_domains(self) -> dict[str, DomainDefinition]:
        """Discover all cognitive domains in the vault."""
        domains: dict[str, DomainDefinition] = {}

        # 1. Built-in wiki domain (default contract if no AGENTS.md)
        wiki_agents_md = self.wiki_dir / FILE_AGENTS
        if wiki_agents_md.exists():
            domains[DIR_WIKI] = self._load_domain_from_agents_md(DIR_WIKI, self.wiki_dir, wiki_agents_md)
        else:
            domains[DIR_WIKI] = DomainDefinition(
                name=DIR_WIKI,
                path=self.wiki_dir,
                description="Canonical knowledge base",
                allowed_types=None,
                require_sources=False,
                directives_markdown="",
            )

        # 2. Discover arbitrary top-level directories
        if self.root.exists():
            for entry in sorted(self.root.iterdir()):
                if not entry.is_dir():
                    continue
                name = entry.name
                if (
                    name.startswith(".")
                    or name in (DIR_WIKI, DIR_RAW, DIR_LOG)
                    or name in DEFAULT_IGNORED_DIRS
                ):
                    continue

                agents_md = entry / FILE_AGENTS
                if agents_md.exists():
                    domains[name] = self._load_domain_from_agents_md(name, entry, agents_md)
                else:
                    domains[name] = DomainDefinition(
                        name=name,
                        path=entry,
                        description=f"{name.capitalize()} domain",
                        allowed_types=None,  # Open
                        require_sources=False,
                        directives_markdown="",
                    )
        return domains

    def _load_domain_from_agents_md(
        self, name: str, dir_path: Path, agents_md: Path
    ) -> DomainDefinition:
        """Parse an AGENTS.md file into a DomainDefinition."""
        raw_text = agents_md.read_text("utf-8", errors="replace")
        try:
            fm, body = parse_frontmatter(raw_text)
        except (FrontmatterParseError, ValueError):
            fm, body = {}, raw_text

        schema_cfg = fm.get("schema", {}) if isinstance(fm.get("schema"), dict) else fm

        allowed_types = schema_cfg.get("allowed_types", fm.get("allowed_types"))
        if isinstance(allowed_types, list):
            allowed_types_list: list[str] | None = [str(t) for t in allowed_types]
        else:
            allowed_types_list = None

        require_sources = bool(schema_cfg.get("require_sources", fm.get("require_sources", False)))
        description = str(fm.get("description", f"{name.capitalize()} domain"))

        return DomainDefinition(
            name=name,
            path=dir_path,
            description=description,
            allowed_types=allowed_types_list,
            require_sources=require_sources,
            directives_markdown=body.strip(),
        )

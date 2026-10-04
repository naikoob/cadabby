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
                searchable=True,
                allowed_types=list(NOTE_TYPES),
                require_sources=False,
                enforce_layout=True,
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
                        searchable=True,
                        allowed_types=None,  # Open
                        require_sources=False,
                        enforce_layout=False,
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
        elif name == DIR_WIKI:
            allowed_types_list = list(NOTE_TYPES)
        else:
            allowed_types_list = None

        searchable = bool(fm.get("searchable", True))
        require_sources = bool(schema_cfg.get("require_sources", fm.get("require_sources", False)))
        enforce_layout = bool(schema_cfg.get("enforce_layout", fm.get("enforce_layout", (name == DIR_WIKI))))
        description = str(fm.get("description", f"{name.capitalize()} domain"))

        return DomainDefinition(
            name=name,
            path=dir_path,
            description=description,
            searchable=searchable,
            allowed_types=allowed_types_list,
            require_sources=require_sources,
            enforce_layout=enforce_layout,
            directives_markdown=body.strip(),
        )

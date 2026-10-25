"""Vault root discovery, configuration loading, and the `Vault` aggregate.

Conforms to Cadabby Technical Specification §2.1-§2.3. The path/CID algebra
this module used to carry now lives in `paths.py`: it needs no vault, and
keeping it here made five modules that wanted nothing else depend on config
loading and manifest parsing to get it.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cadabby.constants import (
    CONFIG_SCHEMA_VERSION,
    DEFAULT_DOMAIN_MULTIPLIERS,
    DEFAULT_INTEGRITY,
    DEFAULT_LOG_ROTATE_BYTES,
    DEFAULT_RAW_TEXT_EXTENSIONS,
    DEFAULT_STATUS_MULTIPLIERS,
    DEFAULT_TRUST_MULTIPLIERS,
    DIR_DOT_CADABBY,
    DIR_LOG,
    DIR_OBSIDIAN,
    DIR_RAW,
    DIR_WIKI,
    FILE_AGENTS,
    FILE_CACHE_DB,
    FILE_CONFIG,
    FILE_INDEX,
    FILE_LOCK,
    FILE_LOG,
    FILE_OBSIDIAN_TEMPLATES,
    INTEGRITY_MODES,
)
from cadabby.domain import DomainDefinition

# Re-exported: the taxonomy lives in errors.py (§5.4), but every existing
# `from cadabby.vault import VaultConfigError` still resolves.
from cadabby.errors import InvalidArgumentError, NotFoundError, VaultConfigError
from cadabby.frontmatter import FrontmatterParseError, parse_frontmatter, split_frontmatter
from cadabby.fsutil import contained
from cadabby.paths import is_excluded_dir, normalize_rel, note_path_violation, template_rel_filename


def is_vault_root(path: Path | str) -> bool:
    """Return True if path is a directory carrying a vault root marker."""
    p = Path(path)
    return (p / FILE_CONFIG).exists() or (p / DIR_OBSIDIAN).is_dir()


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
        raise NotFoundError(
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
    """Locate the vault root per §2.7 levels 2 and 3.

    Level 1 (--vault) never reaches here: it names a root exactly, via
    Vault.at(), which never walks up.

    2. CADABBY_VAULT, taken as an exact root. Raises FileNotFoundError if it
       is set but does not name a vault.
    3. The working directory, walked upward to the first .cadabby.json or
       .obsidian/ marker. None if no marker is found.

    `start_path` replaces the working directory as the walk's origin (it is
    how callers ask "which vault contains this file?"), and being the more
    specific request, it also skips level 2.
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


def default_vault_config(vault_name: str = "vault") -> dict[str, Any]:
    """Return default vault configuration dictionary."""
    return {
        "schema": CONFIG_SCHEMA_VERSION,
        "vault_name": vault_name,
        "raw_text_extensions": list(DEFAULT_RAW_TEXT_EXTENSIONS),
        "integrity": DEFAULT_INTEGRITY,
        "ranking": {
            "trust": dict(DEFAULT_TRUST_MULTIPLIERS),
            "status": dict(DEFAULT_STATUS_MULTIPLIERS),
            "domain": dict(DEFAULT_DOMAIN_MULTIPLIERS),
        },
        "identities": {},
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
    elif isinstance(default, int):
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif isinstance(default, float):
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
            # utf-8-sig: Windows editors prepend a BOM, which json.loads rejects.
            content = cfg_file.read_text("utf-8-sig")
            user_data = json.loads(content)
        except UnicodeDecodeError as e:
            raise VaultConfigError(f"Configuration file at {cfg_file} is not valid UTF-8: {e}") from e
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
            if k == "ranking":
                for sub_k, sub_v in v.items():
                    if sub_k in defaults[k]:
                        _check_config_type(cfg_file, f"{k}.{sub_k}", sub_v, defaults[k][sub_k])
                    if isinstance(sub_v, dict) and isinstance(cfg[k].get(sub_k), dict):
                        cfg[k][sub_k].update(sub_v)
                    else:
                        cfg[k][sub_k] = sub_v
            else:
                # Unrecognized keys survive the merge rather than raising, so a
                # config written for another version still loads (§2.6). The
                # price is that the engine must never ship a key it does not
                # read: `obsidian.materialize_trust_tags` was one, and nothing
                # could have told its users it did nothing.
                cfg[k] = v

        _check_config_values(cfg_file, cfg)

    return cfg


def _check_config_values(cfg_file: Path, cfg: dict[str, Any]) -> None:
    """Reject values of the right JSON type but outside their domain (§2.6).

    After this, every consumer may index the known keys directly: there is no
    second, silent repair further down (search used to coerce a bad multiplier
    to 1.0 and rank on, with nothing telling the user their setting was inert).
    """

    def fail(key: str, expected: str) -> None:
        raise VaultConfigError(f"Invalid configuration value in {cfg_file}: '{key}' must be {expected}")

    if cfg["integrity"] not in INTEGRITY_MODES:
        fail("integrity", " or ".join(f"'{m}'" for m in INTEGRITY_MODES))
    if isinstance(cfg["log_rotate_bytes"], bool) or not isinstance(cfg["log_rotate_bytes"], int) or cfg["log_rotate_bytes"] < 1:
        fail("log_rotate_bytes", "a positive integer")
    for table in ("trust", "status", "domain"):
        for name, mult in cfg["ranking"][table].items():
            # json.loads accepts NaN and Infinity; neither renders as SQL.
            if isinstance(mult, bool) or not isinstance(mult, (int, float)) or not math.isfinite(mult):
                fail(f"ranking.{table}.{name}", "a finite number")
    if not all(isinstance(ext, str) for ext in cfg["raw_text_extensions"]):
        fail("raw_text_extensions", "an array of strings")
    for actor, emails in cfg["identities"].items():
        if not isinstance(emails, list) or not all(isinstance(e, str) for e in emails):
            fail(f"identities.{actor}", "an array of email strings")


def obsidian_template_dir(root: Path | str) -> Path | None:
    """The folder Obsidian's core Templates plugin writes templates into (§7.5).

    Read from `.obsidian/templates.json` rather than mirrored into
    `.cadabby.json` because that file is where the user already declared it:
    a duplicated setting goes stale the moment they rename the folder in
    Obsidian, and the symptom would be `lint` turning red with no hint why.

    Taking a bare root rather than a Vault is what lets `init` ask the question
    before a vault is loadable, so the one command that may write this file
    agrees by construction with the engine that reads it.

    Returns None when Obsidian is absent, the plugin is unconfigured, or the
    setting is unusable. Failing safe is deliberate -- a corrupt third-party
    config should cost a lint finding that names the template, not a vault
    that refuses to open.
    """
    root = Path(root).resolve()
    settings = root / DIR_OBSIDIAN / FILE_OBSIDIAN_TEMPLATES
    try:
        data = json.loads(settings.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    folder = data.get("folder")
    if not isinstance(folder, str) or not folder.strip():
        return None

    rel = normalize_rel(folder)
    parts = [p for p in rel.split("/") if p and p != "."]
    # Checked before stripping made it invisible: '/etc' survives strip('/')
    # as 'etc' and would silently exclude <vault>/etc.
    if not parts or Path(folder).is_absolute():
        return None

    # The one containment test, run after resolve() so it also catches a
    # '..' segment and a template folder symlinked out of the vault. A
    # string-level '..' check alongside it would be a second rule saying
    # less, and no test could tell the two apart.
    candidate = (root / Path(*parts)).resolve()
    if candidate != root and root not in candidate.parents:
        return None
    return candidate


@dataclass
class Vault:
    """Encapsulates a local Cadabby vault directory and configuration.

    `root` is taken as given. Discovery (§2.7) lives in `Vault.open()` and
    `Vault.at()`; there is no silent fallback to the working directory, which
    used to open a non-vault as though it were one.
    """

    # `Path`, not `Path | str`: __post_init__ resolves it before any attribute
    # read, so the union only ever described the constructor's leniency while
    # every one of the ~12 `self.root / ...` uses relied on it being a Path.
    # The coercion below still accepts a str from an untyped caller.
    root: Path
    config: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
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
            raise NotFoundError(_no_vault_message(vault_search_start(start_path)))
        return cls(root=root)

    @classmethod
    def at(cls, path: Path | str) -> Vault:
        """Open the vault rooted exactly at path, without walking up the hierarchy.

        Used for explicitly designated roots (e.g. --vault) so that a typo'd or
        non-vault path fails instead of silently resolving to an ancestor vault.
        """
        root = Path(path).resolve()
        if not is_vault_root(root):
            raise NotFoundError(_no_vault_message(root))
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
        """Convert an absolute or relative path to a vault-relative path with POSIX separators.

        A path inside the vault that a symlink sends elsewhere (`docs ->
        ~/Documents/notes`, followed on read by design) keeps its in-vault
        name: falling back to the absolute path minted CIDs like
        `home/me/Documents/notes/a`.
        """
        path_obj = Path(path)
        lexical = path_obj if path_obj.is_absolute() else self.root / path_obj
        p = lexical.resolve()
        try:
            return p.relative_to(self.root).as_posix()
        except ValueError:
            pass
        try:
            return Path(os.path.normpath(lexical)).relative_to(self.root).as_posix()
        except ValueError:
            return path_obj.as_posix()

    def contained_path(self, path: Path | str) -> Path:
        """The real path a write to `path` would land on; VaultEscapeError if outside the vault (§8)."""
        p = Path(path)
        return contained(self.root, p if p.is_absolute() else self.root / p)

    def abs_path(self, rel_path: str | Path) -> Path:
        """Resolve a vault-relative path to an absolute Path."""
        return (self.root / rel_path).resolve()

    @property
    def raw_text_extensions(self) -> tuple[str, ...]:
        """File extensions under `raw/` that the engine reads as UTF-8 text (§2.4, §2.6)."""
        return tuple(self.config["raw_text_extensions"])

    def template_dir(self) -> Path | None:
        """The folder Obsidian's core Templates plugin writes templates into (§7.5)."""
        return obsidian_template_dir(self.root)

    def template_rel(self) -> str | None:
        """`template_dir()` as a vault-relative path, the form `note_path_violation` takes."""
        tpl_dir = self.template_dir()
        return self.rel_path(tpl_dir) if tpl_dir is not None else None

    def note_path_violation(self, rel_path: str, *, for_write: bool = False) -> str | None:
        """`note_path_violation` against this vault's template folder."""
        return note_path_violation(rel_path, self.template_rel(), for_write=for_write)

    def read_template_body(self, name: str) -> str:
        """Body of a named Obsidian template, with its frontmatter discarded.

        Only the body is returned. A template's frontmatter is placeholder-ridden
        by design (`title: {{title}}` is flow style, `tags: []` a flow sequence)
        and would not survive §3.2, but more importantly OKF frontmatter is
        engine-owned: `generated`, `verified` and the hash they bind to are
        epistemic infrastructure, and a template controlling them could mint
        notes whose trust tier asserts more than the vault can back (§7.5).
        """
        tpl_dir = self.template_dir()
        if tpl_dir is None:
            raise NotFoundError(
                "No Obsidian template folder is configured; expected a 'folder' key in "
                f"{DIR_OBSIDIAN}/{FILE_OBSIDIAN_TEMPLATES}"
            )

        filename = template_rel_filename(name)

        # As in template_dir, containment is tested once, after resolve().
        path = (tpl_dir / filename).resolve()
        if tpl_dir not in path.parents:
            raise InvalidArgumentError(f"Invalid template name '{name}'")
        if not path.is_file():
            raise NotFoundError(f"Template not found: {self.rel_path(path)}")

        _, body = split_frontmatter(path.read_text("utf-8"))
        return body

    def has_folder(self, rel_path: str) -> bool:
        """Whether `rel_path` is an existing directory in the vault (§5.1 `vault_move_note`)."""
        clean = normalize_rel(rel_path)
        return bool(clean) and (self.root / clean).is_dir()

    def list_folder_files(self, rel_prefix: str) -> Sequence[str]:
        """Vault-relative paths of visible files under `rel_prefix/`, sorted (§5.1 `vault_move_note`)."""
        clean = normalize_rel(rel_prefix)
        src_dir = self.root / clean
        if not clean or not src_dir.is_dir():
            return ()
        files: list[str] = []
        for path in sorted(src_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.root).as_posix()
            if any(part.startswith(".") for part in rel.split("/")):
                continue
            files.append(rel)
        return files

    def discover_domains(self) -> dict[str, DomainDefinition]:
        """Discover all cognitive domains in the vault."""
        domains: dict[str, DomainDefinition] = {}

        # 1. Built-in wiki domain (default contract if no AGENTS.md)
        wiki_agents_md = self.wiki_dir / FILE_AGENTS
        if wiki_agents_md.exists():
            domains[DIR_WIKI] = self._load_domain_from_agents_md(DIR_WIKI, self.wiki_dir, wiki_agents_md)
        else:
            domains[DIR_WIKI] = DomainDefinition.permissive(DIR_WIKI, self.wiki_dir, "Canonical knowledge base")

        # 2. Discover arbitrary top-level directories
        if self.root.exists():
            tpl_dir = self.template_dir()
            for entry in sorted(self.root.iterdir()):
                if not entry.is_dir():
                    continue
                name = entry.name
                if is_excluded_dir(name) or name in (DIR_WIKI, DIR_RAW, DIR_LOG):
                    continue
                # A template folder holds pre-notes, not notes (§7.5).
                if tpl_dir is not None and entry.resolve() == tpl_dir:
                    continue

                agents_md = entry / FILE_AGENTS
                if agents_md.exists():
                    domains[name] = self._load_domain_from_agents_md(name, entry, agents_md)
                else:
                    domains[name] = DomainDefinition.permissive(name, entry)
        return domains

    def iter_domain_notes(self) -> Iterator[tuple[Path, str, DomainDefinition]]:
        """Every note file across every cognitive domain, in a stable order.

        Four modules walked the domains themselves and the copies had already
        drifted: `cache.py` omitted the `.md` filter, so a PNG dropped into
        `wiki/` was read with `errors="replace"`, indexed as a note, listed in
        `index.md` as unfiled work and reported by gate 5 as an orphan. Two of
        the four also left `os.walk`'s `files` unsorted, making row insertion
        order depend on readdir order (§4.4, C1).

        Yields `(absolute path, domain name, domain)`. Callers that need only
        one of the three discard the rest; what matters is that "which files
        are notes" is answered in exactly one place.
        """
        # Prunes a nested template folder such as `meta/templates`, where the
        # parent is a real domain and only the subtree is excluded. A top-level
        # one never reaches here, discover_domains having already dropped it.
        tpl_dir = self.template_dir()

        for domain_name, domain_def in self.discover_domains().items():
            if not domain_def.path.exists():
                continue
            for root, dirs, files in os.walk(domain_def.path, topdown=True):
                dirs[:] = sorted(
                    d
                    for d in dirs
                    if not is_excluded_dir(d) and (tpl_dir is None or (Path(root) / d).resolve() != tpl_dir)
                )
                for f in sorted(files):
                    # AGENTS.md is the domain's own manifest, not a note in it.
                    if not f.endswith(".md") or f.startswith(".") or f == FILE_AGENTS:
                        continue
                    path = Path(root) / f
                    # A regular file can only land outside the vault through a
                    # folder link note_read_allowed accepts, so only links pay
                    # for the resolve().
                    if path.is_symlink() and not self.note_read_allowed(path):
                        continue
                    yield path, domain_name, domain_def

    def note_read_allowed(self, lexical: Path) -> bool:
        """Whether a note file named in-vault may be read: it resolves inside the vault or its own domain folder (§8).

        A folder symlinked in (`docs -> ~/notes`) is the user's choice and is
        read under its in-vault name. A single file linking out
        (`wiki/x.md -> /etc/passwd`) is not a note this vault holds: indexing
        it put the outside file's text into search snippets while no tool
        could open it. This mirrors iter_raw_files for raw/, and is the one
        answer shared by the scan walk and the storage adapter.
        """
        real = lexical.resolve()
        if real.is_relative_to(self.root):
            return True
        try:
            rel = lexical.relative_to(self.root)
        except ValueError:
            return False
        if len(rel.parts) < 2:
            return False  # a file at the vault root has no domain folder to stay inside
        return real.is_relative_to((self.root / rel.parts[0]).resolve())

    def iter_raw_files(self) -> Iterator[Path]:
        """Every file under raw/, in a stable order (§2.4).

        The single answer to "which files are raw evidence", shared by the
        cache scan and DiskNoteStorage. Pruning applies only below raw/: the
        scan's former copy also filtered on the absolute path's parts, which
        silently dropped every raw file of a vault living under a folder named
        `target` or `venv` while the storage adapter still listed them.
        """
        if not self.raw_dir.exists():
            return
        # raw/ itself may be a symlink to a shared corpus; a file inside it that
        # links somewhere else entirely is not evidence this vault holds (§2.4).
        raw_real = self.raw_dir.resolve()
        for root, dirs, files in os.walk(self.raw_dir, topdown=True):
            dirs[:] = sorted(d for d in dirs if not is_excluded_dir(d))
            for f in sorted(files):
                path = Path(root) / f
                if not f.startswith(".") and path.resolve().is_relative_to(raw_real):
                    yield path

    def _load_domain_from_agents_md(
        self, name: str, dir_path: Path, agents_md: Path
    ) -> DomainDefinition:
        """Parse `{domain}/AGENTS.md` into a DomainDefinition (§2.2)."""
        return parse_domain_manifest(name, agents_md.read_text("utf-8", errors="replace"), dir_path)


def parse_domain_manifest(name: str, raw_text: str, dir_path: Path = Path()) -> DomainDefinition:
    """Parse `{domain}/AGENTS.md` text into a DomainDefinition (§2.2).

    A manifest that cannot be honored is not silently read as "open": the
    domain still loads permissively so search keeps working, but
    `manifest_error` says why, lint reports DOMAIN_MANIFEST_INVALID, and
    scaffold into the domain is refused until it is fixed.
    """
    try:
        fm, body = parse_frontmatter(raw_text)
    except FrontmatterParseError as e:
        _, body = _split_without_parsing(raw_text)
        domain = DomainDefinition.permissive(name, dir_path)
        domain.directives_markdown = body.strip()
        domain.manifest_error = f"frontmatter does not parse: {e}"
        return domain

    problems: list[str] = []
    allowed_types = fm.get("allowed_types")
    allowed_types_list: list[str] | None = None
    if allowed_types is not None:
        if isinstance(allowed_types, list) and all(isinstance(t, str) for t in allowed_types):
            allowed_types_list = list(allowed_types)
        else:
            problems.append("'allowed_types' must be a list of strings")

    require_sources = fm.get("require_sources", False)
    if not isinstance(require_sources, bool):
        problems.append("'require_sources' must be true or false")
        require_sources = False

    declared = fm.get("domain")
    if declared is not None and declared != name:
        problems.append(f"'domain: {declared}' does not match the folder name '{name}'")

    return DomainDefinition(
        name=name,
        path=dir_path,
        description=str(fm.get("description") or f"{name.capitalize()} domain"),
        allowed_types=allowed_types_list,
        require_sources=require_sources,
        directives_markdown=body.strip(),
        manifest_error="; ".join(problems) or None,
    )


def _split_without_parsing(raw_text: str) -> tuple[str | None, str]:
    """Split frontmatter from body without interpreting it, so raw YAML never leaks into directives."""
    try:
        return split_frontmatter(raw_text)
    except FrontmatterParseError:
        return None, raw_text

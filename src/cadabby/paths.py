"""Vault-relative path and CID algebra: pure string work, no filesystem (§2.1, §2.2).

A leaf, like `markdown.py`, and for the same reason. These nine functions used
to live in `vault.py` alongside config loading, vault discovery, the `Vault`
aggregate and domain-manifest parsing. Five modules -- `graph`, `ground`,
`move`, `resolve` and `search` -- imported *only* this algebra, and so took on
a dependency on all of it: `vault.py` had the highest fan-in in the tree (15),
and nothing in those five needed a vault to exist, let alone a parsed one.

Nothing here touches the disk or reads config, so nothing here can grow one:
`is_excluded_dir` answers from a name, and both `*_violation` functions judge
a string. Anything needing the real folder belongs on `Vault`.

`test_paths_is_a_leaf_module` keeps the import list down to constants/errors.
"""

from __future__ import annotations

import posixpath
from pathlib import Path

from cadabby.constants import (
    DEFAULT_IGNORED_DIRS,
    DIR_LOG,
    DIR_RAW,
    DIR_WIKI,
    FILE_AGENTS,
)
from cadabby.errors import InvalidArgumentError


def normalize_rel(path: str | Path) -> str:
    """Forward slashes, no leading or trailing slash: the one spelling of a vault-relative path."""
    return str(path).replace("\\", "/").strip("/")


def path_to_cid(rel_path: str | Path) -> str:
    """Map a vault-relative path to its canonical CID across all domains.

    Examples:
        'wiki/concepts/Epistemic-Trust-Tiers.md' -> 'wiki/concepts/Epistemic-Trust-Tiers'
        'customers/acme/README.md' -> 'customers/acme/README'
        'projects/apollo/rfc-001.md' -> 'projects/apollo/rfc-001'
        'raw/paper.pdf' -> 'raw/paper.pdf'
        'raw/notes.md' -> 'raw/notes.md'
    """
    clean_path = normalize_rel(rel_path)
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
    clean_cid = str(cid).replace("\\", "/").rstrip("/")
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
    clean = normalize_rel(rel_path)
    parts = clean.split("/")
    return parts[0] if len(parts) > 1 else "root"


def path_to_stem(rel_path: str | Path) -> str:
    """Extract the note or file stem."""
    return Path(rel_path).stem


def is_excluded_dir(name: str) -> bool:
    """Whether discovery skips a folder of this name at any depth (§2.2)."""
    return name.startswith(".") or name in DEFAULT_IGNORED_DIRS


def source_path_violation(rel_path: str) -> str | None:
    """Why `rel_path` cannot name raw evidence, or None if it can (§2.4).

    The one statement of "which raw/ paths are sources", shared by both storage
    adapters (the scan's walk prunes the same names): the in-memory twin once
    accepted `raw/.git/x` and `raw/node_modules/x`, states the disk adapter can
    never produce, and the two disagreed on backslashes.
    """
    if "\\" in rel_path:
        return "a source path uses '/' separators"
    parts = rel_path.split("/")
    if len(parts) < 2 or parts[0] != DIR_RAW:
        return f"a source lives under {DIR_RAW}/"
    if any(p in ("", ".", "..") for p in parts):
        return "empty, '.' or '..' segments are not allowed"
    if any(is_excluded_dir(p) for p in parts[1:-1]):
        return "it is inside a hidden or ignored folder"
    if parts[-1].startswith("."):
        return "a hidden file is not evidence"
    return None


def note_path_violation(rel_path: str, template_rel: str | None = None, *, for_write: bool = False) -> str | None:
    """Why `rel_path` cannot hold a note, or None if it can (§2.1, §2.2, §7.5).

    The single statement of "which paths are notes": the scan's walk
    (`discover_domains` + `iter_domain_notes`), the storage adapter, Markdown
    link classification and scaffold all ask here. `template_rel` is the
    vault-relative Obsidian template folder, if one is configured.

    `for_write` adds the flat-wiki rule. A nested wiki note is still a note --
    the scan indexes it so gate 2 can report it -- but nothing may create one.
    """
    if not rel_path or str(rel_path).startswith(("/", "\\")) or Path(rel_path).is_absolute():
        return "a note path must be relative to the vault root"
    clean = normalize_rel(rel_path)
    parts = clean.split("/")
    if len(parts) < 2 or any(p in ("", ".", "..") for p in parts):
        return "a note lives inside a domain folder, never at the vault root"
    *dirs, name = parts
    if any(is_excluded_dir(d) for d in dirs):
        return "dot-folders and ignored folders are never indexed"
    if dirs[0] in (DIR_RAW, DIR_LOG):
        return f"'{dirs[0]}/' is reserved and holds no notes"
    if template_rel and clean.startswith(normalize_rel(template_rel) + "/"):
        return "the Obsidian template folder holds templates, not notes"
    if name == FILE_AGENTS:
        return f"{FILE_AGENTS} is the domain's manifest, not a note"
    if name.startswith(".") or not name.endswith(".md"):
        return "a note is a visible .md file"
    if for_write and dirs[0] == DIR_WIKI and len(parts) > 2:
        return "wiki/ is flat; nested folders are not allowed (use an MOC in wiki/ or a top-level domain folder)"
    return None


def template_rel_filename(name: str) -> str:
    """Validate a template `name` and return its `.md` path relative to the template folder (§7.5).

    Shared by `Vault` and `InMemoryVaultLayout` so both reject empty, absolute,
    and parent-climbing names identically.
    """
    # is_absolute is asked of the original, as in template_dir: strip('/')
    # would turn '/etc/passwd' into a plausible-looking relative name.
    stem = normalize_rel(name)
    if not stem or Path(name).is_absolute():
        raise InvalidArgumentError(f"Invalid template name '{name}'")
    filename = stem if stem.endswith(".md") else f"{stem}.md"
    norm = posixpath.normpath(filename)
    if norm.startswith("../") or norm in ("", ".", ".."):
        raise InvalidArgumentError(f"Invalid template name '{name}'")
    return norm

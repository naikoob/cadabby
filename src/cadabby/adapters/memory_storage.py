"""In-memory secondary adapters for lightning-fast hermetic testing and ephemeral execution.

Provides full fidelity with NoteStoragePort, LedgerPort, and VaultLayoutPort without any disk or file I/O.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from pathlib import Path

from cadabby.constants import (
    DEFAULT_RAW_TEXT_EXTENSIONS,
    DIR_LOG,
    DIR_OBSIDIAN,
    DIR_RAW,
    DIR_WIKI,
    FILE_AGENTS,
    FILE_OBSIDIAN_TEMPLATES,
)
from cadabby.domain import DomainDefinition, Note
from cadabby.frontmatter import split_frontmatter
from cadabby.fsutil import VaultConflictError, check_expected_hash, compute_bytes_sha256
from cadabby.okf import ledger_line
from cadabby.ports import LedgerPort, NoteStoragePort, VaultLayoutPort
from cadabby.vault import (
    cid_to_path,
    is_excluded_dir,
    normalize_rel,
    note_path_violation,
    parse_domain_manifest,
    path_to_cid,
    source_path_violation,
    template_rel_filename,
)


def _content_hash(note: Note) -> str:
    """The in-memory twin of DiskNoteStorage's token: the hash of the stored bytes."""
    return compute_bytes_sha256(note.serialize().encode("utf-8"))


class InMemoryVaultLayout(VaultLayoutPort):
    """Hermetic in-memory twin of `Vault` for `VaultLayoutPort` (§9.1).

    Calls the same single-source rules as `Vault` (`note_path_violation`,
    `parse_domain_manifest`, `template_rel_filename`, `split_frontmatter`) so
    hermetic use-case tests enforce domain contracts, template rules, folder
    collisions, leftover-file warnings, and `raw_text_extensions` identically.
    """

    def __init__(
        self,
        *,
        template_rel: str | None = None,
        raw_text_extensions: Sequence[str] = DEFAULT_RAW_TEXT_EXTENSIONS,
        domains: Mapping[str, str | DomainDefinition] | None = None,
        templates: Mapping[str, str] | None = None,
    ) -> None:
        self._template_rel = normalize_rel(template_rel) if template_rel else None
        self._raw_text_extensions = tuple(raw_text_extensions)
        self._domains: dict[str, DomainDefinition] = {
            DIR_WIKI: DomainDefinition.permissive(DIR_WIKI, Path(DIR_WIKI), "Canonical knowledge base")
        }
        self._templates: dict[str, str] = {}
        self._folders: set[str] = {DIR_WIKI}
        self._files: dict[str, str] = {}
        if domains:
            for name, spec in domains.items():
                self.put_domain(name, spec)
        if templates:
            for name, text in templates.items():
                self.put_template(name, text)

    @property
    def raw_text_extensions(self) -> tuple[str, ...]:
        return self._raw_text_extensions

    def set_raw_text_extensions(self, extensions: Sequence[str]) -> None:
        """Test seam: configure `raw_text_extensions` (§2.4, §2.6)."""
        self._raw_text_extensions = tuple(extensions)

    def put_domain(self, name: str, manifest: str | DomainDefinition | None = None) -> DomainDefinition:
        """Test seam: register a cognitive domain and optional `{domain}/AGENTS.md` text (§2.2)."""
        clean = normalize_rel(name)
        if not clean or "/" in clean:
            raise ValueError(f"Domain name must be a single top-level folder, got {name!r}")
        self._folders.add(clean)
        if isinstance(manifest, DomainDefinition):
            self._domains[clean] = manifest
        elif isinstance(manifest, str):
            self._domains[clean] = parse_domain_manifest(clean, manifest, Path(clean))
            self._files[f"{clean}/{FILE_AGENTS}"] = manifest
        else:
            self._domains[clean] = DomainDefinition.permissive(clean, Path(clean))
        return self._domains[clean]

    def put_template(self, name: str, raw_text: str) -> None:
        """Test seam: place a template in the Obsidian template folder (§7.5)."""
        if self._template_rel is None:
            self._template_rel = "templates"
        rel = template_rel_filename(name)
        self._templates[rel] = raw_text

    def put_folder(self, rel_path: str) -> None:
        """Test seam: mark a vault-relative directory as existing."""
        clean = normalize_rel(rel_path)
        parts = [p for p in clean.split("/") if p]
        for i in range(1, len(parts) + 1):
            self._folders.add("/".join(parts[:i]))

    def put_file(self, rel_path: str, content: str = "") -> None:
        """Test seam: place a non-note file (e.g. an attachment in a domain folder)."""
        clean = normalize_rel(rel_path)
        if not clean or "/" not in clean:
            raise ValueError(f"Folder file path must be inside a folder, got {rel_path!r}")
        self.put_folder(clean.rsplit("/", 1)[0])
        self._files[clean] = content

    def note_path_violation(self, rel_path: str, *, for_write: bool = False) -> str | None:
        return note_path_violation(rel_path, self._template_rel, for_write=for_write)

    def discover_domains(self) -> dict[str, DomainDefinition]:
        out: dict[str, DomainDefinition] = {}
        top_folders = {f.split("/")[0] for f in self._folders} | set(self._domains)
        for name in sorted(top_folders):
            if name != DIR_WIKI and (is_excluded_dir(name) or name in (DIR_RAW, DIR_LOG) or name == self._template_rel):
                continue
            out[name] = self._domains.get(name) or DomainDefinition.permissive(name, Path(name))
        return out

    def read_template_body(self, name: str) -> str:
        if self._template_rel is None:
            raise FileNotFoundError(
                "No Obsidian template folder is configured; expected a 'folder' key in "
                f"{DIR_OBSIDIAN}/{FILE_OBSIDIAN_TEMPLATES}"
            )
        rel = template_rel_filename(name)
        if rel not in self._templates:
            raise FileNotFoundError(f"Template not found: {self._template_rel}/{rel}")
        _, body = split_frontmatter(self._templates[rel])
        return body

    def has_folder(self, rel_path: str) -> bool:
        clean = normalize_rel(rel_path)
        return bool(clean) and (
            clean in self._folders
            or any(f.startswith(clean + "/") for f in self._folders)
            or any(f.startswith(clean + "/") for f in self._files)
        )

    def list_folder_files(self, rel_prefix: str) -> Sequence[str]:
        clean = normalize_rel(rel_prefix)
        if not clean:
            return ()
        return sorted(
            rel
            for rel in self._files
            if rel.startswith(clean + "/") and not any(part.startswith(".") for part in rel.split("/"))
        )


class InMemoryNoteStorage(NoteStoragePort):
    """Secondary adapter storing notes purely in memory."""

    def __init__(
        self,
        initial_notes: Sequence[Note] | None = None,
        *,
        template_rel: str | None = None,
        layout: InMemoryVaultLayout | None = None,
    ) -> None:
        """`template_rel` plays the vault's Obsidian template folder (§7.5), as on disk."""
        self._notes: dict[str, Note] = {}
        self.layout = layout if layout is not None else InMemoryVaultLayout(template_rel=template_rel)
        self._sources: dict[str, str] = {}

        if initial_notes:
            for n in initial_notes:
                self.save_note(n)

    def _lookup_existing(self, cid_or_path: str) -> Note | None:
        rel_path = cid_to_path(cid_or_path)
        if self.layout.note_path_violation(rel_path):
            return None
        return self._notes.get(path_to_cid(rel_path))

    def get_note(self, cid_or_path: str) -> Note | None:
        note = self._lookup_existing(cid_or_path)
        if note is None:
            return None
        copied = copy.deepcopy(note)
        copied.source_hash = _content_hash(copied)
        return copied

    def save_note(self, note: Note, expected_hash: str | None = None) -> None:
        raw = str(note.rel_path or note.cid).replace("\\", "/")
        if raw.startswith("/") or ".." in raw.split("/"):
            raise ValueError(f"Cannot save note outside vault root: {note.rel_path or note.cid}")
        rel_path = cid_to_path(note.rel_path or note.cid)
        if why := self.layout.note_path_violation(rel_path):
            raise ValueError(f"Cannot save note at '{rel_path}': {why}")
        cid = path_to_cid(rel_path)
        existing = self._notes.get(cid)

        if expected_hash is not None:
            if existing is None:
                # Same rule as atomic_replace_checked: never recreate a note
                # someone deleted after the caller read it (§8).
                raise VaultConflictError(f"VAULT_CONFLICT: {rel_path} was deleted concurrently in memory")
            check_expected_hash(_content_hash(existing), expected_hash, rel_path, where="in memory")

        # Round-trip through serialize() -> Note.from_raw() so tag canonicalization,
        # key ordering, and empty-collection omission match DiskNoteStorage 1:1.
        serialized = note.serialize()
        source_hash = compute_bytes_sha256(serialized.encode("utf-8"))
        self._notes[cid] = Note.from_raw(
            cid=cid,
            rel_path=rel_path,
            raw_text=serialized,
            source_hash=source_hash,
        )

    def note_exists(self, cid_or_path: str) -> bool:
        return self._lookup_existing(cid_or_path) is not None

    def delete_note(self, cid_or_path: str, expected_hash: str | None = None, *, prune_empty_dirs: bool = False) -> None:
        # `prune_empty_dirs` is accepted for port parity; memory has no folders.
        rel_path = cid_to_path(cid_or_path)
        if why := self.layout.note_path_violation(rel_path):
            raise ValueError(f"Cannot delete '{rel_path}': {why}")
        cid = path_to_cid(rel_path)
        existing = self._notes.get(cid)
        if existing is None:
            raise FileNotFoundError(f"Note not found: {rel_path}")
        if expected_hash is not None:
            check_expected_hash(_content_hash(existing), expected_hash, rel_path, where="in memory")
        del self._notes[cid]

    def read_raw(self, cid_or_path: str) -> str | None:
        note = self._lookup_existing(cid_or_path)
        return note.serialize() if note is not None else None

    def list_note_cids(self) -> Sequence[str]:
        return sorted(self._notes.keys())

    def list_source_paths(self) -> Sequence[str]:
        return sorted(self._sources)

    def read_source(self, rel_path: str, max_bytes: int | None = None) -> str | None:
        # Keys are only ever raw/ paths (see put_source), so an exact lookup
        # is the whole confinement rule here.
        text = None if source_path_violation(rel_path) else self._sources.get(rel_path)
        if text is None or max_bytes is None:
            return text
        return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="replace")

    def put_source(self, rel_path: str, text: str) -> None:
        """Test seam: place a raw/ file. Memory plays the human who curates raw/."""
        if why := source_path_violation(rel_path):
            raise ValueError(f"Not a source path: {rel_path} ({why})")
        self._sources[rel_path] = text


class InMemoryLedger(LedgerPort):
    """Secondary adapter recording log entries in an in-memory list."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str | None]] = []

    def append(self, message: str, actor: str | None = None) -> None:
        ledger_line(message, actor)  # the disk ledger's rules: one line, a valid actor
        self.entries.append((message, actor))

"""Disk-based secondary adapters conforming to NoteStoragePort and LedgerPort.

Encapsulates all direct POSIX filesystem operations, atomic tempfile replacements,
and path conversions within the vault directory boundary.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from cadabby.constants import DEFAULT_IGNORED_DIRS
from cadabby.domain import Note
from cadabby.fsutil import atomic_replace_checked
from cadabby.indexer import append_vault_log
from cadabby.ports import LedgerPort, NoteStoragePort
from cadabby.vault import Vault, cid_to_path, path_to_cid


class DiskNoteStorage(NoteStoragePort):
    """Secondary adapter providing filesystem storage for markdown notes and raw assets."""

    def __init__(self, vault: Vault):
        self.vault = vault

    def get_note(self, cid_or_path: str) -> Note | None:
        rel_path = cid_to_path(cid_or_path)
        target = self.vault.abs_path(rel_path)
        if not target.is_relative_to(self.vault.root):
            return None
        if not target.exists() or not target.is_file():
            return None

        raw_text = target.read_text("utf-8")
        cid = path_to_cid(rel_path)
        return Note.from_raw(cid=cid, rel_path=rel_path, raw_text=raw_text)

    def save_note(self, note: Note, expected_hash: str | None = None) -> None:
        target = self.vault.abs_path(note.rel_path)
        if not target.is_relative_to(self.vault.root):
            raise ValueError(f"Cannot save note outside vault root: {note.rel_path}")
        serialized = note.serialize()
        atomic_replace_checked(target, serialized, expected_hash=expected_hash)

    def note_exists(self, cid_or_path: str) -> bool:
        rel_path = cid_to_path(cid_or_path)
        target = self.vault.abs_path(rel_path)
        if not target.is_relative_to(self.vault.root):
            return False
        return target.is_file()

    def list_note_cids(self) -> Sequence[str]:
        cids = [path_to_cid(self.vault.rel_path(p)) for p, _, _ in self.vault.iter_domain_notes()]
        return sorted(cids)

    def list_raw_sources(self) -> Sequence[str]:
        if not self.vault.raw_dir.exists():
            return []
        sources = []
        for root, dirs, files in os.walk(self.vault.raw_dir, topdown=True):
            dirs[:] = [d for d in dirs if d not in DEFAULT_IGNORED_DIRS and not d.startswith(".")]
            for f in files:
                if not f.startswith("."):
                    p = Path(root) / f
                    sources.append(self.vault.rel_path(p))
        return sorted(sources)

    def raw_source_exists(self, rel_path: str) -> bool:
        clean = rel_path.replace("\\", "/").strip("/")
        target = self.vault.abs_path(clean)
        if not target.is_relative_to(self.vault.root):
            return False
        return target.is_file()


class FileLedger(LedgerPort):
    """Secondary adapter providing append-only disk logging to log.md."""

    def __init__(self, vault: Vault):
        self.vault = vault

    def append(self, message: str, actor: str | None = None) -> None:
        append_vault_log(self.vault, message, actor=actor)

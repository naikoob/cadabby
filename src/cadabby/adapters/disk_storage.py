"""Disk-based secondary adapters conforming to NoteStoragePort and LedgerPort.

Encapsulates all direct POSIX filesystem operations, atomic tempfile replacements,
and path conversions within the vault directory boundary.
"""

from __future__ import annotations

from collections.abc import Sequence

from cadabby.constants import BODY_HASH_PREFIX
from cadabby.domain import Note
from cadabby.fsutil import VaultConflictError, atomic_replace_checked, compute_bytes_sha256
from cadabby.indexer import append_vault_log
from cadabby.ports import LedgerPort, NoteStoragePort
from cadabby.vault import Vault, cid_to_path, path_to_cid


class DiskNoteStorage(NoteStoragePort):
    """Secondary adapter providing filesystem storage for markdown notes."""

    def __init__(self, vault: Vault) -> None:
        self.vault = vault

    def get_note(self, cid_or_path: str) -> Note | None:
        rel_path = cid_to_path(cid_or_path)
        if self.vault.note_path_violation(rel_path):
            return None  # e.g. index.md or raw/: a file, but never a note (§2.1)
        target = self.vault.abs_path(rel_path)
        if not target.is_relative_to(self.vault.root) or not target.is_file():
            return None

        # The concurrency token is the hash of the bytes on disk, the same
        # thing atomic_replace_checked recomputes. Hashing decoded text instead
        # made every CRLF note fail verify/update with VAULT_CONFLICT (C31).
        data = target.read_bytes()
        cid = path_to_cid(rel_path)
        return Note.from_raw(
            cid=cid,
            rel_path=rel_path,
            raw_text=data.decode("utf-8"),
            source_hash=compute_bytes_sha256(data),
        )

    def save_note(self, note: Note, expected_hash: str | None = None) -> None:
        target = self.vault.abs_path(note.rel_path)
        if not target.is_relative_to(self.vault.root):
            raise ValueError(f"Cannot save note outside vault root: {note.rel_path}")
        if why := self.vault.note_path_violation(note.rel_path):
            raise ValueError(f"Cannot save note at '{note.rel_path}': {why}")
        serialized = note.serialize()
        atomic_replace_checked(target, serialized, expected_hash=expected_hash)

    def note_exists(self, cid_or_path: str) -> bool:
        rel_path = cid_to_path(cid_or_path)
        if self.vault.note_path_violation(rel_path):
            return False
        target = self.vault.abs_path(rel_path)
        return target.is_relative_to(self.vault.root) and target.is_file()

    def delete_note(self, cid_or_path: str, expected_hash: str | None = None, *, prune_empty_dirs: bool = False) -> None:
        rel_path = cid_to_path(cid_or_path)
        if why := self.vault.note_path_violation(rel_path):
            raise ValueError(f"Cannot delete '{rel_path}': {why}")
        target = self.vault.abs_path(rel_path)
        if not target.is_relative_to(self.vault.root) or not target.is_file():
            raise FileNotFoundError(f"Note not found: {rel_path}")
        if expected_hash is not None:
            current = compute_bytes_sha256(target.read_bytes())
            expected = expected_hash.removeprefix(BODY_HASH_PREFIX)
            if current != expected:
                raise VaultConflictError(
                    f"VAULT_CONFLICT: {rel_path} was modified concurrently on disk "
                    f"(expected {expected[:12]}, found {current[:12]})"
                )
        # Unlink the path as named, not `target`: abs_path() resolves symlinks,
        # and deleting a symlinked note must remove the link, never the file
        # it points at.
        link = self.vault.root / rel_path
        link.unlink()
        if prune_empty_dirs:
            parent = link.parent
            while parent != self.vault.root and parent.is_relative_to(self.vault.root) and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent

    def read_raw(self, cid_or_path: str) -> str | None:
        rel_path = cid_to_path(cid_or_path)
        if self.vault.note_path_violation(rel_path):
            return None
        target = self.vault.abs_path(rel_path)
        if not target.is_relative_to(self.vault.root) or not target.is_file():
            return None
        return target.read_bytes().decode("utf-8", errors="replace")

    def list_note_cids(self) -> Sequence[str]:
        # rel_path() resolves symlinks, so a symlinked note and its target
        # map to one CID; a set keeps it from being listed (and planned) twice.
        return sorted({path_to_cid(self.vault.rel_path(p)) for p, _, _ in self.vault.iter_domain_notes()})


class FileLedger(LedgerPort):
    """Secondary adapter providing append-only disk logging to log.md."""

    def __init__(self, vault: Vault) -> None:
        self.vault = vault

    def append(self, message: str, actor: str | None = None) -> None:
        append_vault_log(self.vault, message, actor=actor)

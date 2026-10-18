"""In-memory secondary adapters for lightning-fast hermetic testing and ephemeral execution.

Provides full fidelity with NoteStoragePort and LedgerPort without any disk or file I/O.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence

from cadabby.domain import Note
from cadabby.okf import ledger_line
from cadabby.fsutil import VaultConflictError, check_expected_hash, compute_bytes_sha256
from cadabby.ports import LedgerPort, NoteStoragePort
from cadabby.vault import cid_to_path, note_path_violation, path_to_cid, source_path_violation


def _content_hash(note: Note) -> str:
    """The in-memory twin of DiskNoteStorage's token: the hash of the stored bytes."""
    return compute_bytes_sha256(note.serialize().encode("utf-8"))


class InMemoryNoteStorage(NoteStoragePort):
    """Secondary adapter storing notes purely in memory."""

    def __init__(self, initial_notes: Sequence[Note] | None = None, *, template_rel: str | None = None) -> None:
        """`template_rel` plays the vault's Obsidian template folder (§7.5), as on disk."""
        self._notes: dict[str, Note] = {}
        self._template_rel = template_rel
        self._sources: dict[str, str] = {}

        if initial_notes:
            for n in initial_notes:
                self.save_note(n)

    def _lookup_existing(self, cid_or_path: str) -> Note | None:
        rel_path = cid_to_path(cid_or_path)
        if note_path_violation(rel_path, self._template_rel):
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
        if why := note_path_violation(rel_path, self._template_rel):
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
        if why := note_path_violation(rel_path, self._template_rel):
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

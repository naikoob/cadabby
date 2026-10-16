"""In-memory secondary adapters for lightning-fast hermetic testing and ephemeral execution.

Provides full fidelity with NoteStoragePort and LedgerPort without any disk or file I/O.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence

from cadabby.domain import Note
from cadabby.fsutil import check_expected_hash, compute_bytes_sha256
from cadabby.ports import LedgerPort, NoteStoragePort
from cadabby.vault import cid_to_path, note_path_violation, path_to_cid


def _content_hash(note: Note) -> str:
    """The in-memory twin of DiskNoteStorage's token: the hash of the stored bytes."""
    return compute_bytes_sha256(note.serialize().encode("utf-8"))


class InMemoryNoteStorage(NoteStoragePort):
    """Secondary adapter storing notes purely in memory."""

    def __init__(self, initial_notes: Sequence[Note] | None = None) -> None:
        self._notes: dict[str, Note] = {}

        if initial_notes:
            for n in initial_notes:
                self.save_note(n)

    def _lookup_existing(self, cid_or_path: str) -> Note | None:
        rel_path = cid_to_path(cid_or_path)
        if note_path_violation(rel_path):
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
        if why := note_path_violation(rel_path):
            raise ValueError(f"Cannot save note at '{rel_path}': {why}")
        cid = path_to_cid(rel_path)
        existing = self._notes.get(cid)

        if expected_hash is not None and existing is not None:
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
        if why := note_path_violation(rel_path):
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


class InMemoryLedger(LedgerPort):
    """Secondary adapter recording log entries in an in-memory list."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str | None]] = []

    def append(self, message: str, actor: str | None = None) -> None:
        self.entries.append((message, actor))

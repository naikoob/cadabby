"""In-memory secondary adapters for lightning-fast hermetic testing and ephemeral execution.

Provides full fidelity with NoteStoragePort and LedgerPort without any disk or file I/O.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence

from cadabby.constants import BODY_HASH_PREFIX
from cadabby.domain import Note
from cadabby.fsutil import VaultConflictError, compute_bytes_sha256
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

    def get_note(self, cid_or_path: str) -> Note | None:
        rel_path = cid_to_path(cid_or_path)
        if note_path_violation(rel_path):
            return None
        note = self._notes.get(path_to_cid(rel_path))
        if note is None:
            return None
        copied = copy.deepcopy(note)
        copied.source_hash = _content_hash(copied)
        return copied

    def save_note(self, note: Note, expected_hash: str | None = None) -> None:
        rel_path = cid_to_path(note.rel_path or note.cid)
        if why := note_path_violation(rel_path):
            raise ValueError(f"Cannot save note at '{rel_path}': {why}")
        cid = path_to_cid(rel_path)
        existing = self._notes.get(cid)

        if expected_hash is not None and existing is not None:
            existing_hash = _content_hash(existing)
            norm_expected = expected_hash.removeprefix(BODY_HASH_PREFIX)
            if existing_hash != norm_expected:
                raise VaultConflictError(
                    f"VAULT_CONFLICT: {rel_path} was modified concurrently in memory "
                    f"(expected {norm_expected[:12]}, found {existing_hash[:12]})"
                )

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
        rel_path = cid_to_path(cid_or_path)
        if note_path_violation(rel_path):
            return False
        return path_to_cid(rel_path) in self._notes

    def list_note_cids(self) -> Sequence[str]:
        return sorted(self._notes.keys())


class InMemoryLedger(LedgerPort):
    """Secondary adapter recording log entries in an in-memory list."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str | None]] = []

    def append(self, message: str, actor: str | None = None) -> None:
        self.entries.append((message, actor))

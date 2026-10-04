"""In-memory secondary adapters for lightning-fast hermetic testing and ephemeral execution.

Provides full fidelity with NoteStoragePort and LedgerPort without any disk or file I/O.
"""

from __future__ import annotations

import copy
import hashlib
from typing import Sequence

from cadabby.domain import Note
from cadabby.fsutil import VaultConflictError
from cadabby.ports import LedgerPort, NoteStoragePort
from cadabby.vault import cid_to_path, path_to_cid


class InMemoryNoteStorage(NoteStoragePort):
    """Secondary adapter storing notes and raw sources purely in memory."""

    def __init__(self, initial_notes: Sequence[Note] | None = None, initial_raw: Sequence[str] | None = None):
        self._notes: dict[str, Note] = {}
        self._raw_sources: set[str] = set(initial_raw or [])

        if initial_notes:
            for n in initial_notes:
                self.save_note(n)

    def _normalize_cid(self, cid_or_path: str) -> str:
        clean = cid_or_path.replace("\\", "/").strip("/")
        return path_to_cid(clean)

    def get_note(self, cid_or_path: str) -> Note | None:
        cid = self._normalize_cid(cid_or_path)
        note = self._notes.get(cid)
        return copy.deepcopy(note) if note is not None else None

    def save_note(self, note: Note, expected_hash: str | None = None) -> None:
        cid = self._normalize_cid(note.cid)
        existing = self._notes.get(cid)

        if expected_hash is not None and existing is not None:
            existing_text = existing.serialize()
            existing_hash = hashlib.sha256(existing_text.encode("utf-8")).hexdigest()
            norm_expected = expected_hash.removeprefix("sha256:")
            if existing_hash != norm_expected:
                raise VaultConflictError(
                    f"VAULT_CONFLICT: {note.rel_path} was modified concurrently in memory "
                    f"(expected {norm_expected[:12]}, found {existing_hash[:12]})"
                )

        # Store a deepcopy to ensure isolation
        stored = copy.deepcopy(note)
        stored.cid = cid
        if not stored.rel_path:
            stored.rel_path = cid_to_path(cid)
        self._notes[cid] = stored

    def note_exists(self, cid_or_path: str) -> bool:
        cid = self._normalize_cid(cid_or_path)
        return cid in self._notes

    def list_note_cids(self) -> Sequence[str]:
        return sorted(self._notes.keys())

    def list_raw_sources(self) -> Sequence[str]:
        return sorted(self._raw_sources)

    def raw_source_exists(self, rel_path: str) -> bool:
        clean = rel_path.replace("\\", "/").strip("/")
        return clean in self._raw_sources


class InMemoryLedger(LedgerPort):
    """Secondary adapter recording log entries in an in-memory list."""

    def __init__(self):
        self.entries: list[tuple[str, str | None]] = []

    def append(self, message: str, actor: str | None = None) -> None:
        self.entries.append((message, actor))

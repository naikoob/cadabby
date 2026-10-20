"""Disk-based secondary adapters conforming to NoteStoragePort and LedgerPort.

Encapsulates all direct POSIX filesystem operations, atomic tempfile replacements,
and path conversions within the vault directory boundary.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from cadabby.domain import Note
from cadabby.errors import InvalidArgumentError, NotFoundError
from cadabby.frontmatter import FrontmatterParseError
from cadabby.fsutil import atomic_replace_checked, check_expected_hash, compute_bytes_sha256
from cadabby.indexer import append_vault_log
from cadabby.ports import LedgerPort, NoteStoragePort
from cadabby.vault import Vault, cid_to_path, path_to_cid, source_path_violation


class DiskNoteStorage(NoteStoragePort):
    """Secondary adapter providing filesystem storage for markdown notes."""

    def __init__(self, vault: Vault) -> None:
        self.vault = vault

    def _resolve_existing_file(self, cid_or_path: str) -> tuple[str, Path] | None:
        rel_path = cid_to_path(cid_or_path)
        if self.vault.note_path_violation(rel_path):
            return None  # e.g. index.md or raw/: a file, but never a note (§2.1)
        # The same read rule as the scan's walk (§8): a folder symlinked in is
        # read under its in-vault name, a single file linking out is not. Before,
        # a note in `docs -> ~/notes` was searchable but NOT_FOUND here.
        lexical = self.vault.root / rel_path
        if not self.vault.note_read_allowed(lexical) or not lexical.is_file():
            return None
        return rel_path, lexical.resolve()

    def get_note(self, cid_or_path: str) -> Note | None:
        resolved = self._resolve_existing_file(cid_or_path)
        if resolved is None:
            return None
        rel_path, target = resolved

        # The concurrency token is the hash of the bytes on disk, the same
        # thing atomic_replace_checked recomputes. Hashing decoded text instead
        # made every CRLF note fail verify/update with VAULT_CONFLICT (C31).
        data = target.read_bytes()
        cid = path_to_cid(rel_path)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            # The file, not the caller's arguments, is at fault; search already
            # shows this note (the scan decodes leniently), so name it.
            raise FrontmatterParseError(f"not valid UTF-8 at byte {exc.start} (in {rel_path})", 1) from None
        try:
            return Note.from_raw(
                cid=cid,
                rel_path=rel_path,
                raw_text=text,
                source_hash=compute_bytes_sha256(data),
            )
        except FrontmatterParseError as exc:
            # The parser knows the line, not the file; every caller (update,
            # verify, move, ground) would otherwise report a pathless error.
            raise FrontmatterParseError(f"{exc.message} (in {rel_path})", exc.line_number) from None

    def save_note(self, note: Note, expected_hash: str | None = None) -> None:
        if why := self.vault.note_path_violation(note.rel_path):
            raise InvalidArgumentError(f"Cannot save note at '{note.rel_path}': {why}")
        # Judged by where the bytes would land (§8): a symlink leaving the vault
        # is refused, and one inside it (even dangling) must point at a place a
        # note may live -- `wiki/x.md -> ../raw/p.md` would write into raw/.
        target = self.vault.contained_path(note.rel_path)
        landing = self.vault.rel_path(target)
        if landing != note.rel_path and (why := self.vault.note_path_violation(landing, for_write=True)):
            raise InvalidArgumentError(
                f"Cannot save note at '{note.rel_path}': it is a symlink to '{landing}', and {why}"
            )
        serialized = note.serialize()
        atomic_replace_checked(target, serialized, expected_hash=expected_hash)

    def note_exists(self, cid_or_path: str) -> bool:
        return self._resolve_existing_file(cid_or_path) is not None

    def delete_note(self, cid_or_path: str, expected_hash: str | None = None, *, prune_empty_dirs: bool = False) -> None:
        rel_path = cid_to_path(cid_or_path)
        if why := self.vault.note_path_violation(rel_path):
            raise InvalidArgumentError(f"Cannot delete '{rel_path}': {why}")
        target = self.vault.abs_path(rel_path)
        if not target.is_relative_to(self.vault.root) or not target.is_file():
            raise NotFoundError(f"Note not found: {rel_path}")
        if expected_hash is not None:
            check_expected_hash(compute_bytes_sha256(target.read_bytes()), expected_hash, rel_path)
        # Unlink the path as named, not `target`: abs_path() resolves symlinks,
        # and deleting a symlinked note must remove the link, never the file
        # it points at.
        link = self.vault.root / rel_path
        link.unlink()
        if prune_empty_dirs:
            protected = {self.vault.root, self.vault.wiki_dir, self.vault.raw_dir}
            parent = link.parent
            while parent not in protected and parent.is_relative_to(self.vault.root) and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent

    def read_raw(self, cid_or_path: str) -> str | None:
        resolved = self._resolve_existing_file(cid_or_path)
        if resolved is None:
            return None
        return resolved[1].read_bytes().decode("utf-8", errors="replace")

    def list_note_cids(self) -> Sequence[str]:
        # rel_path() resolves symlinks, so a symlinked note and its target
        # map to one CID; a set keeps it from being listed (and planned) twice.
        return sorted({path_to_cid(self.vault.rel_path(p)) for p, _, _ in self.vault.iter_domain_notes()})

    def list_source_paths(self) -> Sequence[str]:
        # The scan's own walk (§2.4), so this lists exactly the cache's raw rows;
        # iter_raw_files already leaves out a symlink resolving outside raw/.
        return sorted({self.vault.rel_path(p) for p in self.vault.iter_raw_files()})

    def read_source(self, rel_path: str, max_bytes: int | None = None) -> str | None:
        if source_path_violation(rel_path):
            return None
        target = (self.vault.root / rel_path).resolve()
        if not target.is_relative_to(self.vault.raw_dir.resolve()) or not target.is_file():
            return None
        if max_bytes is None:
            return target.read_bytes().decode("utf-8", errors="replace")
        with open(target, "rb") as fh:
            return fh.read(max_bytes).decode("utf-8", errors="replace")


class FileLedger(LedgerPort):
    """Secondary adapter providing append-only disk logging to log.md."""

    def __init__(self, vault: Vault) -> None:
        self.vault = vault

    def append(self, message: str, actor: str | None = None) -> None:
        append_vault_log(self.vault, message, actor=actor)

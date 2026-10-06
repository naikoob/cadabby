"""Abstract Ports (Driven/Secondary interfaces) for Cadabby.

Defines the contracts between the domain/application core and external infrastructure
(filesystem, SQLite, version control, logging) using standard-library typing.Protocol.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from cadabby.domain import Note


@runtime_checkable
class NoteStoragePort(Protocol):
    """Secondary port for persisting and retrieving notes and raw sources."""

    def get_note(self, cid_or_path: str) -> Note | None:
        """Retrieve Note entity by CID or vault-relative path. Returns None if not found."""
        ...

    def save_note(self, note: Note, expected_hash: str | None = None) -> None:
        """Persist Note entity with optional optimistic concurrency hash check."""
        ...

    def note_exists(self, cid_or_path: str) -> bool:
        """Check whether a note exists at the given CID or relative path."""
        ...

    def list_note_cids(self) -> Sequence[str]:
        """List all note CIDs tracked in the storage layer."""
        ...

    def list_raw_sources(self) -> Sequence[str]:
        """List all raw source paths tracked in the storage layer."""
        ...

    def raw_source_exists(self, rel_path: str) -> bool:
        """Check if a raw source file exists on the storage layer."""
        ...


@runtime_checkable
class IndexCachePort(Protocol):
    """Secondary port for searching, caching, and epistemic health reporting."""

    def scan(self, force: bool = False) -> tuple[int, int, int, int]:
        """Incrementally synchronize the index with storage."""
        ...

    def search(
        self,
        query: str,
        type_: str | None = None,
        status: str | None = None,
        trust: str | None = None,
        tag: str | None = None,
        domain: str | None = None,
        limit: int = 20,
    ) -> list[Any]:
        """Execute epistemic search with trust and status boosting."""
        ...

    def get_status(self) -> dict[str, Any]:
        """Return aggregate epistemic health metrics."""
        ...

    def get_connection(self) -> Any:
        """Return underlying database connection."""
        ...

    def close(self) -> None:
        """Release any underlying database connections or locks."""
        ...


@runtime_checkable
class LedgerPort(Protocol):
    """Secondary port for recording immutable activity entries."""

    def append(self, message: str, actor: str | None = None) -> None:
        """Append an entry to the activity ledger."""
        ...

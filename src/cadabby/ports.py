"""Abstract Ports (Driven/Secondary interfaces) for Cadabby.

Defines the contracts between the domain/application core and external infrastructure
(filesystem, SQLite, version control, logging) using standard-library typing.Protocol.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from cadabby.constants import DEFAULT_LIMIT
from cadabby.domain import DomainDefinition, Note
from cadabby.graph import LinkTargetIndex

if TYPE_CHECKING:
    # Annotation-only: search.py and queries.py belong to the cache adapter's
    # layer, and a port must not import its own adapter's modules (§9.1).
    from cadabby.queries import DebtRow
    from cadabby.search import SearchResult


@runtime_checkable
class IndexRow(Protocol):
    """One read-side row, addressed by column name and nothing else.

    These rows were typed `Mapping[str, Any]`, which the only implementation
    does not satisfy: the disk cache returns `sqlite3.Row`, and that has no
    `.get()` and no `.items()`. The mismatch survived because
    `isinstance(cache, IndexCachePort)` against a `runtime_checkable` protocol
    compares method *names* and ignores signatures, so `test_adapters` passed
    while the declared contract promised an interface callers would crash on.

    `__getitem__` is the whole promise because it is the whole usage; widen it
    only alongside the adapters that must then provide it.
    """

    def __getitem__(self, key: str, /) -> Any: ...


@runtime_checkable
class NoteStoragePort(Protocol):
    """Secondary port for persisting and retrieving notes."""

    def get_note(self, cid_or_path: str) -> Note | None:
        """Retrieve Note entity by CID or vault-relative path. Returns None if not found."""
        ...

    def save_note(self, note: Note, expected_hash: str | None = None) -> None:
        """Persist Note entity with optional optimistic concurrency hash check."""
        ...

    def note_exists(self, cid_or_path: str) -> bool:
        """Check whether a note exists at the given CID or relative path."""
        ...

    def delete_note(self, cid_or_path: str, expected_hash: str | None = None, *, prune_empty_dirs: bool = False) -> None:
        """Remove a note; VaultConflictError if its bytes changed since `expected_hash`.

        `prune_empty_dirs` also removes the folders the removal left empty, up
        to (not including) the vault root -- for undoing a write that made them.
        """
        ...

    def read_raw(self, cid_or_path: str) -> str | None:
        """The note's text without parsing it, or None if absent: for notes whose frontmatter does not parse."""
        ...

    def list_note_cids(self) -> Sequence[str]:
        """List all note CIDs tracked in the storage layer."""
        ...

    def list_source_paths(self) -> Sequence[str]:
        """Vault-relative paths of every file under raw/ that is evidence (§2.4), sorted."""
        ...

    def read_source(self, rel_path: str, max_bytes: int | None = None) -> str | None:
        """Text of the raw/ file at exactly `rel_path`, or None (§2.4, §5.1 `vault_ground`).

        Never a note and never a file outside raw/, whatever `..` or symlink
        the path carries: the confinement is the adapter's, not the caller's.
        With `max_bytes`, at most that many bytes are read (raw dumps can be
        hundreds of megabytes).
        """
        ...


@runtime_checkable
class IndexCachePort(Protocol):
    """Secondary port for searching, caching, and epistemic health reporting."""

    def scan(self, force: bool = False, regenerate_index: bool = True) -> tuple[int, int, int, int]:
        """Incrementally synchronize the index with storage; regenerate index.md if anything changed (§4.3)."""
        ...

    def search(
        self,
        query: str = "",
        type_: str | None = None,
        status: str | None = None,
        trust: str | None = None,
        tag: str | None = None,
        domain: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> list[SearchResult]:
        """Execute epistemic search with trust and status boosting."""
        ...

    def get_status(self) -> dict[str, Any]:
        """Return aggregate epistemic health metrics."""
        ...

    def get_link_resolver(self) -> LinkTargetIndex:
        """Return a LinkTargetIndex over all valid non-raw note CIDs."""
        ...

    def get_note_neighborhood(
        self, cid: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Return (forward_links, backlinks, sources) for `cid`."""
        ...

    def get_source_citers(self, raw_rel_path: str) -> list[dict[str, Any]]:
        """Notes whose `sources:` resolve to `raw_rel_path`."""
        ...

    def get_note_graph(self, target_cid: str, *, limit: int | None = None) -> dict[str, Any] | None:
        """Compute 1-hop and 2-hop neighborhoods, co-citations, and bibliographic coupling (§5.1)."""
        ...

    def get_moc_overview(self, limit: int) -> dict[str, Any]:
        """Every MOC outside archive/ with its link counts, and the largest overlaps between MOCs (§5.1)."""
        ...

    def entry_point_rows(self) -> Sequence[IndexRow]:
        """Every MOC outside archive/ (cid, stem, rel_path, layer, description), wiki MOCs first (§2.5)."""
        ...

    def unfiled_note_rows(self) -> Sequence[IndexRow]:
        """Wiki notes no MOC reaches by forward link at any depth, sorted by stem (§2.5)."""
        ...

    def unprocessed_raw_paths(self) -> Sequence[str]:
        """Raw files no note's `sources:` resolves to, sorted (§2.5, §4.5)."""
        ...

    def verification_debt_rows(self) -> Sequence[DebtRow]:
        """Active stale-verified notes, sorted by CID, each split by who owes it (§3.4, §4.5)."""
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


@runtime_checkable
class VaultLayoutPort(Protocol):
    """Secondary port for vault layout, domain manifests, templates, and raw-text rules (§2.2, §7.5, §9.1)."""

    @property
    def raw_text_extensions(self) -> Sequence[str]:
        """File extensions under `raw/` that the engine reads as UTF-8 text (§2.4, §2.6)."""
        ...

    def note_path_violation(self, rel_path: str, *, for_write: bool = False) -> str | None:
        """Why `rel_path` cannot hold a note in this vault, or None if it can (§2.1, §2.2, §7.5)."""
        ...

    def discover_domains(self) -> dict[str, DomainDefinition]:
        """All cognitive domains in the vault and their parsed `AGENTS.md` contracts (§2.2)."""
        ...

    def read_template_body(self, name: str) -> str:
        """Body of a named Obsidian template with its frontmatter stripped (§7.5)."""
        ...

    def has_folder(self, rel_path: str) -> bool:
        """Whether `rel_path` is an existing directory in the vault (§5.1 `vault_move_note`)."""
        ...

    def list_folder_files(self, rel_prefix: str) -> Sequence[str]:
        """Vault-relative paths of visible files under `rel_prefix/` (§5.1 `vault_move_note`)."""
        ...

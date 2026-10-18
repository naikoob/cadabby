"""SQLite ephemeral cache, FTS5 BM25 search, incremental sync, and link graph storage.

Conforms strictly to Cadabby Technical Specification §4.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import posixpath
import sqlite3
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import cadabby.indexer as _indexer
from cadabby.constants import (
    DIR_ARCHIVE,
    DIR_RAW,
    SCHEMA_VERSION,
    TYPE_MOC,
)
from cadabby.frontmatter import FrontmatterParseError, parse_frontmatter
from cadabby.fsutil import compute_file_sha256
from cadabby.graph import (
    LINK_KIND_MARKDOWN,
    LinkTargetIndex,
    extract_markdown_note_links,
    extract_wikilinks,
    resolve_markdown_target,
)
from cadabby.okf import compute_body_hash, derive_trust_tier, is_actor_human, latest_stale_actor, utc_now_iso
from cadabby.search import (
    SearchResult,
    _fts_table,
    execute_search,
    or_fallback_fts5_query,
    sanitize_fts5_query,
)
from cadabby.vault import Vault, cid_to_path, note_path_violation, path_to_cid, path_to_layer, path_to_stem


def _can_be_note(target_cid: str | None, template_rel: str | None) -> bool:
    """Whether a Markdown link target sits where a note could live (§4.4).

    Root files (`STYLE.md`), reserved and dot-folders, manifests and the
    template folder are links but never to notes: neither edges nor dead.
    """
    return target_cid is not None and note_path_violation(cid_to_path(target_cid), template_rel) is None


def entry_point_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every wiki MOC (stem, rel_path, description), sorted by stem (§2.5)."""
    return conn.execute(
        """
        SELECT stem, rel_path, description FROM notes
        WHERE layer = 'wiki' AND type = ? AND parse_error IS NULL
        ORDER BY stem ASC;
        """,
        (TYPE_MOC,),
    ).fetchall()


def unprocessed_raw_paths(conn: sqlite3.Connection) -> list[str]:
    """Raw files no note's `sources:` resolves to, sorted (§2.5, §4.5).

    The one definition of "unprocessed": `get_status` counts it and the
    index.md raw queue lists it, so the two cannot disagree.
    """
    return [
        r[0]
        for r in conn.execute(
            """
            SELECT r.rel_path FROM notes r
            WHERE r.layer = 'raw'
              AND NOT EXISTS (SELECT 1 FROM sources s WHERE s.resolved_path = r.rel_path)
            ORDER BY r.rel_path;
            """
        )
    ]


def unfiled_note_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Wiki notes no MOC reaches by forward link at any depth, sorted by stem (§2.5).

    The one definition of "unfiled": the index.md section and the
    `vault_status` queue both read it. Reachability is transitive and may
    travel through any layer; only active wiki notes are reported, because
    MOCs are a `wiki/` construct and filing a retired note is not work.
    """
    return conn.execute(
        """
        WITH RECURSIVE filed(cid) AS (
            SELECT cid FROM notes
            WHERE layer = 'wiki' AND type = ? AND parse_error IS NULL
          UNION
            SELECT l.target_cid FROM links l
            JOIN filed f ON l.source_cid = f.cid
            WHERE l.target_cid IS NOT NULL
        )
        SELECT cid, stem, rel_path, description FROM notes
        WHERE layer = 'wiki'
          AND parse_error IS NULL
          AND COALESCE(status, 'active') NOT IN ('deprecated', 'abandoned')
          AND cid NOT IN (SELECT cid FROM filed)
        ORDER BY stem ASC;
        """,
        (TYPE_MOC,),
    ).fetchall()


@dataclass(frozen=True, slots=True)
class DebtRow:
    """One note of actionable verification debt, and who owes its re-review (§4.5)."""

    cid: str
    human_owed: bool


def verification_debt_rows(conn: sqlite3.Connection) -> list[DebtRow]:
    """Active stale-verified notes, sorted by CID, each split by who owes it (§3.4, §4.5).

    Retired notes (`archive/`, `deprecated`, `abandoned`) are excluded: the
    list is a worklist. A note is human-owed when its latest stale attestation
    is `human:*`, the case in which an agent must not verify (§3.5).
    """
    rows = conn.execute(
        """
        SELECT cid, frontmatter_json, body_hash FROM notes
        WHERE trust_tier = 'stale-verified'
          AND parse_error IS NULL
          AND layer != ?
          AND COALESCE(status, 'active') NOT IN ('deprecated', 'abandoned')
        ORDER BY cid ASC;
        """,
        (DIR_ARCHIVE,),
    ).fetchall()
    debt: list[DebtRow] = []
    for r in rows:
        try:
            fm = json.loads(r["frontmatter_json"] or "{}")
        except json.JSONDecodeError:
            fm = {}
        actor = latest_stale_actor(fm.get("verified") if isinstance(fm, dict) else None, r["body_hash"])
        debt.append(DebtRow(cid=r["cid"], human_owed=actor is not None and is_actor_human(actor)))
    return debt


def raw_citers(conn: sqlite3.Connection, raw_rel_path: str) -> list[dict[str, Any]]:
    """Notes whose `sources:` resolve to `raw_rel_path`, sorted by CID (§4.3).

    The same relation that makes a source "processed", read in reverse.
    """
    return [
        {"cid": r["cid"], "title": r["title"], "type": r["type"], "trust_tier": r["trust_tier"]}
        for r in conn.execute(
            """
            SELECT DISTINCT n.cid, n.title, n.type, n.trust_tier
            FROM sources s JOIN notes n ON n.cid = s.source_cid
            WHERE s.resolved_path = ?
            ORDER BY n.cid ASC;
            """,
            (raw_rel_path,),
        )
    ]


def moc_overview(conn: sqlite3.Connection, limit: int) -> dict[str, Any]:
    """Every wiki MOC's link counts and the largest overlaps between MOCs (§5.1 `vault_graph`).

    `outgoing` counts distinct resolved note targets, `incoming` distinct
    linking notes. An overlap's `ratio` is shared targets over the smaller
    MOC's target count, so a MOC wholly contained in another reads 1.0
    whatever the sizes. Both lists are capped at `limit`, with totals.
    """
    mocs = conn.execute(
        """
        SELECT cid, title, trust_tier FROM notes
        WHERE layer = 'wiki' AND type = ? AND parse_error IS NULL
        ORDER BY cid ASC;
        """,
        (TYPE_MOC,),
    ).fetchall()
    targets: dict[str, set[str]] = {m["cid"]: set() for m in mocs}
    incoming: dict[str, int] = {m["cid"]: 0 for m in mocs}
    for r in conn.execute("SELECT DISTINCT source_cid, target_cid FROM links WHERE target_cid IS NOT NULL;"):
        if r["source_cid"] in targets and r["target_cid"] != r["source_cid"]:
            targets[r["source_cid"]].add(r["target_cid"])
        if r["target_cid"] in incoming and r["source_cid"] != r["target_cid"]:
            incoming[r["target_cid"]] += 1

    entries = sorted(
        (
            {
                "cid": m["cid"],
                "title": m["title"],
                "trust_tier": m["trust_tier"],
                "outgoing": len(targets[m["cid"]]),
                "incoming": incoming[m["cid"]],
            }
            for m in mocs
        ),
        key=lambda e: (-e["outgoing"], e["cid"]),
    )
    overlaps: list[dict[str, Any]] = []
    cids = [m["cid"] for m in mocs]
    for i, a in enumerate(cids):
        for b in cids[i + 1 :]:
            shared = len(targets[a] & targets[b])
            if shared:
                smaller = min(len(targets[a]), len(targets[b]))
                overlaps.append({"a": a, "b": b, "shared": shared, "ratio": round(shared / smaller, 3)})
    overlaps.sort(key=lambda o: (-o["ratio"], -o["shared"], o["a"], o["b"]))
    return {
        "mocs": entries[:limit],
        "mocs_total": len(entries),
        "overlaps": overlaps[:limit],
        "overlaps_total": len(overlaps),
    }


def get_note_graph(conn: sqlite3.Connection, target_cid: str, *, limit: int | None = None) -> dict[str, Any] | None:
    """Compute 1-hop and 2-hop neighborhoods, co-citations, and bibliographic coupling (§5.1, §5.3).

    Every entry carries its neighbor's title, type, status and trust tier
    (None when the link does not resolve), so a caller can choose what to read
    next without grounding each candidate. With `limit`, each list is capped
    and its untruncated length reported as `<list>_total`.
    """
    # 1. Check note existence
    cur = conn.execute(
        "SELECT cid, title, type, status, trust_tier, rel_path FROM notes WHERE cid = ? AND parse_error IS NULL;",
        (target_cid,),
    )
    row = cur.fetchone()
    if not row:
        return None

    note_info = {
        "cid": row["cid"],
        "title": row["title"],
        "type": row["type"],
        "status": row["status"],
        "trust_tier": row["trust_tier"],
        "rel_path": row["rel_path"],
    }

    # 2. 1-hop forward links
    cur = conn.execute(
        "SELECT target_raw, target_cid, anchor, kind FROM links WHERE source_cid = ? ORDER BY target_cid ASC;",
        (target_cid,),
    )
    forward_links = [
        {"target_raw": r["target_raw"], "target_cid": r["target_cid"], "anchor": r["anchor"], "kind": r["kind"]}
        for r in cur.fetchall()
    ]
    forward_cids = {r["target_cid"] for r in forward_links if r["target_cid"]}

    # 3. 1-hop backlinks
    cur = conn.execute(
        "SELECT source_cid, target_raw, anchor, kind FROM links WHERE target_cid = ? ORDER BY source_cid ASC;",
        (target_cid,),
    )
    backlinks = [
        {"source_cid": r["source_cid"], "target_raw": r["target_raw"], "anchor": r["anchor"], "kind": r["kind"]}
        for r in cur.fetchall()
    ]
    backlink_cids = {r["source_cid"] for r in backlinks if r["source_cid"]}

    # 4. 2-hop forward links (targets of 1-hop forward targets)
    two_hop_forward: list[dict[str, Any]] = []
    if forward_cids:
        forward_list = sorted(forward_cids)
        placeholders = ",".join("?" for _ in forward_list)
        cur = conn.execute(
            f"""
            SELECT DISTINCT source_cid, target_cid
            FROM links
            WHERE source_cid IN ({placeholders})
              AND target_cid IS NOT NULL
              AND target_cid != ?
              AND target_cid NOT IN ({placeholders})
            ORDER BY target_cid ASC;
            """,
            (*forward_list, target_cid, *forward_list),
        )
        two_hop_forward = [{"via": r["source_cid"], "target_cid": r["target_cid"]} for r in cur.fetchall()]

    # 5. 2-hop backlinks (sources linking to 1-hop backlinks)
    two_hop_backlinks: list[dict[str, Any]] = []
    if backlink_cids:
        backlink_list = sorted(backlink_cids)
        placeholders = ",".join("?" for _ in backlink_list)
        cur = conn.execute(
            f"""
            SELECT DISTINCT source_cid, target_cid
            FROM links
            WHERE target_cid IN ({placeholders})
              AND source_cid != ?
              AND source_cid NOT IN ({placeholders})
            ORDER BY source_cid ASC;
            """,
            (*backlink_list, target_cid, *backlink_list),
        )
        two_hop_backlinks = [{"source_cid": r["source_cid"], "via": r["target_cid"]} for r in cur.fetchall()]

    # 6. Co-citations (notes cited alongside target_cid by common sources)
    cur = conn.execute(
        """
        SELECT l2.target_cid, COUNT(DISTINCT l1.source_cid) AS co_citations
        FROM links l1
        JOIN links l2 ON l1.source_cid = l2.source_cid
        WHERE l1.target_cid = ?
          AND l2.target_cid != ?
          AND l2.target_cid IS NOT NULL
        GROUP BY l2.target_cid
        ORDER BY co_citations DESC, l2.target_cid ASC;
        """,
        (target_cid, target_cid),
    )
    co_citations = [{"cid": r["target_cid"], "count": r["co_citations"]} for r in cur.fetchall()]

    # 7. Bibliographic coupling (notes that cite common targets as target_cid)
    cur = conn.execute(
        """
        SELECT l2.source_cid, COUNT(DISTINCT l1.target_cid) AS shared_targets
        FROM links l1
        JOIN links l2 ON l1.target_cid = l2.target_cid
        WHERE l1.source_cid = ?
          AND l2.source_cid != ?
          AND l1.target_cid IS NOT NULL
        GROUP BY l2.source_cid
        ORDER BY shared_targets DESC, l2.source_cid ASC;
        """,
        (target_cid, target_cid),
    )
    bibliographic_coupling = [{"cid": r["source_cid"], "count": r["shared_targets"]} for r in cur.fetchall()]

    # Which key names the neighbor in each list's entries.
    lists: dict[str, tuple[list[dict[str, Any]], str]] = {
        "forward_links": (forward_links, "target_cid"),
        "backlinks": (backlinks, "source_cid"),
        "two_hop_forward": (two_hop_forward, "target_cid"),
        "two_hop_backlinks": (two_hop_backlinks, "source_cid"),
        "co_citations": (co_citations, "cid"),
        "bibliographic_coupling": (bibliographic_coupling, "cid"),
    }
    result: dict[str, Any] = {"note": note_info}
    for name, (entries, key) in lists.items():
        kept = entries if limit is None else entries[:limit]
        result[name] = kept
        if limit is not None:
            result[f"{name}_total"] = len(entries)
    _attach_neighbor_metadata(conn, [(e, key) for name, (_, key) in lists.items() for e in result[name]])
    return result


def _attach_neighbor_metadata(conn: sqlite3.Connection, entries: list[tuple[dict[str, Any], str]]) -> None:
    """Add title, type, status and trust_tier of the note each entry's `key` names, in one query."""
    cids = sorted({e[key] for e, key in entries if e.get(key)})
    meta: dict[str, sqlite3.Row] = {}
    for start in range(0, len(cids), 500):  # stay under SQLite's bound-parameter limit
        chunk = cids[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        for r in conn.execute(
            f"SELECT cid, title, type, status, trust_tier FROM notes WHERE parse_error IS NULL AND cid IN ({placeholders});",
            chunk,
        ):
            meta[r["cid"]] = r
    for entry, key in entries:
        row = meta.get(entry.get(key) or "")
        for field in ("title", "type", "status", "trust_tier"):
            entry[field] = row[field] if row is not None else None


@dataclass(frozen=True, slots=True, kw_only=True)
class NoteRecord:
    """One row's worth of `notes` content, as handed to `VaultCache.upsert_note()`.

    The identity columns (`cid`, `stem`, `layer`) and `indexed_at` are derived
    inside upsert_note, so a caller cannot hand in a CID that disagrees with its
    path, or a layer that would index the row into the wrong FTS table.
    """

    rel_path: str
    mtime: float
    file_size: int
    file_hash: str | None
    body: str
    title: str | None = None
    description: str | None = None
    type_: str | None = None
    status: str | None = None
    trust_tier: str | None = None
    body_hash: str | None = None
    frontmatter_json: str | None = None
    parse_error: str | None = None
    tags: list[str] | None = None


# Every `notes` column upsert_note() writes besides `rel_path`, the row key.
_NOTE_COLUMNS = (
    "cid",
    "stem",
    "layer",
    "mtime",
    "file_size",
    "file_hash",
    "indexed_at",
    "title",
    "description",
    "type",
    "status",
    "trust_tier",
    "body_hash",
    "frontmatter_json",
    "parse_error",
    "tags",
    "body",
)
_UPDATE_NOTE_SQL = f"UPDATE notes SET {', '.join(f'{c} = ?' for c in _NOTE_COLUMNS)} WHERE id = ?;"
_INSERT_NOTE_SQL = f"INSERT INTO notes (rel_path, {', '.join(_NOTE_COLUMNS)}) VALUES (?{', ?' * len(_NOTE_COLUMNS)});"


def _is_unchanged(cached: sqlite3.Row, st: os.stat_result, f_hash: str | None, integrity_mode: str) -> bool:
    """Whether a file still matches its cached row under the vault's integrity mode."""
    if integrity_mode == "hash":
        return cached["file_hash"] == f_hash
    return abs(cached["mtime"] - st.st_mtime) <= 1e-4 and cached["file_size"] == st.st_size


_RAW_EXTS_META_KEY = "raw_text_extensions"


def _raw_exts_fingerprint(exts: tuple[str, ...]) -> str:
    """Canonical form of the extension list the raw index was built with (§4.3)."""
    return json.dumps(sorted(set(exts)))


def is_raw_text_path(rel_path: str, raw_text_exts: tuple[str, ...]) -> bool:
    """Whether a raw file's body is indexed under the configured extension list (§2.4)."""
    return bool(raw_text_exts) and rel_path.lower().endswith(raw_text_exts)


@functools.cache
def _fts5_available() -> bool:
    """Whether this interpreter's SQLite has FTS5, asked once of a throwaway database.

    The probe used to CREATE and DROP a table in the user's cache.db on every
    open: a write, so every command -- reads included -- needed the write lock,
    and a lock held by another process surfaced as "FTS5 is not available".
    """
    with contextlib.closing(sqlite3.connect(":memory:")) as probe:
        try:
            probe.execute("CREATE VIRTUAL TABLE _fts5_probe USING fts5(x);")
        except sqlite3.OperationalError:
            return False
    return True


def check_fts5_capability(conn: sqlite3.Connection | None = None) -> None:
    """Verify that SQLite was compiled with FTS5 virtual table support.

    `conn` is accepted for compatibility and ignored: the answer is a property
    of the interpreter's SQLite, not of any one database.
    """
    if not _fts5_available():
        raise RuntimeError(
            f"SQLite FTS5 extension is not available in Python {sys.version} "
            f"(sqlite_version={sqlite3.sqlite_version}). "
            "Please use a Python interpreter compiled with SQLite FTS5 support."
        )


def reset_cache_files(db_path: Path) -> None:
    """Delete the cache database and its WAL/SHM sidecars (§4.2, `sync --rebuild`).

    The connection must be closed first. Deleting only `cache.db` would leave
    a `-wal` that SQLite replays into the fresh file on the next open.
    """
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)


def _schema_state(conn: sqlite3.Connection) -> str:
    """`"empty"`, `"current"` (recorded at SCHEMA_VERSION) or `"stale"` (anything else).

    Compared as text so a corrupt, non-integer version reads as stale instead
    of raising from `int()`. Only an empty database needs its tables created:
    re-running the DDL and rewriting `schema_version` on every open made every
    command, reads included, take the write lock.
    """
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table';")}
    if not tables:
        return "empty"
    if "cache_meta" not in tables:
        return "stale"
    row = conn.execute("SELECT value FROM cache_meta WHERE key = 'schema_version';").fetchone()
    return "current" if row is not None and str(row[0]) == str(SCHEMA_VERSION) else "stale"


def _schema_is_current(conn: sqlite3.Connection) -> bool:
    """True for an empty database or one recorded at SCHEMA_VERSION."""
    return _schema_state(conn) != "stale"


class VaultCache:
    """Manages the disposable SQLite cache at .cadabby/cache.db."""

    def __init__(self, vault: Vault) -> None:
        self.vault = vault
        # A `.cadabby -> elsewhere` symlink would put every cache write outside the vault (§8).
        self.db_path = vault.contained_path(vault.cache_db_path)
        self._conn: sqlite3.Connection | None = None

    def get_connection(self) -> sqlite3.Connection:
        """Open or return existing SQLite connection with WAL and busy timeout.

        A cache at any other schema version -- or one whose version cannot be
        read -- is discarded and rebuilt from the vault (§4.2: the cache is
        disposable). Rebuilding in place by DROPping known tables would miss
        any table a newer schema added.
        """
        if self._conn is None:
            conn: sqlite3.Connection | None = None
            try:
                conn = self._open()
                state = _schema_state(conn)
            except sqlite3.DatabaseError:
                # Not a database at all, or a corrupt page: the cache is
                # disposable, so a damaged one is rebuilt rather than failing
                # every command as INTERNAL (§4.2). A second failure is real.
                # Closed first: deleting a file under an open handle leaks it,
                # and fails outright on Windows.
                if conn is not None:
                    conn.close()
                state = "stale"
                conn = None
            if state == "stale":
                if conn is not None:
                    conn.close()
                reset_cache_files(self.db_path)
                conn = self._open()
                state = "empty"
            assert conn is not None
            self._conn = conn
            if state == "empty":
                self._create_tables(conn)
        return self._conn

    def _open(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            check_fts5_capability(conn)
        except BaseException:
            conn.close()
            raise
        return conn

    def _create_tables(self, conn: sqlite3.Connection) -> None:
        """Execute normative schema definitions conforming to §4.2."""
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS notes (
                id           INTEGER PRIMARY KEY,
                cid          TEXT NOT NULL UNIQUE,
                rel_path     TEXT NOT NULL UNIQUE,
                stem         TEXT NOT NULL,
                layer        TEXT NOT NULL,
                mtime        REAL NOT NULL,
                file_size    INTEGER NOT NULL,
                file_hash    TEXT,
                indexed_at   TEXT NOT NULL,
                title        TEXT,
                description  TEXT,
                type         TEXT,
                status       TEXT,
                trust_tier   TEXT,
                body_hash    TEXT,
                frontmatter_json TEXT,
                parse_error  TEXT,
                tags         TEXT NOT NULL DEFAULT '',
                body         TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_notes_layer_type ON notes(layer, type);
            CREATE INDEX IF NOT EXISTS idx_notes_trust ON notes(trust_tier);

            CREATE TABLE IF NOT EXISTS links (
                id          INTEGER PRIMARY KEY,
                source_cid  TEXT NOT NULL,
                target_raw  TEXT NOT NULL,
                target_cid  TEXT,
                alias       TEXT,
                anchor      TEXT,
                occurrences INTEGER NOT NULL DEFAULT 1,
                kind        TEXT NOT NULL DEFAULT 'wiki'
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_links_edge ON links(source_cid, kind, target_raw);
            CREATE INDEX IF NOT EXISTS idx_links_target ON links(target_cid);
            CREATE INDEX IF NOT EXISTS idx_links_source ON links(source_cid);

            CREATE TABLE IF NOT EXISTS sources (
                source_cid  TEXT NOT NULL,
                raw_path    TEXT NOT NULL,
                resolved    INTEGER NOT NULL,
                resolved_path TEXT,
                PRIMARY KEY (source_cid, raw_path)
            );
            CREATE INDEX IF NOT EXISTS idx_sources_raw ON sources(raw_path);
            CREATE INDEX IF NOT EXISTS idx_sources_resolved ON sources(resolved_path);

            -- Each FTS table's content is exactly the rows it indexes, mirroring
            -- the insert gate in upsert_note(): the layer picks the table, and
            -- only parsed rows are indexed. That is what lets integrity-check
            -- compare index against content (rank = 1); pointed at all of
            -- `notes`, every raw or unparseable row reads as corruption of the
            -- other index, so only the structural check was usable -- and it
            -- passes on a stale index (§4.2).
            CREATE VIEW IF NOT EXISTS notes_fts_content AS
                SELECT id, title, description, body, tags FROM notes
                WHERE layer != 'raw' AND parse_error IS NULL;
            CREATE VIEW IF NOT EXISTS raw_fts_content AS
                SELECT id, title, description, body, tags FROM notes
                WHERE layer = 'raw' AND parse_error IS NULL;

            CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
                title, description, body, tags,
                content = 'notes_fts_content',
                content_rowid = 'id',
                tokenize = 'porter unicode61'
            );

            -- Raw sources index on a separate table, deliberately. BM25 scores a
            -- document against its corpus, and a shared index makes raw/ part of
            -- the corpus for curated notes: thirty transcripts repeating a term
            -- drive its IDF to zero, so a wiki note that scored 1.29 on that term
            -- scores 0.00 instead. The trust and status multipliers (§4.4) then
            -- multiply ~0 and epistemic ranking stops working altogether. Keeping
            -- the corpora apart is what makes each set of statistics describe the
            -- documents it actually ranks.
            CREATE VIRTUAL TABLE IF NOT EXISTS raw_fts USING fts5(
                title, description, body, tags,
                content = 'raw_fts_content',
                content_rowid = 'id',
                tokenize = 'porter unicode61'
            );

            CREATE TABLE IF NOT EXISTS cache_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta(key, value) VALUES('schema_version', ?);",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()

    @staticmethod
    def _retract_fts(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
        """Retract a stored row's terms from the FTS5 index of its own corpus (§4.2).

        This must mirror the insert gate in upsert_note() exactly: retracting a
        row that was never inserted, or retracting it from the wrong table,
        corrupts an external-content index silently rather than raising. The
        table and every column are read from the stored row, never from the
        incoming one, so the whole retraction is sourced from old state. The
        table cannot currently differ from the new row's -- layer is a pure
        function of rel_path and the row is keyed by rel_path, so a layer change
        arrives as a delete plus an insert.
        """
        if row["parse_error"] is not None:
            return
        fts = _fts_table(row["layer"])
        conn.execute(
            f"INSERT INTO {fts}({fts}, rowid, title, description, body, tags) "
            "VALUES('delete', ?, ?, ?, ?, ?);",
            (row["id"], row["title"] or "", row["description"] or "", row["body"] or "", row["tags"] or ""),
        )

    def upsert_note(self, record: NoteRecord) -> int:
        """Upsert a note into 'notes' and maintain its FTS5 index with explicit retraction.

        With delete_note(), one of the only two code paths that write 'notes'
        or its FTS5 tables (§4.2); both retract old terms before changing a row.
        """
        conn = self.get_connection()
        # Lowercased here, not only by the agent writer: a hand-written file may
        # still say `ML` (§3.1 makes casing a warning), and the --tag predicate
        # lowercases its probe on the assumption that stored tags are canonical.
        tags_str = " ".join(str(t).lower() for t in (record.tags or []))
        layer = path_to_layer(record.rel_path)
        # Positionally matched to _NOTE_COLUMNS.
        values = (
            path_to_cid(record.rel_path),
            path_to_stem(record.rel_path),
            layer,
            record.mtime,
            record.file_size,
            record.file_hash,
            utc_now_iso(),
            record.title,
            record.description,
            record.type_,
            record.status,
            record.trust_tier,
            record.body_hash,
            record.frontmatter_json,
            record.parse_error,
            tags_str,
            record.body,
        )

        existing = conn.execute(
            "SELECT id, layer, parse_error, title, description, body, tags FROM notes WHERE rel_path = ?;",
            (record.rel_path,),
        ).fetchone()

        if existing is not None:
            row_id = existing["id"]
            # 1. Retract the old terms using the OLD values, then update in place
            # so the rowid the FTS5 tables key on is preserved.
            self._retract_fts(conn, existing)
            conn.execute(_UPDATE_NOTE_SQL, (*values, row_id))
        else:
            cur = conn.execute(_INSERT_NOTE_SQL, (record.rel_path, *values))
            row_id = cur.lastrowid
            assert row_id is not None

        # 2. Index everything that parsed, into the index for its corpus. Raw
        # sources are indexed at all so that the full text of a collected source
        # is reachable (§2.4) -- storing the body and never indexing it made
        # `raw/` a write-only surface, which reads to an agent as "this is not in
        # the vault". Non-text raw files carry an empty body and are indexed by
        # title alone, which is what makes `raw/paper.pdf` findable by name.
        if record.parse_error is None:
            conn.execute(
                f"INSERT INTO {_fts_table(layer)}(rowid, title, description, body, tags) "
                "VALUES (?, ?, ?, ?, ?);",
                (row_id, record.title or "", record.description or "", record.body, tags_str),
            )

        return row_id

    def delete_note(self, rel_path: str) -> bool:
        """Delete a note and retract all FTS5 terms and graph edges.

        This is the ONLY code path that deletes notes.
        """
        conn = self.get_connection()
        row = conn.execute(
            "SELECT id, cid, layer, parse_error, title, description, body, tags FROM notes WHERE rel_path = ?;",
            (rel_path,),
        ).fetchone()
        if row is None:
            return False

        self._retract_fts(conn, row)
        self._clear_edges(row["cid"])
        conn.execute("DELETE FROM notes WHERE id = ?;", (row["id"],))
        return True

    def scan(self, force: bool = False, regenerate_index: bool = True) -> tuple[int, int, int, int]:
        """Perform incremental scan over vault files.

        A scan that changed anything regenerates index.md before returning
        (§4.3). This lives here rather than at each call site because
        "whichever entry point triggered it" is the actual requirement,
        and a list of call sites is a list someone eventually forgets to
        extend. regenerate_index=False exists for the index generator's own
        scan, which would otherwise recurse.

        Returns:
            (inserted, updated, deleted, total)
        """
        conn = self.get_connection()
        integrity_mode = self.vault.config["integrity"]
        raw_text_exts = tuple(self.vault.config["raw_text_extensions"])
        # Which raw files have indexed text depends on config, not only on the
        # file, so a list change must reach files whose stat never moved (§4.3).
        raw_fp = _raw_exts_fingerprint(raw_text_exts)
        stored_fp = conn.execute("SELECT value FROM cache_meta WHERE key = ?;", (_RAW_EXTS_META_KEY,)).fetchone()
        raw_exts_changed = stored_fp is None or stored_fp[0] != raw_fp
        # Markdown links into the declared template folder are not note links (§7.5).
        template_rel = self.vault.template_rel()

        disk_files = self._discover_files()

        try:
            # 1. Deletion reconciliation: rows whose file is gone from disk.
            db_paths = {row["rel_path"] for row in conn.execute("SELECT rel_path FROM notes;")}
            missing_paths = db_paths - disk_files.keys()
            for missing_path in missing_paths:
                self.delete_note(missing_path)
            deleted_count = len(missing_paths)

            # 2. Upsert new or modified files.
            cached_meta = {
                row["rel_path"]: row
                for row in conn.execute("SELECT rel_path, mtime, file_size, file_hash FROM notes;")
            }
            inserted_count = 0
            updated_count = 0
            for rel_path, abs_path in disk_files.items():
                cached = cached_meta.get(rel_path)
                is_raw = path_to_layer(rel_path) == DIR_RAW
                try:
                    st = abs_path.stat()
                    f_hash = compute_file_sha256(abs_path) if integrity_mode == "hash" else None
                    stale_by_config = is_raw and raw_exts_changed
                    if cached is not None and not force and not stale_by_config and _is_unchanged(cached, st, f_hash, integrity_mode):
                        continue

                    if is_raw:
                        self._index_raw_file(rel_path, abs_path, st, f_hash, raw_text_exts)
                    else:
                        self._index_note_file(rel_path, abs_path, st, f_hash, template_rel)
                except FileNotFoundError:
                    # Deleted between discovery and here (an editor's save-by-rename,
                    # a concurrent move): it is simply gone, which is what step 1
                    # would have concluded a moment later (§4.3).
                    if cached is not None:
                        self.delete_note(rel_path)
                        deleted_count += 1
                    continue

                if cached is None:
                    inserted_count += 1
                else:
                    updated_count += 1

            # 3. Resolve link targets if vault structure changed or new/updated links exist
            if inserted_count or updated_count or deleted_count or force:
                self._resolve_edges()

            if raw_exts_changed:
                conn.execute(
                    "INSERT OR REPLACE INTO cache_meta(key, value) VALUES(?, ?);",
                    (_RAW_EXTS_META_KEY, raw_fp),
                )
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise

        total_count = conn.execute("SELECT COUNT(*) FROM notes;").fetchone()[0]

        # A stale queue is worse than no queue: it sends an agent to redo work
        # that is already done (§4.3). Binding this to the scan rather than to
        # the write path also covers the case no write path sees -- a human
        # dropping a file into raw/. The byte-comparison guard in
        # sync_vault_index keeps an unchanged report from churning the diff.
        if regenerate_index and (inserted_count or updated_count or deleted_count):
            _indexer.sync_vault_index(self.vault, cache=self)

        return inserted_count, updated_count, deleted_count, total_count

    def _discover_files(self) -> dict[str, Path]:
        """Every file the cache tracks: raw evidence plus the notes of every cognitive domain."""
        files = {self.vault.rel_path(p): p for p in self.vault.iter_raw_files()}
        files.update((self.vault.rel_path(p), p) for p, _, _ in self.vault.iter_domain_notes())
        return files

    def _index_note_file(
        self,
        rel_path: str,
        abs_path: Path,
        st: os.stat_result,
        f_hash: str | None,
        template_rel: str | None,
    ) -> None:
        """Index one note with its graph edges, or record why its frontmatter does not parse."""
        cid = path_to_cid(rel_path)
        # Bytes, not read_text(): text mode turns a lone `\r` into `\n`, so the
        # cache would parse (and give a trust tier to) frontmatter the storage
        # adapter and the parser refuse -- a `\r`-joined forged attestation (§3.2).
        content = abs_path.read_bytes().decode("utf-8", errors="replace")
        try:
            fm, body = parse_frontmatter(content)
        except FrontmatterParseError as e:
            # Record the error and keep the full content (lint re-parses it for
            # the line number), exclude the row from search, and clear stale edges.
            self.upsert_note(
                NoteRecord(
                    rel_path=rel_path,
                    mtime=st.st_mtime,
                    file_size=st.st_size,
                    file_hash=f_hash,
                    body=content,
                    parse_error=str(e),
                )
            )
            self._clear_edges(cid)
            return

        b_hash = compute_body_hash(body)
        tags = fm.get("tags")
        sources = fm.get("sources")

        def text(key: str) -> str | None:
            # The restricted YAML subset admits a list or mapping here, which
            # sqlite cannot bind. Store NULL and let gate 1 report it
            # (ENUM_INVALID) rather than crash every scan, and every command.
            value = fm.get(key)
            return value if isinstance(value, str) else None

        self.upsert_note(
            NoteRecord(
                rel_path=rel_path,
                mtime=st.st_mtime,
                file_size=st.st_size,
                file_hash=f_hash,
                body=body,
                title=text("title") or abs_path.stem,
                description=text("description"),
                type_=text("type"),
                status=text("status"),
                trust_tier=derive_trust_tier(fm.get("verified"), b_hash),
                body_hash=b_hash,
                frontmatter_json=json.dumps(fm),
                tags=tags if isinstance(tags, list) else None,
            )
        )
        self._replace_edges(cid, body, sources if isinstance(sources, list) else [], template_rel)

    def _index_raw_file(
        self,
        rel_path: str,
        abs_path: Path,
        st: os.stat_result,
        f_hash: str | None,
        raw_text_exts: tuple[str, ...],
    ) -> None:
        """Index one raw source; a non-text file gets an empty body and is found by title (§2.4)."""
        is_text = is_raw_text_path(rel_path, raw_text_exts)
        self.upsert_note(
            NoteRecord(
                rel_path=rel_path,
                mtime=st.st_mtime,
                file_size=st.st_size,
                file_hash=f_hash,
                body=abs_path.read_bytes().decode("utf-8", errors="replace") if is_text else "",
                title=abs_path.stem,
                type_="source",
                status="raw",
            )
        )

    def _clear_edges(self, cid: str) -> None:
        """Drop every outbound link and `sources:` row of a note."""
        conn = self.get_connection()
        conn.execute("DELETE FROM links WHERE source_cid = ?;", (cid,))
        conn.execute("DELETE FROM sources WHERE source_cid = ?;", (cid,))

    def _replace_edges(self, cid: str, body: str, sources: list[Any], template_rel: str | None) -> None:
        """Rewrite a note's outbound links and `sources:` rows from its current content.

        Links and sources are stored unresolved; _resolve_edges() fills them
        once every file touched by the scan is in place.
        """
        conn = self.get_connection()
        self._clear_edges(cid)

        # Wikilinks plus relative Markdown note links (§4.4)
        extracted = extract_wikilinks(body) + [
            link
            for link in extract_markdown_note_links(body)
            if _can_be_note(resolve_markdown_target(cid, link.target_raw), template_rel)
        ]
        if extracted:
            conn.executemany(
                """
                INSERT OR REPLACE INTO links(source_cid, target_raw, target_cid, alias, anchor, occurrences, kind)
                VALUES (?, ?, NULL, ?, ?, ?, ?);
                """,
                [(cid, link.target_raw, link.alias, link.anchor, link.occurrences, link.kind) for link in extracted],
            )

        source_tuples = [(cid, src) for src in sources if isinstance(src, str)]
        if source_tuples:
            conn.executemany(
                "INSERT OR REPLACE INTO sources(source_cid, raw_path, resolved, resolved_path) VALUES (?, ?, 0, NULL);",
                source_tuples,
            )

    def _resolve_edges(self) -> None:
        """Re-resolve every link's `target_cid` and every citation against the cache (§4.3).

        Citations resolve against the indexed raw set, not a separate disk
        probe, and all of them on every changed scan: a raw file that arrives
        after the note citing it must clear SOURCE_MISSING without the note
        being edited, and `./raw/x` must count as citing `raw/x`.
        """
        conn = self.get_connection()
        raw_paths = {r["rel_path"] for r in conn.execute("SELECT rel_path FROM notes WHERE layer = 'raw';")}
        source_updates: list[tuple[int, str | None, str, str]] = []
        for row in conn.execute("SELECT source_cid, raw_path FROM sources;").fetchall():
            norm = posixpath.normpath(row["raw_path"].replace("\\", "/"))
            hit = norm if norm in raw_paths else None
            source_updates.append((1 if hit else 0, hit, row["source_cid"], row["raw_path"]))
        conn.executemany(
            "UPDATE sources SET resolved = ?, resolved_path = ? WHERE source_cid = ? AND raw_path = ?;",
            source_updates,
        )

        resolver = self.get_link_resolver()
        # A link whose target is a note with broken frontmatter stays unresolved
        # (gate 3 names the cause). It must not fall through to another note
        # with the same stem, which is why the resolver still includes it (§4.4).
        broken = self.unparseable_cids()
        link_updates: list[tuple[str | None, int]] = []
        for link_row in conn.execute("SELECT id, source_cid, target_raw, kind FROM links;").fetchall():
            target_raw = link_row["target_raw"]
            if link_row["kind"] == LINK_KIND_MARKDOWN:
                # Exact path from the citing note's folder; never a stem fallback (§4.4).
                hit = resolve_markdown_target(link_row["source_cid"], target_raw)
                hit = hit if hit in resolver.known_cids else None
            else:
                hit = resolver.resolve(target_raw.split("#")[0])
            link_updates.append((None if hit in broken else hit, link_row["id"]))
        conn.executemany("UPDATE links SET target_cid = ? WHERE id = ?;", link_updates)

    def get_link_resolver(self) -> LinkTargetIndex:
        """A LinkTargetIndex over every non-raw note, parseable or not (§4.4). A pure read: callers scan first (§4.3).

        A note whose frontmatter is broken still exists, so it keeps its name.
        Leaving it out let `[[Architecture]]` fall through to another note with
        that stem, silently, the moment someone mistyped YAML. Callers that must
        not land on such a note check `unparseable_cids()`.
        """
        conn = self.get_connection()
        rows = conn.execute("SELECT cid FROM notes WHERE layer != 'raw';")
        return LinkTargetIndex([row["cid"] for row in rows])

    def unparseable_cids(self) -> frozenset[str]:
        """CIDs of non-raw notes whose frontmatter does not parse (§3.2)."""
        conn = self.get_connection()
        rows = conn.execute("SELECT cid FROM notes WHERE layer != 'raw' AND parse_error IS NOT NULL;")
        return frozenset(row["cid"] for row in rows)

    def get_note_neighborhood(
        self, cid: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Return (forward_links, backlinks, sources) for `cid` with 1-hop neighbor metadata (§5.1)."""
        conn = self.get_connection()
        links = [
            {
                "target": r["target_raw"],
                "resolved_cid": r["target_cid"],
                "kind": r["kind"],
                "title": r["title"],
                "type": r["type"],
                "status": r["status"],
                "trust_tier": r["trust_tier"],
                "description": r["description"],
            }
            for r in conn.execute(
                """
                SELECT l.target_raw, l.target_cid, l.kind,
                       n.title, n.type, n.status, n.trust_tier, n.description
                FROM links l
                LEFT JOIN notes n ON n.cid = l.target_cid AND n.parse_error IS NULL
                WHERE l.source_cid = ?;
                """,
                (cid,),
            ).fetchall()
        ]
        backlinks = [
            {
                "source_cid": r["source_cid"],
                "target_raw": r["target_raw"],
                "kind": r["kind"],
                "title": r["title"],
                "type": r["type"],
                "status": r["status"],
                "trust_tier": r["trust_tier"],
                "description": r["description"],
            }
            for r in conn.execute(
                """
                SELECT l.source_cid, l.target_raw, l.kind,
                       n.title, n.type, n.status, n.trust_tier, n.description
                FROM links l
                LEFT JOIN notes n ON n.cid = l.source_cid AND n.parse_error IS NULL
                WHERE l.target_cid = ?;
                """,
                (cid,),
            ).fetchall()
        ]
        sources = [
            {"path": r["raw_path"], "exists": bool(r["resolved"])}
            for r in conn.execute(
                "SELECT raw_path, resolved FROM sources WHERE source_cid = ?;",
                (cid,),
            ).fetchall()
        ]
        return links, backlinks, sources

    def search(
        self,
        query: str = "",
        type_: str | None = None,
        status: str | None = None,
        trust: str | None = None,
        tag: str | None = None,
        domain: str | None = None,
        limit: int = 20,
    ) -> list[SearchResult]:
        """Perform BM25 or filter-only search across cognitive domain notes with epistemic rank boosting (§4.4)."""
        return execute_search(
            self.get_connection(),
            self.vault.config["ranking"],
            query=query,
            type_=type_,
            status=status,
            trust=trust,
            tag=tag,
            domain=domain,
            limit=limit,
        )

    def get_source_citers(self, raw_rel_path: str) -> list[dict[str, Any]]:
        """Notes whose `sources:` resolve to the raw/ file, with title, type and trust tier (§5.1)."""
        return raw_citers(self.get_connection(), raw_rel_path)

    def get_note_graph(self, target_cid: str, *, limit: int | None = None) -> dict[str, Any] | None:
        """Compute 1-hop and 2-hop neighborhoods, co-citations, and bibliographic coupling (§5.1, §5.3)."""
        return get_note_graph(self.get_connection(), target_cid, limit=limit)

    def get_moc_overview(self, limit: int) -> dict[str, Any]:
        """Every wiki MOC's link counts and the largest overlaps between MOCs (§5.1 `vault_graph`)."""
        return moc_overview(self.get_connection(), limit)

    def entry_point_rows(self) -> list[sqlite3.Row]:
        """Every wiki MOC (stem, rel_path, description), sorted by stem (§2.5)."""
        return entry_point_rows(self.get_connection())

    def unfiled_note_rows(self) -> list[sqlite3.Row]:
        """Wiki notes no MOC reaches by forward link at any depth, sorted by stem (§2.5)."""
        return unfiled_note_rows(self.get_connection())

    def unprocessed_raw_paths(self) -> list[str]:
        """Raw files no note's `sources:` resolves to, sorted (§2.5, §4.5)."""
        return unprocessed_raw_paths(self.get_connection())

    def verification_debt_rows(self) -> list[DebtRow]:
        """Active stale-verified notes, sorted by CID, each split by who owes it (§3.4, §4.5)."""
        return verification_debt_rows(self.get_connection())

    def lint_note_rows(self) -> list[sqlite3.Row]:
        """Every non-raw note row the scan indexed, ordered by path for deterministic lint findings (§6.3)."""
        return self.get_connection().execute(
            """
            SELECT cid, rel_path, layer, body, frontmatter_json, parse_error, body_hash, trust_tier
            FROM notes
            WHERE layer != 'raw'
            ORDER BY rel_path;
            """
        ).fetchall()

    def lint_link_rows(self) -> list[sqlite3.Row]:
        """Every extracted link edge for Gate 3 link/anchor validation (§6.3)."""
        return self.get_connection().execute(
            "SELECT source_cid, target_raw, target_cid, anchor, kind FROM links;"
        ).fetchall()

    def unresolved_source_rows(self) -> list[sqlite3.Row]:
        """Every `sources:` entry that did not resolve to an indexed raw/ file for Gate 4 (§6.3)."""
        return self.get_connection().execute(
            "SELECT source_cid, raw_path FROM sources WHERE resolved = 0;"
        ).fetchall()

    def orphan_wiki_paths(self) -> list[str]:
        """Vault-relative paths of parseable wiki notes with zero inbound and zero outbound links (§6.3)."""
        rows = self.get_connection().execute(
            """
            SELECT n.rel_path
            FROM notes n
            WHERE n.layer = 'wiki'
              AND n.parse_error IS NULL
              AND NOT EXISTS (SELECT 1 FROM links l1 WHERE l1.source_cid = n.cid)
              AND NOT EXISTS (SELECT 1 FROM links l2 WHERE l2.target_cid = n.cid)
            ORDER BY n.rel_path;
            """
        ).fetchall()
        return [r["rel_path"] for r in rows]

    def get_status(self) -> dict[str, Any]:
        """Return aggregate epistemic health metrics."""
        conn = self.get_connection()
        cur = conn.execute("SELECT COUNT(*) FROM notes WHERE layer = 'wiki';")
        total_wiki = cur.fetchone()[0]

        cur = conn.execute("SELECT COUNT(*) FROM notes WHERE layer = 'raw';")
        total_raw = cur.fetchone()[0]

        raw_text_exts = tuple(self.vault.config["raw_text_extensions"])
        filename_only_raw: dict[str, int] = {}
        for (rel_path,) in conn.execute("SELECT rel_path FROM notes WHERE layer = 'raw' ORDER BY rel_path;"):
            if not is_raw_text_path(rel_path, raw_text_exts):
                ext = Path(rel_path).suffix.lower()
                filename_only_raw[ext] = filename_only_raw.get(ext, 0) + 1
        filename_only_raw = dict(sorted(filename_only_raw.items()))

        unprocessed_raw = len(self.unprocessed_raw_paths())
        broken_links = conn.execute("SELECT COUNT(*) FROM links WHERE target_cid IS NULL;").fetchone()[0]

        cur = conn.execute(
            "SELECT layer, COUNT(*) as cnt FROM notes WHERE layer != 'raw' GROUP BY layer;"
        )
        domain_counts = {r["layer"]: r["cnt"] for r in cur.fetchall()}
        total_notes = sum(domain_counts.values())

        cur = conn.execute(
            "SELECT trust_tier, COUNT(*) as cnt FROM notes WHERE layer != 'raw' GROUP BY trust_tier;"
        )
        tier_counts = {r["trust_tier"] or "unverified": r["cnt"] for r in cur.fetchall()}
        stale_count = tier_counts.get("stale-verified", 0)

        cur = conn.execute(
            "SELECT type, COUNT(*) as cnt FROM notes WHERE layer != 'raw' GROUP BY type;"
        )
        type_counts = {r["type"] or "unknown": r["cnt"] for r in cur.fetchall()}

        cur = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM notes WHERE layer != 'raw' GROUP BY status;"
        )
        status_counts = {r["status"] or "unknown": r["cnt"] for r in cur.fetchall()}

        return {
            "vault_name": self.vault.config["vault_name"],
            "root": str(self.vault.root),
            "total_notes": total_notes,
            "total_wiki": total_wiki,
            "domains": domain_counts,
            "total_raw": total_raw,
            "unprocessed_raw": unprocessed_raw,
            "filename_only_raw": filename_only_raw,
            "broken_links": broken_links,
            "verification_debt": stale_count,
            "trust_tiers": tier_counts,
            "types": type_counts,
            "statuses": status_counts,
            "integrity": self.vault.config["integrity"],
        }

    def check_fts_integrity(self) -> bool:
        """Check both FTS5 indexes against their content, not only their structure (§4.2).

        `rank = 1` makes FTS5 compare every indexed term with the content view,
        which is what catches a missed or mis-targeted retraction; the plain form
        only checks the index's internal consistency and passes on a stale index.
        Both tables, because a mismatched retraction corrupts one index while
        leaving the other clean, and checking only the curated one would report
        health while raw search silently returns wrong rows.
        """
        conn = self.get_connection()
        try:
            for table in ("notes_fts", "raw_fts"):
                conn.execute(f"INSERT INTO {table}({table}, rank) VALUES('integrity-check', 1);")
            return True
        except sqlite3.DatabaseError:  # FTS5 reports an index/content mismatch as SQLITE_CORRUPT
            return False
        finally:
            # The INSERT opened an implicit transaction; left open it held the
            # write lock against every other process.
            if conn.in_transaction:
                conn.rollback()

    def close(self) -> None:
        """Close SQLite database connection if open."""
        if self._conn is not None:
            try:
                self._conn.close()
            except (sqlite3.Error, OSError):
                pass
            self._conn = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    def __del__(self) -> None:
        # Finalizers must not raise: a partially constructed instance can fail
        # in close() with AttributeError, which the interpreter would otherwise
        # print as an "Exception ignored" traceback on stderr. A connection
        # still open here is a leak, reported the way CPython reports its own,
        # so `-W error::ResourceWarning` can still see it.
        try:
            if getattr(self, "_conn", None) is not None:
                warnings.warn(f"unclosed VaultCache for {self.db_path}", ResourceWarning, stacklevel=2)
            self.close()
        except Exception:  # noqa: BLE001
            pass


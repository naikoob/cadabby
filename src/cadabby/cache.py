"""SQLite ephemeral cache, FTS5 BM25 search, incremental sync, and link graph storage.

Conforms strictly to Cadabby Technical Specification §4.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from cadabby.constants import (
    DIR_RAW,
    FTS_COLUMN_WEIGHTS,
    FTS_FIELD_HIT_FLOOR,
    SCHEMA_VERSION,
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
from cadabby.okf import compute_body_hash, derive_trust_tier, utc_now_iso
from cadabby.vault import Vault, cid_to_path, note_path_violation, path_to_cid, path_to_layer, path_to_stem


def _can_be_note(target_cid: str | None, template_rel: str | None) -> bool:
    """Whether a Markdown link target sits where a note could live (§4.4).

    Root files (`STYLE.md`), reserved and dot-folders, manifests and the
    template folder are links but never to notes: neither edges nor dead.
    """
    return target_cid is not None and note_path_violation(cid_to_path(target_cid), template_rel) is None


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


@dataclass(frozen=True, slots=True, kw_only=True)
class NoteRecord:
    """One row's worth of `notes` content, as handed to `VaultCache.upsert_note()`.

    The identity columns (`cid`, `stem`) and `indexed_at` are derived inside
    upsert_note, so a caller cannot hand in a CID that disagrees with its path.
    """

    rel_path: str
    layer: str
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


def _is_raw_text_path(rel_path: str, raw_text_exts: tuple[str, ...]) -> bool:
    """Whether a raw file's body is indexed under the configured extension list (§2.4)."""
    return bool(raw_text_exts) and rel_path.lower().endswith(raw_text_exts)


def sanitize_fts5_query(query: str) -> str:
    """Sanitize and prepare a query string for SQLite FTS5 MATCH expression.

    Handles:
    - Preserves explicitly quoted phrases (e.g. "B-Tree Index").
    - Quotes tokens with internal hyphens or special characters (e.g. B-tree -> "B-tree")
      preventing SQLite FTS5 from treating '-' as column selectors or unary NOT operators.
    - Preserves uppercase boolean operators (AND, OR, NOT) while stripping invalid leading, trailing, or consecutive operator sequences.
    - Supports prefix matching (e.g. term* -> term* or "phrase"*).
    - Cleans unclosed quotes and non-syntax characters gracefully.
    """
    query = query.strip()
    if not query:
        return ""

    if query.count('"') % 2 == 1:
        query = query + '"'

    tokens: list[str] = []
    pattern = re.compile(r"\"([^\"]*)\"|(\S+)")
    for match in pattern.finditer(query):
        quoted, bare = match.groups()
        if quoted is not None:
            clean_quoted = quoted.strip()  # the group cannot contain '"'
            if clean_quoted and any(c.isalnum() for c in clean_quoted):
                tokens.append(f'"{clean_quoted}"')
        elif bare is not None:
            if bare in ("AND", "OR", "NOT"):
                tokens.append(bare)
            else:
                has_prefix = bare.endswith("*") and len(bare) > 1
                base = bare[:-1] if has_prefix else bare
                clean_base = base.replace('"', "")
                if not clean_base or not any(c.isalnum() for c in clean_base):
                    continue
                if re.search(r"[^\w]", clean_base):
                    quoted_token = f'"{clean_base}"'
                    if has_prefix:
                        quoted_token = f"{quoted_token}*"
                    tokens.append(quoted_token)
                else:
                    if has_prefix:
                        tokens.append(f"{clean_base}*")
                    else:
                        tokens.append(clean_base)

    clean_tokens: list[str] = []
    for t in tokens:
        if t in ("AND", "OR", "NOT"):
            if not clean_tokens or clean_tokens[-1] in ("AND", "OR", "NOT"):
                continue
            clean_tokens.append(t)
        else:
            clean_tokens.append(t)
    while clean_tokens and clean_tokens[-1] in ("AND", "OR", "NOT"):
        clean_tokens.pop()

    return " ".join(clean_tokens)


_FTS_TOKEN_RE = re.compile(r'"[^"]*"\*?|\S+')


def or_fallback_fts5_query(clean_query: str) -> str | None:
    """Build an OR-joined FTS5 query when an implicit-AND multi-term query returns 0 rows.

    Returns None for empty/single-term queries or when the caller wrote explicit
    boolean operators (AND, OR, NOT), preserving strict boolean semantics when requested.
    """
    tokens = _FTS_TOKEN_RE.findall(clean_query)
    if len(tokens) < 2 or any(t in ("AND", "OR", "NOT") for t in tokens):
        return None
    return " OR ".join(tokens)


@dataclass
class SearchResult:
    """Represents a scored search result."""

    cid: str
    title: str | None
    description: str | None
    type: str | None
    status: str | None
    trust_tier: str | None
    score: float
    snippet: str
    domain: str = field(init=False)

    def __post_init__(self) -> None:
        self.domain = path_to_layer(self.cid)

    def to_dict(self) -> dict[str, Any]:
        """Convert SearchResult to serializable dictionary."""
        return {
            "cid": self.cid,
            "domain": self.domain,
            "title": self.title,
            "description": self.description,
            "type": self.type,
            "status": self.status,
            "trust_tier": self.trust_tier,
            "score": round(self.score, 4),
            "snippet": self.snippet,
        }


def _fts_table(layer: str | None) -> str:
    """Return the FTS table that indexes a given layer.

    Raw sources are kept in their own index so their term statistics do not
    enter the BM25 scoring of curated notes (see the DDL for what that costs).
    Every read and write must agree on this mapping, so it lives in one place.
    """
    return "raw_fts" if layer == DIR_RAW else "notes_fts"


def check_fts5_capability(conn: sqlite3.Connection) -> None:
    """Verify that SQLite was compiled with FTS5 virtual table support."""
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x);")
        conn.execute("DROP TABLE IF EXISTS _fts5_probe;")
    except sqlite3.OperationalError as e:
        raise RuntimeError(
            f"SQLite FTS5 extension is not available in Python {sys.version} "
            f"(sqlite_version={sqlite3.sqlite_version}). "
            "Please use a Python interpreter compiled with SQLite FTS5 support."
        ) from e


def reset_cache_files(db_path: Path) -> None:
    """Delete the cache database and its WAL/SHM sidecars (§4.2, `sync --rebuild`).

    The connection must be closed first. Deleting only `cache.db` would leave
    a `-wal` that SQLite replays into the fresh file on the next open.
    """
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)


def _schema_is_current(conn: sqlite3.Connection) -> bool:
    """True for an empty database or one recorded at SCHEMA_VERSION.

    Compared as text so a corrupt, non-integer version reads as stale instead
    of raising from `int()`.
    """
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table';")}
    if not tables:
        return True
    if "cache_meta" not in tables:
        return False
    row = conn.execute("SELECT value FROM cache_meta WHERE key = 'schema_version';").fetchone()
    return row is not None and str(row[0]) == str(SCHEMA_VERSION)


class VaultCache:
    """Manages the disposable SQLite cache at .cadabby/cache.db."""

    def __init__(self, vault: Vault) -> None:
        self.vault = vault
        self.db_path = vault.cache_db_path
        self._conn: sqlite3.Connection | None = None

    def get_connection(self) -> sqlite3.Connection:
        """Open or return existing SQLite connection with WAL and busy timeout.

        A cache at any other schema version -- or one whose version cannot be
        read -- is discarded and rebuilt from the vault (§4.2: the cache is
        disposable). Rebuilding in place by DROPping known tables would miss
        any table a newer schema added.
        """
        if self._conn is None:
            conn = self._open()
            if not _schema_is_current(conn):
                conn.close()
                reset_cache_files(self.db_path)
                conn = self._open()
            self._conn = conn
            self._create_tables(conn)
        return self._conn

    def _open(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        check_fts5_capability(conn)
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

            CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
                title, description, body, tags,
                content = 'notes',
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
                content = 'notes',
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
        tags_str = " ".join(str(t) for t in (record.tags or []))
        # Positionally matched to _NOTE_COLUMNS.
        values = (
            path_to_cid(record.rel_path),
            path_to_stem(record.rel_path),
            record.layer,
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
                f"INSERT INTO {_fts_table(record.layer)}(rowid, title, description, body, tags) "
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
                st = abs_path.stat()
                f_hash = compute_file_sha256(abs_path) if integrity_mode == "hash" else None
                cached = cached_meta.get(rel_path)
                is_raw = path_to_layer(rel_path) == DIR_RAW
                stale_by_config = is_raw and raw_exts_changed
                if cached is not None and not force and not stale_by_config and _is_unchanged(cached, st, f_hash, integrity_mode):
                    continue

                if is_raw:
                    self._index_raw_file(rel_path, abs_path, st, f_hash, raw_text_exts)
                else:
                    self._index_note_file(rel_path, abs_path, st, f_hash, template_rel)

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
            from cadabby.indexer import sync_vault_index

            sync_vault_index(self.vault, cache=self)

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
        layer = path_to_layer(rel_path)
        cid = path_to_cid(rel_path)
        content = abs_path.read_text("utf-8", errors="replace")
        try:
            fm, body = parse_frontmatter(content)
        except FrontmatterParseError as e:
            # Record the error and keep the full content (lint re-parses it for
            # the line number), exclude the row from search, and clear stale edges.
            self.upsert_note(
                NoteRecord(
                    rel_path=rel_path,
                    layer=layer,
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
        self.upsert_note(
            NoteRecord(
                rel_path=rel_path,
                layer=layer,
                mtime=st.st_mtime,
                file_size=st.st_size,
                file_hash=f_hash,
                body=body,
                title=fm.get("title") or abs_path.stem,
                description=fm.get("description"),
                type_=fm.get("type"),
                status=fm.get("status"),
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
        is_text = _is_raw_text_path(rel_path, raw_text_exts)
        self.upsert_note(
            NoteRecord(
                rel_path=rel_path,
                layer=path_to_layer(rel_path),
                mtime=st.st_mtime,
                file_size=st.st_size,
                file_hash=f_hash,
                body=abs_path.read_text("utf-8", errors="replace") if is_text else "",
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
        link_updates: list[tuple[str | None, int]] = []
        for link_row in conn.execute("SELECT id, source_cid, target_raw, kind FROM links;").fetchall():
            target_raw = link_row["target_raw"]
            if link_row["kind"] == LINK_KIND_MARKDOWN:
                # Exact path from the citing note's folder; never a stem fallback (§4.4).
                md_cid = resolve_markdown_target(link_row["source_cid"], target_raw)
                link_updates.append((md_cid if md_cid in resolver.known_cids else None, link_row["id"]))
            else:
                link_updates.append((resolver.resolve(target_raw.split("#")[0]), link_row["id"]))
        conn.executemany("UPDATE links SET target_cid = ? WHERE id = ?;", link_updates)

    def get_link_resolver(self) -> LinkTargetIndex:
        """A LinkTargetIndex over every parseable non-raw note. A pure read: callers scan first (§4.3)."""
        conn = self.get_connection()
        rows = conn.execute("SELECT cid FROM notes WHERE layer != 'raw' AND parse_error IS NULL;")
        return LinkTargetIndex([row["cid"] for row in rows])

    def get_note_neighborhood(
        self, cid: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Return (forward_links, backlinks, sources) for `cid` (§5.1)."""
        conn = self.get_connection()
        links = [
            {"target": r["target_raw"], "resolved_cid": r["target_cid"], "kind": r["kind"]}
            for r in conn.execute(
                "SELECT target_raw, target_cid, kind FROM links WHERE source_cid = ?;",
                (cid,),
            ).fetchall()
        ]
        backlinks = [
            {"source_cid": r["source_cid"], "target_raw": r["target_raw"], "kind": r["kind"]}
            for r in conn.execute(
                "SELECT source_cid, target_raw, kind FROM links WHERE target_cid = ?;",
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
        query: str,
        type_: str | None = None,
        status: str | None = None,
        trust: str | None = None,
        tag: str | None = None,
        domain: str | None = None,
        limit: int = 20,
    ) -> list[SearchResult]:
        """Perform BM25 search across cognitive domain notes with epistemic rank boosting conforming to §4.4."""
        clean_query = sanitize_fts5_query(query)
        if not clean_query:
            return []

        # A pure read like every getter here: entry points scan first (§4.3).
        conn = self.get_connection()

        # Multipliers were validated as finite numbers by load_vault_config
        # (§2.6); keys are free-form strings, so they are still SQL-escaped.
        ranking_cfg = self.vault.config["ranking"]

        def safe_case(k: str, v: float) -> str:
            clean_k = k.replace("'", "''")
            return f"WHEN '{clean_k}' THEN {float(v)}"

        def multiplier_expr(column: str, cfg: dict[str, float]) -> str:
            cases = " ".join(safe_case(k, v) for k, v in cfg.items())
            # An empty CASE body is a syntax error; a neutral multiplier is not
            return f"CASE {column} {cases} ELSE 1.0 END" if cases else "1.0"

        # Searching the raw layer reads a different index (see _fts_table). The
        # table name is chosen here, never interpolated from caller input.
        fts = _fts_table(domain)

        field_hits = " + ".join(
            f"(CASE WHEN instr(highlight({fts}, {idx}, char(1), char(2)), char(1)) > 0 THEN {w} ELSE 0.0 END)"
            for idx, w in enumerate(FTS_COLUMN_WEIGHTS)
        )
        score_expr = f"""
            ((-bm25({fts}, {FTS_COLUMN_WEIGHTS[0]}, {FTS_COLUMN_WEIGHTS[1]}, {FTS_COLUMN_WEIGHTS[2]}, {FTS_COLUMN_WEIGHTS[3]}))
             + ({FTS_FIELD_HIT_FLOOR} * ({field_hits})))
            * ({multiplier_expr("n.trust_tier", ranking_cfg["trust"])})
            * ({multiplier_expr("n.status", ranking_cfg["status"])})
        """

        where_clauses = [
            f"{fts} MATCH :query",
            "n.parse_error IS NULL",
        ]
        params: dict[str, Any] = {"query": clean_query, "limit": limit}

        if domain:
            where_clauses.append("n.layer = :domain")
            params["domain"] = domain
        else:
            where_clauses.append("n.layer != 'raw'")

        if type_:
            where_clauses.append("n.type = :type")
            params["type"] = type_
        if status:
            where_clauses.append("n.status = :status")
            params["status"] = status
        if trust:
            where_clauses.append("n.trust_tier = :trust")
            params["trust"] = trust
        if tag:
            # Exact, whole-tag containment against the space-joined column. This is
            # unambiguous only because tags may not contain whitespace (§3.1).
            # Stored tags are canonically lowercase, so fold the probe to match.
            where_clauses.append("(instr(' ' || n.tags || ' ', ' ' || :tag || ' ') > 0)")
            params["tag"] = tag.lower()

        sql = f"""
            SELECT n.cid,
                   n.title,
                   n.description,
                   n.type,
                   n.status,
                   n.trust_tier,
                   {score_expr} AS score,
                   snippet({fts}, 2, '<b>', '</b>', '...', 15) AS snippet
            FROM {fts}
            JOIN notes n ON n.id = {fts}.rowid
            WHERE {" AND ".join(where_clauses)}
            ORDER BY score DESC, n.cid ASC
            LIMIT :limit;
        """

        try:
            rows = conn.execute(sql, params).fetchall()
            if not rows:
                fallback_query = or_fallback_fts5_query(clean_query)
                if fallback_query is not None:
                    rows = conn.execute(sql, {**params, "query": fallback_query}).fetchall()
        except sqlite3.OperationalError as e:
            # Only a MATCH expression FTS5 cannot parse means "no results". A
            # lock or I/O failure must propagate so classify() reports it as
            # retryable instead of as an empty vault (§5.4).
            if "fts5: syntax error" in str(e):
                return []
            raise
        return [
            SearchResult(
                cid=row["cid"],
                title=row["title"],
                description=row["description"],
                type=row["type"],
                status=row["status"],
                trust_tier=row["trust_tier"],
                score=float(row["score"]),
                snippet=row["snippet"],
            )
            for row in rows
        ]

    def unprocessed_raw_paths(self) -> list[str]:
        """Raw files no note's `sources:` resolves to, sorted (§2.5, §4.5)."""
        return unprocessed_raw_paths(self.get_connection())

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
            if not _is_raw_text_path(rel_path, raw_text_exts):
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
        """Run FTS5 internal integrity check over both indexes.

        Both, because a mismatched retraction corrupts one index while leaving
        the other clean, and checking only the curated one would report health
        while raw search silently returns wrong rows.
        """
        conn = self.get_connection()
        try:
            for table in ("notes_fts", "raw_fts"):
                conn.execute(f"INSERT INTO {table}({table}) VALUES('integrity-check');")
            return True
        except sqlite3.OperationalError:
            return False

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
        # print as an "Exception ignored" traceback on stderr.
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass


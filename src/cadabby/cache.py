"""SQLite ephemeral cache, FTS5 BM25 search, incremental sync, and link graph storage.

Conforms strictly to Cadabby Technical Specification §4.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from cadabby.constants import (
    DEFAULT_IGNORED_DIRS,
    DEFAULT_RAW_TEXT_EXTENSIONS,
    DEFAULT_STATUS_MULTIPLIERS,
    DEFAULT_TRUST_MULTIPLIERS,
    FILE_AGENTS,
    FTS_COLUMN_WEIGHTS,
    SCHEMA_VERSION,
)
from cadabby.frontmatter import FrontmatterParseError, parse_frontmatter
from cadabby.fsutil import compute_file_sha256
from cadabby.graph import LinkTargetIndex, extract_wikilinks
from cadabby.okf import compute_body_hash, derive_trust_tier
from cadabby.vault import Vault, path_to_cid, path_to_layer, path_to_stem


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
    domain: str = ""

    def __post_init__(self):
        if not self.domain and self.cid:
            self.domain = self.cid.split("/")[0]

    def to_dict(self) -> dict[str, Any]:
        """Convert SearchResult to serializable dictionary."""
        return {
            "cid": self.cid,
            "domain": self.domain or (self.cid.split("/")[0] if self.cid else ""),
            "title": self.title,
            "description": self.description,
            "type": self.type,
            "status": self.status,
            "trust_tier": self.trust_tier,
            "score": round(self.score, 4),
            "snippet": self.snippet,
        }


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


class VaultCache:
    """Manages the disposable SQLite cache at .cadabby/cache.db."""

    def __init__(self, vault: Vault):
        self.vault = vault
        self.db_path = vault.cache_db_path
        self._conn: sqlite3.Connection | None = None

    def get_connection(self) -> sqlite3.Connection:
        """Open or return existing SQLite connection with WAL and busy timeout."""
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            check_fts5_capability(conn)
            self._conn = conn
            self._ensure_schema()
        return self._conn

    def __del__(self) -> None:
        self.close()

    def _ensure_schema(self) -> None:
        """Create or verify database schema version."""
        conn = self._conn
        assert conn is not None

        # Check schema version
        try:
            cur = conn.execute("SELECT value FROM cache_meta WHERE key = 'schema_version';")
            row = cur.fetchone()
            if row and int(row[0]) != SCHEMA_VERSION:
                # Schema version mismatch; drop tables and rebuild
                self._rebuild_tables(conn)
                return
        except sqlite3.OperationalError:
            # Table doesn't exist yet
            pass

        self._create_tables(conn)

    def _rebuild_tables(self, conn: sqlite3.Connection) -> None:
        """Drop all tables and recreate clean schema."""
        conn.execute("DROP TABLE IF EXISTS notes_fts;")
        conn.execute("DROP TABLE IF EXISTS sources;")
        conn.execute("DROP TABLE IF EXISTS links;")
        conn.execute("DROP TABLE IF EXISTS notes;")
        conn.execute("DROP TABLE IF EXISTS cache_meta;")
        self._create_tables(conn)

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
                occurrences INTEGER NOT NULL DEFAULT 1
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_links_edge ON links(source_cid, target_raw);
            CREATE INDEX IF NOT EXISTS idx_links_target ON links(target_cid);
            CREATE INDEX IF NOT EXISTS idx_links_source ON links(source_cid);

            CREATE TABLE IF NOT EXISTS sources (
                source_cid  TEXT NOT NULL,
                raw_path    TEXT NOT NULL,
                resolved    INTEGER NOT NULL,
                PRIMARY KEY (source_cid, raw_path)
            );
            CREATE INDEX IF NOT EXISTS idx_sources_raw ON sources(raw_path);

            CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
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

    def upsert_note(
        self,
        rel_path: str,
        layer: str,
        mtime: float,
        file_size: int,
        file_hash: str | None,
        title: str | None,
        description: str | None,
        type_: str | None,
        status: str | None,
        trust_tier: str | None,
        body_hash: str | None,
        frontmatter_json: str | None,
        parse_error: str | None,
        body: str,
        tags: list[str] | None = None,
    ) -> int:
        """Upsert a note into 'notes' and maintain 'notes_fts' with explicit retraction.

        This is the ONLY code path that writes to 'notes' or 'notes_fts'.
        """
        conn = self.get_connection()
        cid = path_to_cid(rel_path)
        stem = path_to_stem(rel_path)
        indexed_at = datetime.now(UTC).isoformat()
        tags_str = " ".join(str(t) for t in (tags or []))

        # Check existing row
        cur = conn.execute(
            "SELECT id, layer, parse_error, title, description, body, tags FROM notes WHERE rel_path = ?;",
            (rel_path,),
        )
        existing = cur.fetchone()

        if existing is not None:
            row_id = existing["id"]
            old_title = existing["title"] or ""
            old_desc = existing["description"] or ""
            old_body = existing["body"] or ""
            old_tags = existing["tags"] or ""

            # 1. Retract old terms from FTS5 index using OLD values if previously indexed
            if existing["layer"] != "raw" and existing["parse_error"] is None:
                try:
                    conn.execute(
                        "INSERT INTO notes_fts(notes_fts, rowid, title, description, body, tags) "
                        "VALUES('delete', ?, ?, ?, ?, ?);",
                        (row_id, old_title, old_desc, old_body, old_tags),
                    )
                except sqlite3.OperationalError:
                    pass

            # 2. Update 'notes' row
            conn.execute(
                """
                UPDATE notes SET
                    cid = ?, stem = ?, layer = ?, mtime = ?, file_size = ?, file_hash = ?,
                    indexed_at = ?, title = ?, description = ?, type = ?, status = ?,
                    trust_tier = ?, body_hash = ?, frontmatter_json = ?, parse_error = ?,
                    tags = ?, body = ?
                WHERE id = ?;
                """,
                (
                    cid,
                    stem,
                    layer,
                    mtime,
                    file_size,
                    file_hash,
                    indexed_at,
                    title,
                    description,
                    type_,
                    status,
                    trust_tier,
                    body_hash,
                    frontmatter_json,
                    parse_error,
                    tags_str,
                    body,
                    row_id,
                ),
            )
        else:
            # Insert new row
            cur = conn.execute(
                """
                INSERT INTO notes (
                    cid, rel_path, stem, layer, mtime, file_size, file_hash, indexed_at,
                    title, description, type, status, trust_tier, body_hash,
                    frontmatter_json, parse_error, tags, body
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    cid,
                    rel_path,
                    stem,
                    layer,
                    mtime,
                    file_size,
                    file_hash,
                    indexed_at,
                    title,
                    description,
                    type_,
                    status,
                    trust_tier,
                    body_hash,
                    frontmatter_json,
                    parse_error,
                    tags_str,
                    body,
                ),
            )
            row_id = cur.lastrowid
            assert row_id is not None

        # 3. Insert into FTS5 index only if valid cognitive domain note without parse errors
        if layer != "raw" and parse_error is None:
            conn.execute(
                "INSERT INTO notes_fts(rowid, title, description, body, tags) VALUES (?, ?, ?, ?, ?);",
                (row_id, title or "", description or "", body, tags_str),
            )

        return row_id

    def delete_note(self, rel_path: str) -> bool:
        """Delete a note and retract all FTS5 terms and graph edges.

        This is the ONLY code path that deletes notes.
        """
        conn = self.get_connection()
        cur = conn.execute(
            "SELECT id, cid, layer, parse_error, title, description, body, tags FROM notes WHERE rel_path = ?;",
            (rel_path,),
        )
        row = cur.fetchone()
        if row is None:
            return False

        row_id = row["id"]
        cid = row["cid"]
        old_title = row["title"] or ""
        old_desc = row["description"] or ""
        old_body = row["body"] or ""
        old_tags = row["tags"] or ""

        # Retract from FTS5 if previously indexed
        if row["layer"] != "raw" and row["parse_error"] is None:
            try:
                conn.execute(
                    "INSERT INTO notes_fts(notes_fts, rowid, title, description, body, tags) "
                    "VALUES('delete', ?, ?, ?, ?, ?);",
                    (row_id, old_title, old_desc, old_body, old_tags),
                )
            except sqlite3.OperationalError:
                pass

        # Delete dependent edges
        conn.execute("DELETE FROM links WHERE source_cid = ?;", (cid,))
        conn.execute("DELETE FROM sources WHERE source_cid = ?;", (cid,))
        conn.execute("DELETE FROM notes WHERE id = ?;", (row_id,))

        return True

    def scan(self, force: bool = False) -> tuple[int, int, int, int]:
        """Perform incremental scan over vault files.

        Returns:
            (inserted, updated, deleted, total)
        """
        conn = self.get_connection()
        integrity_mode = self.vault.config.get("integrity", "mtime_size")
        raw_text_exts = tuple(self.vault.config.get("raw_text_extensions", DEFAULT_RAW_TEXT_EXTENSIONS))

        # Discover all files on disk under raw/ and cognitive domains
        disk_files: dict[str, Path] = {}

        # 1. Raw evidence directory
        if self.vault.raw_dir.exists():
            for root, dirs, files in os.walk(self.vault.raw_dir, topdown=True):
                dirs[:] = [d for d in dirs if d not in DEFAULT_IGNORED_DIRS and not d.startswith(".")]
                for f in files:
                    if not f.startswith("."):
                        p = Path(root) / f
                        if not any(part in DEFAULT_IGNORED_DIRS for part in p.parts):
                            rel = self.vault.rel_path(p)
                            disk_files[rel] = p

        # 2. Cognitive domains (wiki, customers, projects, etc.)
        domains = self.vault.discover_domains()
        for domain_def in domains.values():
            if domain_def.path.exists():
                for root, dirs, files in os.walk(domain_def.path, topdown=True):
                    dirs[:] = [d for d in dirs if d not in DEFAULT_IGNORED_DIRS and not d.startswith(".")]
                    for f in files:
                        if not f.startswith("."):
                            if f == FILE_AGENTS:
                                continue
                            p = Path(root) / f
                            rel = self.vault.rel_path(p)
                            disk_files[rel] = p

        # 1. Deletion reconciliation: find notes in DB missing from disk
        cur = conn.execute("SELECT rel_path FROM notes;")
        db_paths = {row["rel_path"] for row in cur.fetchall()}

        deleted_count = 0
        for missing_path in db_paths - disk_files.keys():
            self.delete_note(missing_path)
            deleted_count += 1

        # 2. Inspect and upsert new or modified files
        inserted_count = 0
        updated_count = 0

        # Query existing mtime and sizes
        cur = conn.execute("SELECT rel_path, mtime, file_size, file_hash FROM notes;")
        cached_meta = {row["rel_path"]: row for row in cur.fetchall()}

        modified_cids: set[str] = set()

        for rel_path, abs_path in disk_files.items():
            st = abs_path.stat()
            mtime = st.st_mtime
            size = st.st_size
            f_hash = compute_file_sha256(abs_path) if integrity_mode == "hash" else None

            existing = cached_meta.get(rel_path)
            is_new = existing is None
            is_modified = False

            if not is_new and not force:
                if integrity_mode == "hash":
                    is_modified = existing["file_hash"] != f_hash
                else:
                    is_modified = (abs(existing["mtime"] - mtime) > 1e-4) or (existing["file_size"] != size)

            if not is_new and not is_modified and not force:
                continue

            # Process file
            layer = path_to_layer(rel_path)
            cid = path_to_cid(rel_path)
            modified_cids.add(cid)

            if layer != "raw":
                content = abs_path.read_text("utf-8", errors="replace")
                try:
                    fm, body = parse_frontmatter(content)
                    b_hash = compute_body_hash(body)
                    tier = derive_trust_tier(fm.get("verified"), b_hash)
                    title = fm.get("title") or abs_path.stem
                    desc = fm.get("description")
                    type_ = fm.get("type")
                    status = fm.get("status")
                    tags = fm.get("tags") if isinstance(fm.get("tags"), list) else None
                    sources = fm.get("sources") if isinstance(fm.get("sources"), list) else []

                    self.upsert_note(
                        rel_path=rel_path,
                        layer=layer,
                        mtime=mtime,
                        file_size=size,
                        file_hash=f_hash,
                        title=title,
                        description=desc,
                        type_=type_,
                        status=status,
                        trust_tier=tier,
                        body_hash=b_hash,
                        frontmatter_json=json.dumps(fm),
                        parse_error=None,
                        body=body,
                        tags=tags,
                    )

                    # Update links from body
                    conn.execute("DELETE FROM links WHERE source_cid = ?;", (cid,))
                    extracted = extract_wikilinks(body)
                    for link in extracted:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO links(source_cid, target_raw, target_cid, alias, anchor, occurrences)
                            VALUES (?, ?, NULL, ?, ?, ?);
                            """,
                            (cid, link.target_raw, link.alias, link.anchor, link.occurrences),
                        )

                    # Update sources from frontmatter
                    conn.execute("DELETE FROM sources WHERE source_cid = ?;", (cid,))
                    for src in sources:
                        if isinstance(src, str):
                            is_resolved = 1 if (self.vault.root / src).exists() else 0
                            conn.execute(
                                "INSERT OR REPLACE INTO sources(source_cid, raw_path, resolved) VALUES (?, ?, ?);",
                                (cid, src, is_resolved),
                            )

                except FrontmatterParseError as e:
                    # Unparseable frontmatter; record error, exclude from search
                    self.upsert_note(
                        rel_path=rel_path,
                        layer=layer,
                        mtime=mtime,
                        file_size=size,
                        file_hash=f_hash,
                        title=None,
                        description=None,
                        type_=None,
                        status=None,
                        trust_tier=None,
                        body_hash=None,
                        frontmatter_json=None,
                        parse_error=str(e),
                        body=content,
                        tags=None,
                    )
            else:
                # Raw file
                is_text = rel_path.lower().endswith(raw_text_exts)
                body = abs_path.read_text("utf-8", errors="replace") if is_text else ""

                self.upsert_note(
                    rel_path=rel_path,
                    layer=layer,
                    mtime=mtime,
                    file_size=size,
                    file_hash=f_hash,
                    title=abs_path.stem,
                    description=None,
                    type_="source",
                    status="raw",
                    trust_tier=None,
                    body_hash=None,
                    frontmatter_json=None,
                    parse_error=None,
                    body=body,
                    tags=None,
                )

            if is_new:
                inserted_count += 1
            else:
                updated_count += 1

        # 3. Resolve link targets if vault structure changed or new/updated links exist
        if inserted_count > 0 or updated_count > 0 or deleted_count > 0 or force:
            cur = conn.execute("SELECT cid FROM notes WHERE layer != 'raw' AND parse_error IS NULL;")
            all_cids = [row["cid"] for row in cur.fetchall()]
            resolver = LinkTargetIndex(all_cids)

            # If note topology changed (inserted/deleted/force), re-resolve all links.
            # If only existing notes were updated, only resolve the newly inserted unresolved links.
            if inserted_count > 0 or deleted_count > 0 or force:
                cur = conn.execute("SELECT id, target_raw FROM links;")
            else:
                cur = conn.execute("SELECT id, target_raw FROM links WHERE target_cid IS NULL;")

            link_updates = []
            for link_row in cur.fetchall():
                link_id = link_row["id"]
                target_raw = link_row["target_raw"]
                target_stem = target_raw.split("#")[0]
                resolved_cid = resolver.resolve(target_stem)
                link_updates.append((resolved_cid, link_id))

            conn.executemany("UPDATE links SET target_cid = ? WHERE id = ?;", link_updates)

        conn.commit()

        cur = conn.execute("SELECT COUNT(*) FROM notes;")
        total_count = cur.fetchone()[0]

        return inserted_count, updated_count, deleted_count, total_count

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
        # Ensure cache is synced
        self.scan()
        conn = self.get_connection()

        # Build dynamic multipliers CASE statements from config with fallback to defaults
        ranking_cfg = self.vault.config.get("ranking", {})
        trust_cfg = ranking_cfg.get("trust") if isinstance(ranking_cfg.get("trust"), dict) else DEFAULT_TRUST_MULTIPLIERS
        status_cfg = ranking_cfg.get("status") if isinstance(ranking_cfg.get("status"), dict) else DEFAULT_STATUS_MULTIPLIERS

        trust_cases = " ".join(f"WHEN '{k}' THEN {float(v)}" for k, v in trust_cfg.items())
        status_cases = " ".join(f"WHEN '{k}' THEN {float(v)}" for k, v in status_cfg.items())

        score_expr = f"""
            (-bm25(notes_fts, {FTS_COLUMN_WEIGHTS[0]}, {FTS_COLUMN_WEIGHTS[1]}, {FTS_COLUMN_WEIGHTS[2]}, {FTS_COLUMN_WEIGHTS[3]}))
            * (CASE n.trust_tier {trust_cases} ELSE 1.0 END)
            * (CASE n.status {status_cases} ELSE 1.0 END)
        """

        where_clauses = [
            "notes_fts MATCH :query",
            "n.parse_error IS NULL",
        ]
        params: dict[str, Any] = {"query": query, "limit": limit}

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
            where_clauses.append("(instr(' ' || n.tags || ' ', ' ' || :tag || ' ') > 0)")
            params["tag"] = tag

        sql = f"""
            SELECT n.cid,
                   n.title,
                   n.description,
                   n.type,
                   n.status,
                   n.trust_tier,
                   {score_expr} AS score,
                   snippet(notes_fts, 2, '<b>', '</b>', '...', 15) AS snippet
            FROM notes_fts
            JOIN notes n ON n.id = notes_fts.rowid
            WHERE {" AND ".join(where_clauses)}
            ORDER BY score DESC
            LIMIT :limit;
        """

        try:
            cur = conn.execute(sql, params)
            results = []
            for row in cur.fetchall():
                cid = row["cid"]
                results.append(
                    SearchResult(
                        cid=cid,
                        title=row["title"],
                        description=row["description"],
                        type=row["type"],
                        status=row["status"],
                        trust_tier=row["trust_tier"],
                        score=float(row["score"]),
                        snippet=row["snippet"],
                        domain=cid.split("/")[0] if cid else "",
                    )
                )
            return results
        except sqlite3.OperationalError:
            # Bad query syntax in MATCH expression (e.g. unclosed quote)
            return []

    def get_status(self) -> dict[str, Any]:
        """Return aggregate epistemic health metrics."""
        conn = self.get_connection()
        cur = conn.execute("SELECT COUNT(*) FROM notes WHERE layer = 'wiki';")
        total_wiki = cur.fetchone()[0]

        cur = conn.execute("SELECT COUNT(*) FROM notes WHERE layer = 'raw';")
        total_raw = cur.fetchone()[0]

        cur = conn.execute(
            """
            SELECT COUNT(*) FROM notes r
            WHERE r.layer = 'raw'
              AND NOT EXISTS (SELECT 1 FROM sources s WHERE s.raw_path = r.rel_path);
            """
        )
        unprocessed_raw = cur.fetchone()[0]

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
            "vault_name": self.vault.config.get("vault_name", "vault"),
            "root": str(self.vault.root),
            "total_notes": total_notes,
            "total_wiki": total_wiki,
            "domains": domain_counts,
            "total_raw": total_raw,
            "unprocessed_raw": unprocessed_raw,
            "unprocessed_raw_sources": unprocessed_raw,
            "verification_debt": stale_count,
            "trust_tiers": tier_counts,
            "types": type_counts,
            "statuses": status_counts,
            "integrity": self.vault.config.get("integrity", "mtime_size"),
        }

    def check_fts_integrity(self) -> bool:
        """Run FTS5 internal integrity check."""
        conn = self.get_connection()
        try:
            conn.execute("INSERT INTO notes_fts(notes_fts) VALUES('integrity-check');")
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

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

"""SQLite ephemeral cache, FTS5 BM25 search, incremental sync, and link graph storage.

Conforms strictly to Cadabby Technical Specification §4.
"""

from __future__ import annotations

import json
import math
import os
import re
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
    FTS_COLUMN_WEIGHTS,
    FTS_FIELD_HIT_FLOOR,
    SCHEMA_VERSION,
)
from cadabby.frontmatter import FrontmatterParseError, parse_frontmatter
from cadabby.fsutil import compute_file_sha256
from cadabby.graph import LinkTargetIndex, extract_wikilinks
from cadabby.okf import compute_body_hash, derive_trust_tier
from cadabby.vault import Vault, path_to_cid, path_to_layer, path_to_stem


def _is_valid_raw_source(vault: Vault, src: str) -> bool:
    """Return True if `src` is a relative path pointing to an existing regular file inside `raw/`."""
    if not src or Path(src).is_absolute():
        return False
    try:
        raw_root = vault.raw_dir.resolve()
        candidate = (vault.root / src).resolve()
        return candidate.is_relative_to(raw_root) and candidate.is_file()
    except OSError:
        return False


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
            clean_quoted = quoted.replace('"', '""').strip()
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


def _fts_table(layer: str | None) -> str:
    """Return the FTS table that indexes a given layer.

    Raw sources are kept in their own index so their term statistics do not
    enter the BM25 scoring of curated notes (see the DDL for what that costs).
    Every read and write must agree on this mapping, so it lives in one place.
    """
    return "raw_fts" if layer == "raw" else "notes_fts"


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
        conn.execute("DROP TABLE IF EXISTS raw_fts;")
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

            # 1. Retract old terms from FTS5 index using OLD values if previously
            # indexed. This must mirror the insert gate below exactly: retracting
            # a row that was never inserted, or retracting it from the wrong
            # table, corrupts an external-content index silently rather than
            # raising. Reading the table from the stored row keeps the whole
            # retraction sourced from old state, consistent with the old-values
            # rule above. It cannot currently differ from _fts_table(layer) --
            # layer is a pure function of rel_path and the row is keyed by
            # rel_path, so a layer change arrives as a delete plus an insert.
            if existing["parse_error"] is None:
                try:
                    old_fts = _fts_table(existing["layer"])
                    conn.execute(
                        f"INSERT INTO {old_fts}({old_fts}, rowid, title, description, body, tags) "
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

        # 3. Index everything that parsed, into the index for its corpus. Raw
        # sources are indexed at all so that the full text of a collected source
        # is reachable (§2.4) -- storing the body and never indexing it made
        # `raw/` a write-only surface, which reads to an agent as "this is not in
        # the vault". Non-text raw files carry an empty body and are indexed by
        # title alone, which is what makes `raw/paper.pdf` findable by name.
        if parse_error is None:
            conn.execute(
                f"INSERT INTO {_fts_table(layer)}(rowid, title, description, body, tags) "
                "VALUES (?, ?, ?, ?, ?);",
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

        # Retract from FTS5 if previously indexed (mirrors the insert gate)
        if row["parse_error"] is None:
            try:
                fts = _fts_table(row["layer"])
                conn.execute(
                    f"INSERT INTO {fts}({fts}, rowid, title, description, body, tags) "
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

    def scan(self, force: bool = False, regenerate_index: bool = True) -> tuple[int, int, int, int]:
        """Perform incremental scan over vault files.

        A scan that changed anything regenerates index.md before returning
        (§4.3). This lives here rather than at each of the ten call sites
        because "whichever entry point triggered it" is the actual requirement,
        and a list of call sites is a list someone eventually forgets to
        extend. regenerate_index=False exists for the index generator's own
        scan, which would otherwise recurse.

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
        for p, _, _ in self.vault.iter_domain_notes():
            disk_files[self.vault.rel_path(p)] = p

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
                    if extracted:
                        conn.executemany(
                            """
                            INSERT OR REPLACE INTO links(source_cid, target_raw, target_cid, alias, anchor, occurrences)
                            VALUES (?, ?, NULL, ?, ?, ?);
                            """,
                            [(cid, link.target_raw, link.alias, link.anchor, link.occurrences) for link in extracted],
                        )

                    # Update sources from frontmatter
                    conn.execute("DELETE FROM sources WHERE source_cid = ?;", (cid,))
                    if sources:
                        source_tuples = [
                            (cid, src, 1 if _is_valid_raw_source(self.vault, src) else 0)
                            for src in sources
                            if isinstance(src, str)
                        ]
                        if source_tuples:
                            conn.executemany(
                                "INSERT OR REPLACE INTO sources(source_cid, raw_path, resolved) VALUES (?, ?, ?);",
                                source_tuples,
                            )

                except FrontmatterParseError as e:
                    # Unparseable frontmatter; record error, exclude from search, and clear stale graph edges
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
                    conn.execute("DELETE FROM links WHERE source_cid = ?;", (cid,))
                    conn.execute("DELETE FROM sources WHERE source_cid = ?;", (cid,))
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
            resolver = self.get_link_resolver()
            cur = conn.execute("SELECT id, target_raw FROM links;")

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

        # A stale queue is worse than no queue: it sends an agent to redo work
        # that is already done (§4.3). Binding this to the scan rather than to
        # the write path also covers the case no write path sees -- a human
        # dropping a file into raw/. The byte-comparison guard in
        # sync_vault_index keeps an unchanged report from churning the diff.
        if regenerate_index and (inserted_count or updated_count or deleted_count):
            from cadabby.indexer import sync_vault_index

            sync_vault_index(self.vault, cache=self)

        return inserted_count, updated_count, deleted_count, total_count

    def get_link_resolver(self) -> LinkTargetIndex:
        """Return a LinkTargetIndex over all valid non-raw note CIDs."""
        conn = self.get_connection()
        cur = conn.execute("SELECT cid FROM notes WHERE layer != 'raw' AND parse_error IS NULL;")
        all_cids = [row["cid"] for row in cur.fetchall()]
        if not all_cids:
            cur = conn.execute("SELECT COUNT(*) FROM notes;")
            if cur.fetchone()[0] == 0:
                self.scan()
                cur = conn.execute("SELECT cid FROM notes WHERE layer != 'raw' AND parse_error IS NULL;")
                all_cids = [row["cid"] for row in cur.fetchall()]
        return LinkTargetIndex(all_cids)

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

        # Ensure cache is synced
        self.scan()
        conn = self.get_connection()

        # Build dynamic multipliers CASE statements from config with fallback to defaults.
        # Every level is type-checked: a hand-edited .cadabby.json may hold null or
        # a scalar anywhere in this subtree.
        ranking_cfg = self.vault.config.get("ranking")
        if not isinstance(ranking_cfg, dict):
            ranking_cfg = {}
        trust_cfg = ranking_cfg.get("trust") if isinstance(ranking_cfg.get("trust"), dict) else DEFAULT_TRUST_MULTIPLIERS
        status_cfg = ranking_cfg.get("status") if isinstance(ranking_cfg.get("status"), dict) else DEFAULT_STATUS_MULTIPLIERS

        def safe_case(k: Any, v: Any) -> str:
            clean_k = str(k).replace("'", "''")
            try:
                val = float(v)
            except (ValueError, TypeError):
                val = 1.0
            # inf/nan render as bare identifiers that SQLite cannot parse
            if not math.isfinite(val):
                val = 1.0
            return f"WHEN '{clean_k}' THEN {val}"

        def multiplier_expr(column: str, cfg: dict[Any, Any]) -> str:
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
            * ({multiplier_expr("n.trust_tier", trust_cfg)})
            * ({multiplier_expr("n.status", status_cfg)})
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

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def __del__(self) -> None:
        # Finalizers must not raise: a partially constructed instance can fail
        # in close() with AttributeError, which the interpreter would otherwise
        # print as an "Exception ignored" traceback on stderr.
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass


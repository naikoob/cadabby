"""FTS5 query sanitization, epistemic multiplier SQL, and BM25 / filter-only search.

Conforms strictly to Cadabby Technical Specification §4.2, §4.4.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from cadabby.constants import DEFAULT_LIMIT, DIR_RAW, FTS_COLUMN_WEIGHTS, FTS_FIELD_HIT_FLOOR
from cadabby.errors import InvalidArgumentError
from cadabby.okf import canonical_tag
from cadabby.vault import path_to_layer


def sanitize_fts5_query(query: str) -> str:
    """Sanitize and prepare a query string for SQLite FTS5 MATCH expression.

    Handles:
    - Preserves explicitly quoted phrases (e.g. "B-Tree Index").
    - Quotes tokens with internal hyphens or special characters (e.g. B-tree -> "B-tree")
      preventing SQLite FTS5 from treating '-' as column selectors or unary NOT operators.
    - Preserves uppercase boolean operators (AND, OR, NOT) while stripping invalid leading, trailing, or consecutive operator sequences.
    - Supports prefix matching (e.g. term* -> term* or "phrase"*).
    - Cleans unclosed quotes and non-syntax characters gracefully; a stray
      quote inside a word separates it (foo"bar -> foo "bar").
    - `a AND NOT b` keeps its meaning: FTS5's NOT is binary, so the AND is dropped.
    """
    query = query.strip()
    if not query:
        return ""

    if query.count('"') % 2 == 1:
        query = query + '"'

    tokens: list[str] = []
    pattern = re.compile(r"\"([^\"]*)\"(\*?)|([^\s\"]+)")
    for match in pattern.finditer(query):
        quoted, star, bare = match.groups()
        if quoted is not None:
            clean_quoted = quoted.strip()  # the group cannot contain '"'
            if clean_quoted and any(c.isalnum() for c in clean_quoted):
                tokens.append(f'"{clean_quoted}"{star}')
        elif bare is not None:
            if bare in ("AND", "OR", "NOT"):
                tokens.append(bare)
            else:
                has_prefix = bare.endswith("*") and len(bare) > 1
                base = bare[:-1] if has_prefix else bare
                clean_base = base  # the bare group cannot contain '"'
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
            if t == "NOT" and clean_tokens and clean_tokens[-1] == "AND":
                clean_tokens[-1] = "NOT"  # `a AND NOT b` is FTS5's `a NOT b`
                continue
            if not clean_tokens or clean_tokens[-1] in ("AND", "OR", "NOT"):
                continue
            clean_tokens.append(t)
        else:
            clean_tokens.append(t)
    while clean_tokens and clean_tokens[-1] in ("AND", "OR", "NOT"):
        clean_tokens.pop()

    return " ".join(clean_tokens)


_FTS_TOKEN_RE = re.compile(r'"[^"]*"\*?|\S+')
_CAMEL_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_CAMEL_LOWER_UPPER_RE = re.compile(r"([a-z0-9])([A-Z])")
_CAMEL_ACRONYM_UPPER_RE = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_ALPHA_DIGIT_RE = re.compile(r"([A-Za-z])([0-9])")
_CAMEL_DIGIT_LOWER_RE = re.compile(r"([0-9])([a-z])")
_OR_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
        "in", "into", "is", "it", "of", "on", "or", "that", "the", "to",
        "was", "with",
    }
)


def _coalesce_short_acronyms(pieces: list[str]) -> list[str]:
    """Merge 2-letter Capitalized + 1-letter Uppercase acronym pairs (`So` + `A` -> `SoA`)."""
    out: list[str] = []
    i = 0
    while i < len(pieces):
        if (
            i + 1 < len(pieces)
            and len(pieces[i]) == 2
            and pieces[i][0].isupper()
            and pieces[i][1].islower()
            and len(pieces[i + 1]) == 1
            and pieces[i + 1].isupper()
        ):
            out.append(pieces[i] + pieces[i + 1])
            i += 2
        else:
            out.append(pieces[i])
            i += 1
    return out


def _split_camelcase_parts(word: str) -> list[str]:
    """Split a mixed-case alphanumeric word into its atomic lowercase subword parts."""
    if word.islower() or word.isupper() or word.isdigit():
        return []
    first_pass = _CAMEL_LOWER_UPPER_RE.sub(r"\1 \2", word).split()
    alpha_parts: list[str] = []
    for piece in first_pass:
        alpha_parts.extend(_CAMEL_ACRONYM_UPPER_RE.sub(r"\1 \2", piece).split())
    parts: list[str] = []
    for piece in _coalesce_short_acronyms(alpha_parts):
        sub = _CAMEL_ALPHA_DIGIT_RE.sub(r"\1 \2", piece)
        sub = _CAMEL_DIGIT_LOWER_RE.sub(r"\1 \2", sub)
        parts.extend(sub.split())
    return [p.lower() for p in parts] if len(parts) >= 2 else []


def extract_camelcase_subwords(*texts: str | None) -> str:
    """Extract lowercase CamelCase subwords and adjacent/prefix compounds for FTS5 (§4.2, §4.4).

    `CrashLoopBackOff` yields atomic parts (`crash`, `loop`, `back`, `off`),
    adjacent pairs (`crashloop`, `loopback`, `backoff`), and progressive
    prefixes (`crashloopback`), plus any first-pass acronym splits (`SQLiteWAL` -> `sqlite`, `wal`).
    """
    seen: dict[str, None] = {}
    for text in texts:
        if not text:
            continue
        for word in _CAMEL_WORD_RE.findall(text):
            if word.islower() or word.isupper() or word.isdigit():
                continue
            for piece in _CAMEL_LOWER_UPPER_RE.sub(r"\1 \2", word).split():
                if piece.lower() != word.lower():
                    seen[piece.lower()] = None
            parts = _split_camelcase_parts(word)
            for idx, part in enumerate(parts):
                seen[part] = None
                if idx + 1 < len(parts):
                    seen[part + parts[idx + 1]] = None
                if 1 < idx + 1 < len(parts):
                    seen["".join(parts[: idx + 1])] = None
    return " ".join(seen)


def camelcase_fts5_query(clean_query: str) -> str | None:
    """Expand unquoted CamelCase query tokens into space-joined subwords (§4.4).

    A query for `CrashLoopBackOff` expands to `crash loop back off` (implicit
    AND), matching notes that write the words separately without matching every
    note that happens to contain `off` or `back` alone. Returns None when no
    token splits or when the caller wrote explicit boolean operators.
    """
    tokens = _FTS_TOKEN_RE.findall(clean_query)
    if not tokens or any(t in ("AND", "OR", "NOT") for t in tokens):
        return None
    expanded: list[str] = []
    changed = False
    for token in tokens:
        if not token.startswith('"'):
            parts = _split_camelcase_parts(token.rstrip("*"))
            if parts:
                expanded.extend(parts)
                changed = True
                continue
        expanded.append(token)
    if not changed:
        return None
    return " ".join(expanded)


def or_fallback_fts5_query(clean_query: str) -> str | None:
    """Build an OR-joined FTS5 query to fill an implicit-AND search short of `limit` (§4.4).

    Returns None when the caller wrote explicit boolean operators (AND, OR, NOT)
    or when the query has fewer than two distinct terms. Common English stopwords
    are dropped so an OR fallback does not match every note containing 'of' or 'the'.
    """
    tokens = _FTS_TOKEN_RE.findall(clean_query)
    if len(tokens) < 2 or any(t in ("AND", "OR", "NOT") for t in tokens):
        return None
    distinct: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        folded = token.casefold()
        if folded not in seen:
            seen.add(folded)
            distinct.append(token)
    if len(distinct) < 2:
        return None
    content = [t for t in distinct if t.casefold() not in _OR_STOPWORDS]
    if not content:
        return None
    return " OR ".join(content)


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


def _epistemic_multiplier_sql(ranking_cfg: dict[str, Any]) -> str:
    """Build the SQL expression for `:trust_mult * :status_mult * :domain_mult` (§4.4)."""
    # Multipliers were validated as finite numbers by load_vault_config
    # (§2.6); keys are free-form strings, so they are still SQL-escaped.
    def safe_case(k: str, v: float) -> str:
        clean_k = k.replace("'", "''")
        return f"WHEN '{clean_k}' THEN {float(v)}"

    def multiplier_expr(column: str, cfg: dict[str, float]) -> str:
        cases = " ".join(safe_case(k, v) for k, v in cfg.items())
        # An empty CASE body is a syntax error; a neutral multiplier is not
        return f"CASE {column} {cases} ELSE 1.0 END" if cases else "1.0"

    return f"""
        ({multiplier_expr("n.trust_tier", ranking_cfg["trust"])})
        * ({multiplier_expr("n.status", ranking_cfg["status"])})
        * ({multiplier_expr("n.layer", ranking_cfg.get("domain", {}))})
    """


def _build_search_filters(
    *,
    domain: str | None,
    type_: str | None,
    status: str | None,
    trust: str | None,
    tag: str | None,
    limit: int,
) -> tuple[list[str], dict[str, Any]]:
    """Build the common `WHERE` predicates and query parameters for `search()`."""
    where_clauses = ["n.parse_error IS NULL"]
    params: dict[str, Any] = {"limit": limit}

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
        # Stored tags are canonical (okf.canonical_tag), so the probe is too.
        where_clauses.append("(instr(' ' || n.tags || ' ', ' ' || :tag || ' ') > 0)")
        params["tag"] = canonical_tag(tag)

    return where_clauses, params


def _search_filter_only(
    conn: sqlite3.Connection,
    epistemic_mult_expr: str,
    where_clauses: list[str],
    params: dict[str, Any],
) -> list[sqlite3.Row]:
    """Run a filter-only search against `notes` ordered by epistemic multipliers (§4.4)."""
    sql = f"""
        SELECT n.cid,
               n.title,
               n.description,
               n.type,
               n.status,
               n.trust_tier,
               {epistemic_mult_expr} AS score,
               COALESCE(NULLIF(n.description, ''), substr(n.body, 1, 120), '') AS snippet
        FROM notes n
        WHERE {" AND ".join(where_clauses)}
        ORDER BY score DESC, n.cid ASC
        LIMIT :limit;
    """
    return conn.execute(sql, params).fetchall()


def _search_fts(
    conn: sqlite3.Connection,
    clean_query: str,
    domain: str | None,
    limit: int,
    epistemic_mult_expr: str,
    where_clauses: list[str],
    params: dict[str, Any],
) -> list[sqlite3.Row]:
    """Run an FTS5 BM25 search with field-presence floor and multi-term OR fill (§4.4)."""
    # Searching the raw layer reads a different index (see _fts_table). The
    # table name is chosen here, never interpolated from caller input.
    fts = _fts_table(domain)
    field_hits = " + ".join(
        f"(CASE WHEN instr(highlight({fts}, {idx}, char(1), char(2)), char(1)) > 0 THEN {w} ELSE 0.0 END)"
        for idx, w in enumerate(FTS_COLUMN_WEIGHTS)
    )
    bm25_weights = ", ".join(str(w) for w in FTS_COLUMN_WEIGHTS)
    score_expr = f"""
        ((-bm25({fts}, {bm25_weights}))
         + ({FTS_FIELD_HIT_FLOOR} * ({field_hits})))
        * {epistemic_mult_expr}
    """
    fts_params = {**params, "query": clean_query}
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
        WHERE {" AND ".join([f"{fts} MATCH :query", *where_clauses])}
        ORDER BY score DESC, n.cid ASC
        LIMIT :limit;
    """
    try:
        rows = conn.execute(sql, fts_params).fetchall()
        for fill_query in (camelcase_fts5_query(clean_query), or_fallback_fts5_query(clean_query)):
            if fill_query is not None and len(rows) < limit:
                # Primary matches keep the top slots; CamelCase subword AND and multi-term OR fill what is left.
                seen = {r["cid"] for r in rows}
                extra = conn.execute(sql, {**fts_params, "query": fill_query}).fetchall()
                rows += [r for r in extra if r["cid"] not in seen][: limit - len(rows)]
        return rows
    except sqlite3.OperationalError as e:
        # Only a MATCH expression FTS5 cannot parse means "no results". A
        # lock or I/O failure must propagate so classify() reports it as
        # retryable instead of as an empty vault (§5.4).
        if "fts5: syntax error" in str(e):
            return []
        raise


def execute_search(
    conn: sqlite3.Connection,
    ranking_cfg: dict[str, Any],
    *,
    query: str = "",
    type_: str | None = None,
    status: str | None = None,
    trust: str | None = None,
    tag: str | None = None,
    domain: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[SearchResult]:
    """Perform BM25 or filter-only search across cognitive domain notes with epistemic rank boosting (§4.4)."""
    # The OR fill compares against `limit`, so it must be a real count: a
    # string broke the comparison and a negative one (SQLite's "no limit")
    # silently skipped the fill.
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise InvalidArgumentError(f"limit must be a positive integer, got {limit!r}")
    if query is not None and not isinstance(query, str):
        raise InvalidArgumentError("query must be a string")

    raw_query = (query or "").strip()
    filter_only = not raw_query
    if filter_only and not any((type_, status, trust, tag, domain)):
        raise InvalidArgumentError(
            "Pass a non-empty search query or at least one filter (domain, type, status, trust, tag)"
        )

    clean_query = "" if filter_only else sanitize_fts5_query(raw_query)
    if not filter_only and not clean_query:
        # Nothing searchable survived (`---`, `"*"`): with a filter the
        # caller still said what they want, so answer that rather than `[]`.
        if not any((type_, status, trust, tag, domain)):
            return []
        filter_only = True

    # A pure read like every getter here: entry points scan first (§4.3).
    epistemic_mult_expr = _epistemic_multiplier_sql(ranking_cfg)
    where_clauses, params = _build_search_filters(
        domain=domain,
        type_=type_,
        status=status,
        trust=trust,
        tag=tag,
        limit=limit,
    )

    if filter_only:
        rows = _search_filter_only(conn, epistemic_mult_expr, where_clauses, params)
    else:
        rows = _search_fts(conn, clean_query, domain, limit, epistemic_mult_expr, where_clauses, params)

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

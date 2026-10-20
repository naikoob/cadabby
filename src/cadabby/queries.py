"""Read-only SQL over a cache connection: queues, graph views, lint rows, status counts.

The read side of the disposable cache (§4). Every function here takes an open
`sqlite3.Connection` and returns plain rows or dicts. Nothing here opens a
connection, writes a table, or reads vault configuration: connections belong
to `VaultCache` (the only caller of `get_connection()`), every write to its
`upsert_note()` / `delete_note()` pair (§4.2), and anything config-dependent to
its facade. That is what lets `index.md` and `vault_status` share one
definition per queue (§4.5) without either depending on the scan.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from cadabby.constants import DIR_ARCHIVE, TYPE_MOC
from cadabby.okf import is_actor_human, latest_stale_actor


def entry_point_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every MOC outside archive/ (cid, stem, rel_path, layer, description): wiki MOCs first, then by stem (§2.5).

    A `type: moc` note is a MOC wherever it lives. Before, a domain MOC such as
    `services/services-moc` was missing here and from `vault_graph`, and the
    wiki notes it linked were reported unfiled. Retired MOCs in archive/ are
    not entry points and file nothing.
    """
    return conn.execute(
        """
        SELECT cid, stem, rel_path, layer, description FROM notes
        WHERE layer != ? AND type = ? AND parse_error IS NULL
        ORDER BY (layer != 'wiki') ASC, stem ASC, cid ASC;
        """,
        (DIR_ARCHIVE, TYPE_MOC),
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
    `vault_status` queue both read it. Reachability starts at every MOC
    outside archive/ (the same set `entry_point_rows` lists), is transitive
    and may travel through any layer. Only active wiki notes are reported:
    the flat namespace is the one place a note has no folder to be found by,
    and filing a retired note is not work.
    """
    return conn.execute(
        """
        WITH RECURSIVE filed(cid) AS (
            SELECT cid FROM notes
            WHERE layer != ? AND type = ? AND parse_error IS NULL
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
        (DIR_ARCHIVE, TYPE_MOC),
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
    """Every MOC's link counts and the largest overlaps between MOCs (§5.1 `vault_graph`).

    MOCs are the set `entry_point_rows` lists: `type: moc` in any domain but
    archive/. `outgoing` counts distinct resolved note targets, `incoming`
    distinct linking notes. An overlap's `ratio` is shared targets over the
    smaller MOC's target count, so a MOC wholly contained in another reads
    1.0 whatever the sizes. Both lists are capped at `limit`, with totals.
    """
    mocs = conn.execute(
        """
        SELECT cid, title, trust_tier FROM notes
        WHERE layer != ? AND type = ? AND parse_error IS NULL
        ORDER BY cid ASC;
        """,
        (DIR_ARCHIVE, TYPE_MOC),
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


def note_neighborhood(
    conn: sqlite3.Connection, cid: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (forward_links, backlinks, sources) for `cid` with 1-hop neighbor metadata (§5.1)."""
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


def lint_note_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every non-raw note row the scan indexed, ordered by path for deterministic lint findings (§6.3)."""
    return conn.execute(
        """
        SELECT cid, rel_path, layer, body, frontmatter_json, parse_error, body_hash, trust_tier
        FROM notes
        WHERE layer != 'raw'
        ORDER BY rel_path;
        """
    ).fetchall()


def lint_link_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every extracted link edge for Gate 3 link/anchor validation (§6.3)."""
    return conn.execute("SELECT source_cid, target_raw, target_cid, anchor, kind FROM links;").fetchall()


def unresolved_source_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every `sources:` entry that did not resolve to an indexed raw/ file for Gate 4 (§6.3)."""
    return conn.execute("SELECT source_cid, raw_path FROM sources WHERE resolved = 0;").fetchall()


def orphan_wiki_paths(conn: sqlite3.Connection) -> list[str]:
    """Vault-relative paths of parseable wiki notes with zero inbound and zero outbound links (§6.3)."""
    rows = conn.execute(
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


def raw_rel_paths(conn: sqlite3.Connection) -> list[str]:
    """Every indexed raw/ path, sorted (§2.4)."""
    return [r[0] for r in conn.execute("SELECT rel_path FROM notes WHERE layer = 'raw' ORDER BY rel_path;")]


@dataclass(frozen=True, slots=True, kw_only=True)
class StatusCounts:
    """The SQL half of `vault_status` (§4.5); the facade adds what depends on config."""

    total_notes: int
    total_wiki: int
    domains: dict[str, int]
    total_raw: int
    unprocessed_raw: int
    broken_links: int
    verification_debt: int
    trust_tiers: dict[str, int]
    types: dict[str, int]
    statuses: dict[str, int]


def status_counts(conn: sqlite3.Connection) -> StatusCounts:
    """Aggregate note, tier, type, status, raw-queue and dead-link counts (§4.5)."""
    total_wiki = conn.execute("SELECT COUNT(*) FROM notes WHERE layer = 'wiki';").fetchone()[0]
    total_raw = conn.execute("SELECT COUNT(*) FROM notes WHERE layer = 'raw';").fetchone()[0]
    unprocessed_raw = len(unprocessed_raw_paths(conn))
    broken_links = conn.execute("SELECT COUNT(*) FROM links WHERE target_cid IS NULL;").fetchone()[0]

    cur = conn.execute("SELECT layer, COUNT(*) as cnt FROM notes WHERE layer != 'raw' GROUP BY layer;")
    domain_counts = {r["layer"]: r["cnt"] for r in cur.fetchall()}

    cur = conn.execute("SELECT trust_tier, COUNT(*) as cnt FROM notes WHERE layer != 'raw' GROUP BY trust_tier;")
    tier_counts = {r["trust_tier"] or "unverified": r["cnt"] for r in cur.fetchall()}

    cur = conn.execute("SELECT type, COUNT(*) as cnt FROM notes WHERE layer != 'raw' GROUP BY type;")
    type_counts = {r["type"] or "unknown": r["cnt"] for r in cur.fetchall()}

    cur = conn.execute("SELECT status, COUNT(*) as cnt FROM notes WHERE layer != 'raw' GROUP BY status;")
    status_counts_ = {r["status"] or "unknown": r["cnt"] for r in cur.fetchall()}

    return StatusCounts(
        total_notes=sum(domain_counts.values()),
        total_wiki=total_wiki,
        domains=domain_counts,
        total_raw=total_raw,
        unprocessed_raw=unprocessed_raw,
        broken_links=broken_links,
        verification_debt=tier_counts.get("stale-verified", 0),
        trust_tiers=tier_counts,
        types=type_counts,
        statuses=status_counts_,
    )

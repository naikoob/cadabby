"""Deterministic epistemic linting executing the six normative gates.

Conforms strictly to Cadabby Technical Specification §6.3.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote

from cadabby.cache import VaultCache
from cadabby.constants import (
    DIR_RAW,
    DIR_WIKI,
    FILE_AGENTS,
    NOTE_STATUSES,
    REQUIRED_FRONTMATTER_FIELDS,
)
from cadabby.domain import DomainDefinition, iter_fence_states
from cadabby.frontmatter import FrontmatterParseError, parse_frontmatter
from cadabby.graph import (
    LINK_KIND_MARKDOWN,
    LinkTargetIndex,
    iter_markdown_link_targets,
    relative_note_path,
)
from cadabby.okf import (
    is_actor_human,
    is_canonical_tag,
    is_valid_actor,
    is_valid_timestamp,
    tag_has_whitespace,
)
from cadabby.vault import Vault, cid_to_path


@dataclass
class LintFinding:
    code: str
    severity: str  # "error" | "warning"
    rel_path: str
    line: int | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        """Convert LintFinding to serializable dictionary."""
        return {
            "code": self.code,
            "severity": self.severity,
            "rel_path": self.rel_path,
            "line": self.line,
            "message": self.message,
        }


def _error(*, code: str, rel_path: str, message: str, line: int | None = None) -> LintFinding:
    return LintFinding(code=code, severity="error", rel_path=rel_path, line=line, message=message)


def _warning(*, code: str, rel_path: str, message: str, line: int | None = None) -> LintFinding:
    return LintFinding(code=code, severity="warning", rel_path=rel_path, line=line, message=message)


@dataclass(frozen=True, slots=True)
class _NoteRow:
    """A non-raw `notes` row: everything the per-note gates need, already parsed by the scan."""

    cid: str
    rel_path: str
    layer: str
    body: str
    frontmatter: dict[str, Any] | None  # None when parse_error is set
    parse_error: str | None
    body_hash: str | None
    trust_tier: str | None


def run_vault_lint(vault: Vault, cache: VaultCache | None = None) -> list[LintFinding]:
    """Execute the six normative lint gates against the vault.

    1. Schema Integrity
    2. Layout Consistency
    3. Link Consistency
    4. Provenance Check
    5. Graph Connectivity
    6. Verification Integrity
    """
    if cache is not None:
        return _run_vault_lint_impl(vault, cache)
    with VaultCache(vault) as local_cache:
        return _run_vault_lint_impl(vault, local_cache)


_INLINE_CODE_RE = re.compile(r"`+([^`\n]+)`+")


def _extract_headings(body: str) -> list[str]:
    """Extract markdown ATX headings outside fenced code blocks, unwrapping inline code backticks."""
    unfenced = "\n".join(
        "" if in_fence else line for line, in_fence in iter_fence_states(body.split("\n"))
    )
    raw_headings = re.findall(r"^#{1,6}\s+(.+)$", unfenced, flags=re.MULTILINE)
    return [
        _INLINE_CODE_RE.sub(r"\1", re.sub(r"\s+#+\s*$", "", h.strip()))
        for h in raw_headings
    ]


def _heading_slug(text: str) -> str:
    """GitHub-style heading anchor: lowercase, punctuation dropped, spaces to hyphens."""
    unwrapped = _INLINE_CODE_RE.sub(r"\1", text.strip())
    return re.sub(r"[^\w\- ]", "", unwrapped.lower()).replace(" ", "-")


def _anchor_matches(anchor: str, headings: list[str]) -> bool:
    """Whether a link anchor names one of the headings.

    Accepts the heading text as written (Obsidian), its space/hyphen-normalized
    form, and the GitHub slug, so `#b-tree-indexes-v2` finds `## B-Tree Indexes (v2)`.
    """
    a = _INLINE_CODE_RE.sub(r"\1", unquote(anchor).strip())
    a_norm = re.sub(r"[\s-]+", " ", a.lower())
    a_slug = _heading_slug(a)
    for h in headings:
        if a.lower() == h.strip().lower() or a_norm == re.sub(r"[\s-]+", " ", h.strip().lower()):
            return True
        if a_slug and a_slug == _heading_slug(h):
            return True
    return False


def _markdown_path_hint(source_cid: str, target_raw: str, resolver: LinkTargetIndex) -> str:
    """Suggest the relative path that would resolve, when the stem names exactly one note."""
    path_part = unquote(target_raw.split("#", 1)[0])
    candidate = resolver.suggest(path_part)
    if candidate is None:
        return ""
    suggestion = relative_note_path(source_cid, candidate)
    anchor = target_raw.partition("#")[2]
    if anchor:
        suggestion = f"{suggestion}#{anchor}"
    return f"; did you mean '{suggestion}'?"


def _raw_link_findings(vault: Vault, notes: Iterable[_NoteRow]) -> Iterator[LintFinding]:
    """Gate 4: relative Markdown links into raw/ must resolve to a raw/ file (§6.3).

    AGENTS.md tells agents to cite raw evidence in-body with a Markdown link
    relative to the citing note. Those links are never graph edges (gate 3
    takes note links only), so a link with one `../` too many would pass lint
    while the citation a reader clicks was dead. Only links whose path names a
    `raw` segment are checked here: other relative links are outside provenance.
    """
    raw_root = vault.abs_path(DIR_RAW).resolve()
    for note in notes:
        note_dir = vault.abs_path(note.rel_path).parent
        seen: set[str] = set()
        for target in iter_markdown_link_targets(note.body):
            if target in seen:
                continue
            seen.add(target)
            path_part = unquote(target.split("#", 1)[0])
            parts = PurePosixPath(path_part).parts
            if DIR_RAW not in parts:
                continue
            resolved = (note_dir / path_part).resolve()
            if resolved.is_file() and resolved.is_relative_to(raw_root):
                continue
            # Suggest the path that would work: everything after the last `raw`
            # segment, re-rooted at raw/ and made relative to the note's folder.
            tail = parts[len(parts) - 1 - parts[::-1].index(DIR_RAW) + 1 :]
            candidate = raw_root.joinpath(*tail) if tail else None
            hint = ""
            if candidate is not None and candidate.is_file():
                suggestion = Path(os.path.relpath(candidate, note_dir)).as_posix()
                hint = f"; did you mean '{suggestion}'?"
            yield _error(
                code="RAW_LINK_BROKEN",
                rel_path=note.rel_path,
                message=f"Markdown link '{target}' does not resolve to a file in raw/{hint}",
            )


def _run_vault_lint_impl(vault: Vault, cache: VaultCache) -> list[LintFinding]:
    # The scan is the only pass that reads and parses note files. Every gate
    # below reads its rows, so lint costs one incremental scan instead of a
    # second full read and parse of the vault.
    cache.scan()
    conn = cache.get_connection()
    domains = vault.discover_domains()
    notes = _load_note_rows(conn)
    notes_by_cid = {note.cid: note for note in notes}

    findings: list[LintFinding] = list(_manifest_findings(vault, domains))  # Gate 1 (domain manifests)
    for note in notes:
        findings.extend(_note_findings(note, domains[note.layer]))  # Gates 1, 2, 4 (domain), 6
    findings.extend(_link_findings(conn, cache, notes_by_cid))  # Gate 3
    findings.extend(_source_findings(conn))  # Gate 4
    findings.extend(_raw_link_findings(vault, notes))  # Gate 4 (in-body citations)
    findings.extend(_orphan_findings(conn))  # Gate 5
    return findings


def _manifest_findings(vault: Vault, domains: dict[str, DomainDefinition]) -> Iterator[LintFinding]:
    """A `{domain}/AGENTS.md` that cannot be honored (§2.2).

    Without this a typo such as flow-style `allowed_types` silently opened the
    domain to every type, and nothing anywhere said its contract had lapsed.
    """
    for domain_def in domains.values():
        if domain_def.manifest_error:
            yield _error(
                code="DOMAIN_MANIFEST_INVALID",
                rel_path=vault.rel_path(domain_def.path / FILE_AGENTS),
                message=f"Domain manifest not honored: {domain_def.manifest_error}",
            )


def _load_note_rows(conn: sqlite3.Connection) -> list[_NoteRow]:
    """Every non-raw note the scan indexed, ordered by path so findings are deterministic."""
    rows = conn.execute(
        """
        SELECT cid, rel_path, layer, body, frontmatter_json, parse_error, body_hash, trust_tier
        FROM notes
        WHERE layer != 'raw'
        ORDER BY rel_path;
        """
    ).fetchall()
    return [
        _NoteRow(
            cid=r["cid"],
            rel_path=r["rel_path"],
            layer=r["layer"],
            body=r["body"] or "",
            # The §3.2 subset yields only str/bool/None scalars, lists and
            # mappings, so the JSON round-trip returns exactly what was parsed.
            frontmatter=json.loads(r["frontmatter_json"]) if r["frontmatter_json"] is not None else None,
            parse_error=r["parse_error"],
            body_hash=r["body_hash"],
            trust_tier=r["trust_tier"],
        )
        for r in rows
    ]


def _note_findings(note: _NoteRow, domain_def: DomainDefinition) -> list[LintFinding]:
    """The per-note gates: 1 (schema), 2 (layout), 4 (domain provenance) and 6 (verification)."""
    # Gate 2 reads only the path, so it runs even when the frontmatter does
    # not: a nested wiki note must not hide behind a broken header (C11).
    layout = list(_layout_findings(note.rel_path, domain_def))
    if note.parse_error is not None or note.frontmatter is None:
        return [_unparseable_finding(note), *layout]
    fm = note.frontmatter
    if not fm:
        return [
            _error(code="FRONTMATTER_UNPARSEABLE", rel_path=note.rel_path, line=1, message="Missing frontmatter block"),
            *layout,
        ]
    return [
        *_schema_findings(note.rel_path, fm, domain_def),
        *layout,
        *_domain_provenance_findings(note.rel_path, fm, domain_def),
        *_verification_findings(note.rel_path, fm, note.body_hash, note.trust_tier),
    ]


def _unparseable_finding(note: _NoteRow) -> LintFinding:
    """Gate 1 for a note whose frontmatter the scan could not parse.

    The cache keeps the full file content for such a row, so re-parsing it
    recovers the parser's line number without touching disk. Only broken
    notes pay for the second parse.
    """
    try:
        parse_frontmatter(note.body)
    except FrontmatterParseError as e:
        return _error(code="FRONTMATTER_UNPARSEABLE", rel_path=note.rel_path, line=e.line_number, message=e.message)
    # Reached only if the parser changed between the scan and this call.
    return _error(code="FRONTMATTER_UNPARSEABLE", rel_path=note.rel_path, message=note.parse_error or "Unparseable")


def _is_missing_required(val: Any) -> bool:
    return val is None or (isinstance(val, str) and not val.strip())


def _schema_findings(rel_path: str, fm: dict[str, Any], domain_def: DomainDefinition) -> Iterator[LintFinding]:
    """Gate 1: required fields, the type and status enums, tag grammar and timestamps."""
    missing = {f for f in REQUIRED_FRONTMATTER_FIELDS if _is_missing_required(fm.get(f))}
    for field_name in REQUIRED_FRONTMATTER_FIELDS:
        if field_name in missing:
            yield _error(code="FIELD_MISSING", rel_path=rel_path, message=f"Missing required field '{field_name}'")
        elif field_name in ("title", "description") and not isinstance(fm[field_name], str):
            yield _error(
                code="ENUM_INVALID",
                rel_path=rel_path,
                message=f"Invalid {field_name} {fm[field_name]!r}. Must be a non-empty string",
            )

    if "type" not in missing:
        note_type = fm["type"]
        allowed = domain_def.allowed_types
        if allowed is not None and (not isinstance(note_type, str) or note_type not in allowed):
            yield _error(code="ENUM_INVALID", rel_path=rel_path, message=f"Invalid type '{note_type}'. Must be one of {allowed}")
        elif allowed is None and not isinstance(note_type, str):
            yield _error(code="ENUM_INVALID", rel_path=rel_path, message=f"Invalid type '{note_type}'. Type must be a non-empty string")

    if "status" not in missing and (not isinstance(fm["status"], str) or fm["status"] not in NOTE_STATUSES):
        yield _error(code="ENUM_INVALID", rel_path=rel_path, message=f"Invalid status '{fm['status']}'. Must be one of {NOTE_STATUSES}")

    yield from _tag_findings(rel_path, fm.get("tags"))
    yield from _timestamp_findings(rel_path, fm)


def _tag_findings(rel_path: str, note_tags: Any) -> Iterator[LintFinding]:
    """Tag grammar (§3.1).

    Whitespace is an error because it corrupts the space-joined `--tag` facet
    (§4.2); a merely non-canonical tag is a warning because the canonicalizing
    writer repairs it (§3.2).
    """
    if note_tags is None:
        return
    if not isinstance(note_tags, list):
        yield _error(
            code="TAG_MALFORMED",
            rel_path=rel_path,
            message=f"'tags' must be a sequence of strings, got {type(note_tags).__name__}",
        )
        return
    for tag in note_tags:
        if not isinstance(tag, str):
            yield _error(code="TAG_MALFORMED", rel_path=rel_path, message=f"Invalid tag {tag!r}. Tags must be strings")
        elif tag_has_whitespace(tag):
            yield _error(
                code="TAG_MALFORMED",
                rel_path=rel_path,
                message=(
                    f"Invalid tag '{tag}'. Tags must not contain whitespace, "
                    "which breaks --tag filtering; use kebab-case"
                ),
            )
        elif not is_canonical_tag(tag):
            yield _warning(
                code="TAG_MALFORMED",
                rel_path=rel_path,
                message=(
                    f"Non-canonical tag '{tag}'. Canonical form is lowercase "
                    "kebab-case, optionally '/'-nested (e.g. 'trust/human-reviewed')"
                ),
            )


def _timestamp_findings(rel_path: str, fm: dict[str, Any]) -> Iterator[LintFinding]:
    """RFC 3339 checks on `generated.at` and every `verified[i].at`."""
    gen_block = fm.get("generated")
    if isinstance(gen_block, dict):
        gen_at = gen_block.get("at")
        if gen_at and not is_valid_timestamp(gen_at):
            yield _error(
                code="TIMESTAMP_INVALID",
                rel_path=rel_path,
                message=f"Invalid RFC 3339 timestamp in generated.at: '{gen_at}'",
            )

    verified_entries = fm.get("verified", [])
    if isinstance(verified_entries, list):
        for v_idx, v_entry in enumerate(verified_entries):
            if isinstance(v_entry, dict):
                v_at = v_entry.get("at")
                if v_at and not is_valid_timestamp(v_at):
                    yield _error(
                        code="TIMESTAMP_INVALID",
                        rel_path=rel_path,
                        message=f"Invalid RFC 3339 timestamp in verified[{v_idx}].at: '{v_at}'",
                    )


def _layout_findings(rel_path: str, domain_def: DomainDefinition) -> Iterator[LintFinding]:
    """Gate 2: the wiki domain is flat; other domains nest freely.

    Compares the whole parent path rather than the parent folder's name, which
    let `wiki/x/wiki/N.md` through.
    """
    parent = PurePosixPath(rel_path).parent
    if domain_def.name == DIR_WIKI and parent != PurePosixPath(DIR_WIKI):
        yield _error(
            code="WIKI_NESTING_DISALLOWED",
            rel_path=rel_path,
            message=f"Note is nested in '{parent}/'. The wiki domain must be completely flat.",
        )


def _domain_provenance_findings(
    rel_path: str, fm: dict[str, Any], domain_def: DomainDefinition
) -> Iterator[LintFinding]:
    """Gate 4: a domain declaring `require_sources: true` needs a non-empty `sources:` list."""
    if not domain_def.require_sources:
        return
    sources = fm.get("sources")
    if not sources or not isinstance(sources, list):
        yield _error(
            code="SOURCE_MISSING",
            rel_path=rel_path,
            message=f"Domain '{domain_def.name}' requires 'sources:' provenance list",
        )


def _verification_findings(
    rel_path: str, fm: dict[str, Any], body_hash: str | None, tier: str | None
) -> Iterator[LintFinding]:
    """Gate 6: actor grammar, `of:` binding, and stale attestations nothing has superseded.

    `body_hash` and `tier` are the scan's, computed from the same body and
    `verified:` list this function walks.
    """
    verified_entries = fm.get("verified", [])
    if not isinstance(verified_entries, list):
        return
    warned_stale_buckets: set[str] = set()
    for v_entry in verified_entries:
        if not isinstance(v_entry, dict):
            continue
        actor = v_entry.get("by")
        of_hash = v_entry.get("of")

        if not actor or not is_valid_actor(actor):
            yield _error(code="ACTOR_MALFORMED", rel_path=rel_path, message=f"Malformed actor identity: '{actor}'")

        if not isinstance(of_hash, str) or not of_hash.strip():
            yield _error(
                code="VERIFICATION_UNBOUND",
                rel_path=rel_path,
                message=f"Verification entry by '{actor}' missing 'of:' body hash binding",
            )
        elif of_hash != body_hash:
            actor_is_human = isinstance(actor, str) and is_actor_human(actor)
            superseded = tier == "human-reviewed" or (tier == "machine-confirmed" and not actor_is_human)
            stale_bucket = "human" if actor_is_human else "machine"
            if not superseded and stale_bucket not in warned_stale_buckets:
                warned_stale_buckets.add(stale_bucket)
                yield _warning(
                    code="VERIFICATION_STALE",
                    rel_path=rel_path,
                    message=f"Verification entry by '{actor}' is stale (hash drifted from {of_hash[:16]}...)",
                )


def _link_findings(
    conn: sqlite3.Connection, cache: VaultCache, notes_by_cid: dict[str, _NoteRow]
) -> Iterator[LintFinding]:
    """Gate 3: wikilinks and relative Markdown note links resolve, and their anchors exist."""
    resolver = cache.get_link_resolver()
    headings_cache: dict[str, list[str]] = {}
    for link_row in conn.execute("SELECT source_cid, target_raw, target_cid, anchor, kind FROM links;").fetchall():
        source_cid = link_row["source_cid"]
        target_raw = link_row["target_raw"]
        target_cid = link_row["target_cid"]
        anchor = link_row["anchor"]
        rel_path = cid_to_path(source_cid)

        if target_cid is None:
            if link_row["kind"] == LINK_KIND_MARKDOWN:
                hint = _markdown_path_hint(source_cid, target_raw, resolver)
                message = f"Dead Markdown link '{target_raw}'{hint}"
            else:
                message = f"Dead wikilink [[{target_raw}]]"
            yield _error(code="LINK_DEAD", rel_path=rel_path, message=message)
        elif anchor:
            target_note = notes_by_cid.get(target_cid)
            if target_note:
                headings = headings_cache.get(target_cid)
                if headings is None:
                    headings = _extract_headings(target_note.body)
                    headings_cache[target_cid] = headings
                if not _anchor_matches(anchor, headings):
                    yield _warning(
                        code="ANCHOR_MISSING",
                        rel_path=rel_path,
                        message=f"Anchor '#{anchor}' not found in target [[{target_cid}]]",
                    )


def _source_findings(conn: sqlite3.Connection) -> Iterator[LintFinding]:
    """Gate 4: every `sources:` entry exists under raw/."""
    for src_row in conn.execute("SELECT source_cid, raw_path FROM sources WHERE resolved = 0;").fetchall():
        yield _error(
            code="SOURCE_MISSING",
            rel_path=cid_to_path(src_row["source_cid"]),
            message=f"Referenced source does not exist: '{src_row['raw_path']}'",
        )


def _orphan_findings(conn: sqlite3.Connection) -> Iterator[LintFinding]:
    """Gate 5: canonical wiki notes with zero inbound and zero outbound links."""
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
    for row in rows:
        yield _warning(
            code="NOTE_ORPHAN",
            rel_path=row["rel_path"],
            message="Orphan note: 0 inbound and 0 outbound links",
        )

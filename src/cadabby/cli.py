"""Command Line Interface (CLI) for Cadabby.

Conforms strictly to Cadabby Technical Specification §5.3.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

from cadabby.adapters.disk_storage import DiskNoteStorage
from cadabby.audit import run_vault_audit
from cadabby.cache import VaultCache, reset_cache_files
from cadabby.constants import DEFAULT_LIMIT, DIR_RAW, DIR_WIKI, NOTE_STATUSES, NOTE_TYPES, TRUST_TIERS
from cadabby.errors import (
    EXIT_ENVIRONMENT,
    EXIT_FINDINGS,
    EXIT_OK,
    InvalidArgumentError,
    NotFoundError,
    classify,
    exit_code_for,
)
from cadabby.indexer import rotate_vault_log, sync_vault_index
from cadabby.installer import InitRefusedError, init_vault
from cadabby.lint import run_vault_lint
from cadabby.okf import actor_name, is_actor_human, is_valid_actor
from cadabby.ops import (
    graph_view,
    ground_notes,
    move_note,
    raw_sources_match,
    resolve_note_cid,
    scaffold_note_result,
    search_notes,
    update_note_result,
    vault_status,
    verify_note,
)
from cadabby.vault import Vault


def resolve_cli_vault(args: argparse.Namespace) -> Vault:
    """Resolve vault from CLI arguments or environment, failing fast if not inside a vault.

    An explicit --vault names the root exactly. Without it, CADABBY_VAULT names
    the root exactly if set (and errors if it is not a vault); otherwise the
    vault is discovered by walking up from the cwd.
    """
    vault_arg = getattr(args, "vault", None)
    if vault_arg is not None:
        return Vault.at(vault_arg)
    return Vault.open()


CLI_DEFAULT_ACTOR = "process:cli"


def _default_cli_actor(args: argparse.Namespace) -> str:
    """The actor a CLI write records: `--actor` if given, else `process:cli` (§5.3).

    Never inferred from the terminal. Agents run shell commands in
    pseudo-terminals, so `isatty()` is no evidence of a person, and inferring
    `human:<user>` from it let an agent's flagless call sign as a human. The
    only path to a `human:*` attestation is `verify --human`.
    """
    actor = getattr(args, "actor", None) or CLI_DEFAULT_ACTOR
    try:
        return parse_cli_actor(actor)  # programmatic callers bypass argparse's `type=`
    except argparse.ArgumentTypeError as e:
        raise InvalidArgumentError(str(e)) from None


def parse_cli_actor(value: str) -> str:
    """argparse `type=` for `--actor`: the §3.4 grammar, and never `human:*` (§3.5).

    Unchecked, `--actor` went straight into `generated.by` and log.md: a
    newline forged ledger lines, and `human:alice` claimed a person's
    authorship with no person present.
    """
    if not is_valid_actor(value):
        raise argparse.ArgumentTypeError(f"invalid actor {value!r}: expected agent:<id> or process:<id>")
    if is_actor_human(value):
        raise argparse.ArgumentTypeError(
            f"{value!r}: a human:* actor is recorded only by `cadabby verify --human`, never claimed by a flag"
        )
    return value


def cmd_init(args: argparse.Namespace) -> int:
    """Scaffold a fresh vault structure conforming to §2.1, §7.6 and §9."""
    raw_target = getattr(args, "target_path", None) or getattr(args, "vault", None)
    target_dir = Path(raw_target).resolve() if raw_target else Path.cwd()
    try:
        report = init_vault(
            target_dir,
            vault_name=args.name,
            force=getattr(args, "force", False),
            obsidian=args.obsidian,
            obsidian_templates=getattr(args, "obsidian_templates", False),
        )
    except InitRefusedError as e:
        info = classify(e)
        print(f"Error [{info.code}]: {info.message}", file=sys.stderr)
        return EXIT_ENVIRONMENT
    print("\n".join(report))
    return EXIT_OK


def cmd_sync(args: argparse.Namespace) -> int:
    """Run incremental cache scan and regenerate the index.md gap report."""
    vault = resolve_cli_vault(args)
    rebuild = getattr(args, "rebuild", False)
    if rebuild:
        reset_cache_files(vault.cache_db_path)

    with VaultCache(vault) as cache:
        # sync regenerates unconditionally (§4.3), so the scan must not do it too.
        ins, upd, deleted, total = cache.scan(force=(args.force or rebuild), regenerate_index=False)
        sync_vault_index(vault, cache=cache)
        rotated = rotate_vault_log(vault)

    msg = f"Vault sync complete: {total} total tracked ({ins} inserted, {upd} updated, {deleted} deleted)."
    if rotated:
        msg += f" (Rotated active log to {vault.rel_path(rotated)})"
    print(msg)
    return EXIT_OK


def cmd_search(args: argparse.Namespace) -> int:
    """Search vault notes with normalized epistemic BM25 ranking."""
    vault = resolve_cli_vault(args)
    with VaultCache(vault) as cache:
        results = search_notes(
            vault,
            cache,
            query=args.query,
            type_=args.type,
            status=args.status,
            trust=args.trust,
            tag=args.tag,
            domain=getattr(args, "domain", None),
            limit=args.limit,
        )
        # Unfiltered search excludes raw/ by design (§4.4), so "no results"
        # can hide the one place the term occurs. Only an unscoped text search
        # gets the hint: a filter is a deliberate narrowing, and --json must
        # stay the bare list vault_search returns.
        scoped = any((args.type, args.status, args.trust, args.tag, getattr(args, "domain", None)))
        raw_hit = not results and not scoped and not args.json and raw_sources_match(cache, args.query)

    if args.json:
        out = [r.to_dict() for r in results]
        print(json.dumps(out, indent=2))
        return EXIT_OK

    label = f"'{args.query}'" if args.query else "active filters"
    if not results:
        print(f"No results found matching {label}.")
        if raw_hit:
            print(f"Sources in {DIR_RAW}/ match; search them with --domain {DIR_RAW}.")
        return EXIT_OK

    cid_w = max(42, max(len(r.cid) for r in results) + 2)
    print(f"\nFound {len(results)} matches for {label}:\n")
    print(f"{'CID':<{cid_w}} {'TRUST':<18} {'STATUS':<12} {'SCORE':<8} {'TITLE'}")
    print("-" * (cid_w + 58))
    for r in results:
        tier = r.trust_tier or "unverified"
        st = r.status or "active"
        title = (r.title or "")[:35]
        print(f"{r.cid:<{cid_w}} {tier:<18} {st:<12} {r.score:<8.3f} {title}")
    print()
    return EXIT_OK


def cmd_ground(args: argparse.Namespace) -> int:
    """Retrieve full content and 1-hop graph for specified CIDs."""
    vault = resolve_cli_vault(args)
    with VaultCache(vault) as cache:
        grounded = ground_notes(vault, args.targets, budget_tokens=args.budget_tokens, cache=cache)

    if args.json:
        print(json.dumps(grounded, indent=2))
        return EXIT_OK

    for item in grounded:
        if "error" in item:
            # A target that could not be grounded is reported in place (§5.1);
            # the others still print. Nothing grounded at all raised above.
            err = item["error"]
            print(f"Error [{err['code']}] {item['target']}: {err['message']}", file=sys.stderr)
            continue
        print("=" * 80)
        if item["kind"] == "raw":
            sec_suffix = f" [Section: {item['section']}]" if item.get("section") else ""
            print(f"Source: {item['path']} (raw evidence, no trust tier){sec_suffix}")
            if item.get("section_error"):
                print(f"Note: {item['section_error']}; showing the whole source.")
            cited = ", ".join(f"{c['cid']} ({c['trust_tier']})" for c in item["cited_by"]) or "(no note cites it yet)"
            print(f"Cited by: {cited}")
            print("-" * 80)
            print(item["content"] if not item["filename_only"] else "(filename only: the engine cannot read this format)")
            print()
            continue
        sec_suffix = f" [Section: {item['section']}]" if item.get("section") else ""
        print(f"CID: {item['cid']} ({item['trust_tier']}){sec_suffix}")
        if item.get("section_error"):
            print(f"Note: {item['section_error']}; showing the whole note.")
        print(f"Title: {item['title']} | Type: {item['type']} | Status: {item['status']}")
        print(f"Links ({len(item['links'])}): {', '.join(l['target'] for l in item['links'])}")
        print(f"Backlinks ({len(item['backlinks'])}): {', '.join(b['source_cid'] for b in item['backlinks'])}")
        print(f"Sources: {', '.join(s['path'] for s in item['sources'])}")
        print("-" * 80)
        print(item["content"])
        print()
    return EXIT_OK


def cmd_scaffold(args: argparse.Namespace) -> int:
    """Scaffold a new note."""
    vault = resolve_cli_vault(args)
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None
    raw_aliases = getattr(args, "aliases", None)
    aliases = [a.strip() for a in raw_aliases.split(",") if a.strip()] if raw_aliases else None
    sources = [s.strip() for s in args.sources.split(",") if s.strip()] if args.sources else None
    raw_fm = getattr(args, "frontmatter", None)
    frontmatter = (
        _parse_json_flag(raw_fm, "--frontmatter", dict, "object")
        if raw_fm
        else None
    )

    with VaultCache(vault) as cache:
        result = scaffold_note_result(
            vault=vault,
            title=args.title,
            type_=args.type,
            description=args.desc,
            tags=tags,
            sources=sources,
            body=getattr(args, "body", None) or "",
            actor=_default_cli_actor(args),
            domain=getattr(args, "domain", DIR_WIKI),
            path=getattr(args, "path", None),
            template=getattr(args, "template", None),
            status=getattr(args, "status", None) or "draft",
            aliases=aliases,
            frontmatter=frontmatter,
            cache=cache,
        )
    return _report_write(args, result.to_dict(), f"Scaffolded note: {result.rel_path}")


def _report_write(args: argparse.Namespace, payload: dict[str, Any], line: str) -> int:
    """Print a write's result: the MCP payload with --json, else one line; warnings go to stderr."""
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    else:
        print(line)
    for warning in payload.get("warnings", []):
        print(f"Warning: {warning}", file=sys.stderr)
    return EXIT_OK


def _parse_json_flag(raw: str, flag_name: str, expected_type: type, type_label: str) -> Any:
    """Parse a JSON CLI flag and validate its top-level type."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidArgumentError(f"Invalid JSON for {flag_name}: {exc.msg}") from exc
    if not isinstance(parsed, expected_type):
        raise InvalidArgumentError(
            f"Expected JSON {type_label} for {flag_name}, got {type(parsed).__name__}"
        )
    return parsed


def cmd_update(args: argparse.Namespace) -> int:
    """Patch note frontmatter and apply ordered body edits, as `vault_update_note` (§5.3)."""
    vault = resolve_cli_vault(args)
    patch = (
        _parse_json_flag(args.patch_frontmatter, "--patch-frontmatter", dict, "object")
        if args.patch_frontmatter
        else None
    )

    raw_edits = getattr(args, "edits", None)
    edits = (
        _parse_json_flag(raw_edits, "--edits", list, "array")
        if raw_edits
        else None
    )

    with VaultCache(vault) as cache:
        result = update_note_result(
            vault=vault,
            cid_or_path=args.cid,
            frontmatter_patch=patch,
            expected_hash=args.expected_hash,
            actor=_default_cli_actor(args),
            edits=edits,
            cache=cache,
        )
    return _report_write(args, result.to_dict(), f"Updated note: {result.rel_path} ({result.source_hash[:12]})")


def cmd_move(args: argparse.Namespace) -> int:
    """Move or rename a note and repoint its links; prints the plan unless --apply."""
    vault = resolve_cli_vault(args)
    with VaultCache(vault) as cache:
        result = move_note(
            vault,
            args.cid,
            args.to,
            apply=args.apply,
            approve_stale=args.approve_stale or [],
            plan_token=getattr(args, "plan_token", None),
            actor=_default_cli_actor(args),
            cache=cache,
        )
    if args.json:
        print(json.dumps(result, indent=2))
        return EXIT_OK

    verb = "Moved" if result["applied"] else "Would move"
    print(f"{verb} {result['from']} -> {result['to']}")
    moved_list = result.get("moved_notes") or [result["moved_note"]]
    for moved in moved_list:
        if len(moved_list) > 1 or moved["links_rewritten"] or moved["tier_before"] != moved["tier_after"]:
            print(f"  {moved['cid']}: {moved['links_rewritten']} own link(s), {moved['tier_before']} -> {moved['tier_after']}")
    for n in result["notes"]:
        print(f"  {n['cid']}: {n['links_rewritten']} link(s), {n['tier_before']} -> {n['tier_after']}")
    for line in result["newly_resolved"]:
        print(f"  note: {line}")
    for line in result["warnings"]:
        print(f"  warning: {line}")
    for line in result["blocked"]:
        print(f"  BLOCKED: {line}")
    if not result["applied"]:
        needs = result.get("needs_approval") or []
        if result["blocked"] and len(result["blocked"]) == len(needs):
            approvals = " ".join(f"--approve-stale {c}" for c in needs)
            print(
                f"Dry run: nothing written. Once the user agrees, re-run with "
                f"--apply {approvals} --plan-token {result['plan_token']}"
            )
        elif result["blocked"]:
            print("Dry run: resolve the blockers above first.")
        else:
            print(f"Dry run: nothing written. Re-run with --apply --plan-token {result['plan_token']} to move.")
    return EXIT_OK


def _confirm_human_attestation(vault: Vault, cid: str, actor: str) -> tuple[bool, str | None]:
    """Ask the person at the keyboard to type back a one-time token (§3.5).

    The TTY check alone is satisfied by any pseudo-terminal, which is exactly
    what agent harnesses run commands in. A token printed now and typed back
    now cannot be answered by a pre-piped `y`, so an endorsement can no longer
    be made by accident.

    Returns (confirmed, the body hash shown). The stamp is bound to that hash:
    if the body changes while the person reads and types, nothing is signed.
    """
    storage = DiskNoteStorage(vault)
    try:
        note = storage.get_note(resolve_note_cid(storage, cid))
    except FileNotFoundError:
        note = None
    if note is None:
        # Let verify_note raise its normal NOT_FOUND error with the usual wording.
        return True, None
    token = secrets.token_hex(2)
    shown = note.body_hash
    print(f"You are about to record that you personally reviewed '{note.title}'.")
    print(f"  cid:  {note.cid}")
    print(f"  body: {shown}")
    print(f"  as:   {actor}")
    try:
        answer = input(f"Type {token} to sign, anything else to abort: ")
    except EOFError:
        return False, shown
    return answer.strip() == token, shown


def cmd_verify(args: argparse.Namespace) -> int:
    """Stamp a content-bound verification attestation."""
    vault = resolve_cli_vault(args)

    # The parser makes these exclusive (argparse owns exit 2, §5.4); this
    # guard is for programmatic callers that bypass it.
    if args.human and args.agent:
        raise InvalidArgumentError("--human and --agent are mutually exclusive")

    if args.human:
        if not sys.stdin.isatty():
            raise InvalidArgumentError("--human requires an interactive TTY")
        # Settled before the prompt: a person must never review and type the
        # token only to have the stamp refused. SSSD/AD usernames carry '@'
        # and Windows ones may carry spaces, so the name is reduced to the
        # actor grammar exactly as an MCP client name is (§3.5).
        login = getpass.getuser()
        name = actor_name(login)
        if not name:
            raise InvalidArgumentError(
                f"Cannot sign as OS user {login!r}: no letter or digit of it fits the actor grammar (§3.4)"
            )
        actor = f"human:{name}"
        confirmed, reviewed_hash = _confirm_human_attestation(vault, args.cid, actor)
        if not confirmed:
            raise InvalidArgumentError("Aborted: confirmation token did not match; nothing was written")
        is_human = True
    elif args.agent:
        actor = args.agent if args.agent.startswith(("agent:", "process:")) else f"agent:{args.agent}"
        is_human = False
        reviewed_hash = None
    else:
        actor = CLI_DEFAULT_ACTOR  # see _default_cli_actor
        is_human = False
        reviewed_hash = None

    with VaultCache(vault) as cache:
        res = verify_note(
            vault=vault,
            cid_or_path=args.cid,
            actor=actor,
            method=args.method or ("manual-review" if is_human else "automated-check"),
            is_human_authorized=is_human,
            cache=cache,
            expected_body_hash=reviewed_hash,
        )
    status_label = "already verified" if res.get("already_verified") else "verified"
    line = f"{status_label.capitalize()}: {res['cid']} as '{res['actor']}' -> tier: '{res['trust_tier']}' ({res['of'][:16]}...)"
    return _report_write(args, res, line)


def cmd_status(args: argparse.Namespace) -> int:
    """Print high-level vault status, distribution, and verification debt."""
    vault = resolve_cli_vault(args)
    with VaultCache(vault) as cache:
        status_data = vault_status(vault, cache, lint=getattr(args, "lint", False))

    if args.json:
        print(json.dumps(status_data, indent=2))
        return EXIT_OK

    print(f"\n=== Vault Status: {status_data['vault_name']} ===")
    print(f"Path: {vault.root}")
    print(
        f"Total Notes: {status_data['total_notes']} | Raw Sources: {status_data['total_raw']} "
        f"({status_data['unprocessed_raw']} unprocessed)"
    )
    fn_only = status_data.get("filename_only_raw", {})
    if fn_only:
        ext_list = ", ".join(f"{ext or '<no-ext>'}: {cnt}" for ext, cnt in fn_only.items())
        print(f"Filename-Only Raw (body not indexed): {sum(fn_only.values())} ({ext_list})")
    print(f"Verification Debt (stale): {status_data['verification_debt']}")
    print(f"Broken Links: {status_data['broken_links']}")
    counts = status_data["finding_counts"]
    details = "below" if "findings" in status_data else "cadabby status --lint"
    print(f"Lint Findings: {counts['error']} error(s), {counts['warning']} warning(s) (details: {details})")
    queues = status_data["queues"]
    debt = queues["verification_debt"]
    for label, queue in (
        ("Unprocessed Raw Sources", queues["unprocessed_raw"]),
        ("Unfiled Notes", queues["unfiled"]),
        ("Debt an Agent May Re-verify", debt["agent_payable"]),
        ("Debt Owed by a Human (cadabby verify <cid> --human)", debt["human_owed"]),
    ):
        if queue["total"]:
            more = f" (+{queue['total'] - len(queue['items'])} more)" if queue["total"] > len(queue["items"]) else ""
            print(f"\n{label}: {queue['total']}{more}")
            for item in queue["items"]:
                print(f"  - {item}")
    print("\nTrust Tiers:")
    for tier in TRUST_TIERS:
        print(f"  - {tier:<20}: {status_data['trust_tiers'].get(tier, 0)}")
    print("\nDomains:")
    for d, count in status_data.get("domains", {}).items():
        print(f"  - {d:<20}: {count}")
    print("\nTypes:")
    all_types = sorted(set(NOTE_TYPES) | set(status_data.get("types", {}).keys()))
    for t in all_types:
        print(f"  - {t:<20}: {status_data['types'].get(t, 0)}")
    print("\nStatuses:")
    for s, count in status_data["statuses"].items():
        print(f"  - {s:<20}: {count}")
    if "findings" in status_data:
        print()
        _print_findings(status_data["findings"])
    print()
    return EXIT_OK


def _print_findings(findings: list[dict[str, Any]]) -> None:
    """One line per lint finding, shared by `lint` and `status --lint`."""
    for f in findings:
        loc = f"{f['rel_path']}:{f['line']}" if f["line"] else f["rel_path"]
        sev = "ERROR" if f["severity"] == "error" else "WARN "
        print(f"[{sev}] {f['code']:<22} {loc:<45} {f['message']}")


def cmd_lint(args: argparse.Namespace) -> int:
    """Run six-gate epistemic linting."""
    vault = resolve_cli_vault(args)
    with VaultCache(vault) as cache:
        findings = run_vault_lint(vault, cache=cache)

    if args.json:
        out = [f.to_dict() for f in findings]
        print(json.dumps(out, indent=2))
        has_errors = any(f.severity == "error" for f in findings)
        return EXIT_FINDINGS if has_errors else EXIT_OK

    errors = [f for f in findings if f.severity == "error"]
    warnings = [f for f in findings if f.severity == "warning"]

    if not findings:
        print("Vault is clean. All six normative gates passed.")
        return EXIT_OK

    print(f"\nEpistemic Lint Results: {len(errors)} error(s), {len(warnings)} warning(s)\n")
    _print_findings([f.to_dict() for f in findings])
    print()

    return EXIT_FINDINGS if errors else EXIT_OK


def cmd_audit(args: argparse.Namespace) -> int:
    """Run Git provenance audit against human:* endorsements."""
    vault = resolve_cli_vault(args)
    findings, notice = run_vault_audit(vault, require_signed=args.require_signed)

    if args.json:
        # One shape on every path, so a script never branches on the type.
        out = {
            "notice": notice,
            "findings": [
                {
                    "code": f.code,
                    "rel_path": f.rel_path,
                    "line": f.line,
                    "actor": f.actor,
                    "commit": f.commit,
                    "message": f.message,
                }
                for f in findings
            ],
        }
        print(json.dumps(out, indent=2))
        return EXIT_FINDINGS if findings else EXIT_OK

    if notice:
        print(f"Notice: {notice}")
        return EXIT_OK

    if not findings:
        print("Audit clean: all human:* endorsements match Git author provenance.")
        return EXIT_OK

    print(f"\nAudit Findings: {len(findings)} issue(s) detected\n")
    for f in findings:
        where = f" ({f.actor} in {f.commit[:8]})" if f.actor and f.commit else (f" ({f.actor})" if f.actor else "")
        print(f"[{f.code}] {f.rel_path}:{f.line}{where} - {f.message}")
    print()
    return EXIT_FINDINGS


def cmd_graph(args: argparse.Namespace) -> int:
    """Link structure without note text, as `vault_graph` (§5.1, §5.3).

    `<target>` shows one note's neighborhood; `--mocs` shows every MOC's link
    counts and overlaps. Exactly one of the two, checked by the parser.
    """
    vault = resolve_cli_vault(args)
    mocs = getattr(args, "mocs", False)
    with VaultCache(vault) as cache:
        data = graph_view(
            vault,
            cache,
            view="mocs" if mocs else "neighborhood",
            target=None if mocs else args.target,
            limit=getattr(args, "limit", None),
        )

    if args.json:
        print(json.dumps(data, indent=2))
        return EXIT_OK
    if mocs:
        _print_moc_overview(data)
    else:
        _print_neighborhood(data)
    return EXIT_OK


def _more(data: dict[str, Any], key: str) -> str:
    shown, total = len(data[key]), data.get(f"{key}_total", len(data[key]))
    return f" (+{total - shown} more)" if total > shown else ""


def _tier(entry: dict[str, Any]) -> str:
    return f" [{entry['trust_tier'] or 'unverified'}]" if entry.get("title") is not None else ""


def _print_neighborhood(data: dict[str, Any]) -> None:
    note = data["note"]
    print(f"Graph for [[{note['cid']}]] ({note['type']}, {note['trust_tier']}):")
    sections: list[tuple[str, str, Any]] = [
        ("Forward Links (1-hop)", "forward_links",
         lambda e: f"  -> [[{e['target_cid'] or e['target_raw'] + ' (unresolved)'}]]{_tier(e)}"),
        ("Backlinks (1-hop)", "backlinks", lambda e: f"  <- [[{e['source_cid']}]]{_tier(e)}"),
        ("2-Hop Forward", "two_hop_forward", lambda e: f"  -> [[{e['target_cid']}]]{_tier(e)} (via [[{e['via']}]])"),
        ("2-Hop Backlinks", "two_hop_backlinks", lambda e: f"  <- [[{e['source_cid']}]]{_tier(e)} (via [[{e['via']}]])"),
        ("Co-Citations", "co_citations", lambda e: f"  * [[{e['cid']}]]{_tier(e)} ({e['count']} shared citations)"),
        ("Bibliographic Coupling", "bibliographic_coupling",
         lambda e: f"  * [[{e['cid']}]]{_tier(e)} ({e['count']} shared targets)"),
    ]
    for title, key, render in sections:
        print(f"\n{title}{_more(data, key)}:")
        for entry in data[key]:
            print(render(entry))
        if not data[key]:
            print("  (none)")


def _print_moc_overview(data: dict[str, Any]) -> None:
    print(f"Maps of Content: {data['mocs_total']}{_more(data, 'mocs')}")
    for m in data["mocs"]:
        print(f"  [[{m['cid']}]] {m['outgoing']} out, {m['incoming']} in [{m['trust_tier'] or 'unverified'}]")
    if not data["mocs"]:
        print("  (none)")
    print(f"\nOverlaps: {data['overlaps_total']}{_more(data, 'overlaps')}")
    for o in data["overlaps"]:
        print(f"  [[{o['a']}]] ~ [[{o['b']}]]: {o['shared']} shared, ratio {o['ratio']:.2f}")
    if not data["overlaps"]:
        print("  (none)")


def cmd_install(args: argparse.Namespace) -> int:
    """Install or uninstall Cadabby harness configurations (§7.6)."""
    from cadabby.installer import run_install

    dest_path = Path(args.path) if getattr(args, "path", None) else None
    is_global = getattr(args, "is_global", False)
    vault_path: Path | None = None
    vault_arg = getattr(args, "vault", None)

    if vault_arg is not None:
        # An explicit --vault binds, including under --global: "available
        # everywhere, always this vault" is a real request from a single-vault
        # user, and typing the vault is how they accept the tradeoff (a bound
        # plugin must be a copy, because mcp_config.json lives inside it and a
        # symlink would have nowhere to write it).
        vault_path = Vault.at(vault_arg).root
    elif is_global:
        # Bare --global is deliberately unbound, whatever directory it is run
        # from. Discovering the cwd vault here would bake one absolute path
        # into a config every workspace on the machine reads, so opening a
        # second vault would silently operate on the first -- the opposite of
        # the "multi-project universal access" --global exists to provide
        # (§7.6). Unbound, the server resolves the vault per launch via
        # CADABBY_VAULT or the working directory.
        vault_path = None
    elif dest_path is None:
        try:
            vault_path = Vault.open().root
        except FileNotFoundError:
            raise NotFoundError(
                "No Cadabby vault found in current working directory. "
                "Run inside a vault, pass --vault <path>, or pass --global for user-level installation."
            ) from None
    elif not args.uninstall:
        try:
            vault_path = Vault.open().root
        except FileNotFoundError:
            vault_path = None

    return run_install(
        antigravity=args.antigravity,
        claude=args.claude,
        all_targets=args.all,
        is_global=is_global,
        uninstall=args.uninstall,
        dry_run=args.dry_run,
        dest_path=dest_path,
        vault_path=vault_path,
        force=args.force,
    )


def cmd_mcp(args: argparse.Namespace) -> int:
    """Run the Model Context Protocol stdio server."""
    from cadabby.mcp import run_mcp_server

    vault = resolve_cli_vault(args)
    return run_mcp_server(vault)


class _StrictArgumentParser(argparse.ArgumentParser):
    """An ArgumentParser that never accepts abbreviated long options.

    With abbreviation on, `--for` silently means `--force` and `--exp` means
    `--expected-hash`, so a typo in an agent's shell call changes behavior
    instead of failing. `add_subparsers()` builds children with the parent's
    class, so setting it here covers every subcommand.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)


def _non_blank(text: str) -> str:
    """argparse type for a name that must say something: `""` is a usage error (exit 2)."""
    if not text.strip():
        raise argparse.ArgumentTypeError("must not be empty")
    return text


def _positive_int(text: str) -> int:
    """argparse type for counts: 1 or more. `--limit -1` once read as "no limit" by accident of SQLite."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{text}' is not an integer") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value


def build_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser."""
    vault_parent = _StrictArgumentParser(add_help=False)
    vault_parent.add_argument(
        "--vault",
        type=Path,
        default=argparse.SUPPRESS,
        help="Path to vault root (default: $CADABBY_VAULT, else discovered from cwd)",
    )

    parser = _StrictArgumentParser(
        prog="cadabby",
        description="Epistemically auditable LLM Wiki engine for humans and AI agents.",
    )
    parser.add_argument(
        "--vault",
        type=Path,
        default=None,
        help="Path to vault root (default: $CADABBY_VAULT, else discovered from cwd)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # The eight vault verbs, each mirroring the MCP tool of the same name (§5.3).
    # search
    p_search = subparsers.add_parser("search", parents=[vault_parent], help="Search vault notes")
    p_search.add_argument(
        "query",
        nargs="?",
        default="",
        help="Search query (optional when at least one filter flag is passed)",
    )
    p_search.add_argument(
        "--domain",
        help="Filter by cognitive domain (e.g. wiki, customers, projects); 'raw' searches unprocessed sources",
    )
    p_search.add_argument("--type", help="Filter by note type")
    p_search.add_argument("--status", help="Filter by note status")
    p_search.add_argument("--trust", help="Filter by trust tier")
    p_search.add_argument("--tag", help="Filter by tag")
    p_search.add_argument("--limit", type=_positive_int, default=DEFAULT_LIMIT, help=f"Maximum results, at least 1 (default: {DEFAULT_LIMIT})")
    p_search.add_argument("--json", action="store_true", help="Output as JSON")
    p_search.set_defaults(func=cmd_search)

    # ground
    p_ground = subparsers.add_parser("ground", parents=[vault_parent], help="Read notes (with 1-hop graph) or raw/ sources (with citing notes)")
    p_ground.add_argument(
        "targets",
        nargs="+",
        help="Note CIDs, stems, wikilinks, or raw/ paths (append #Heading to ground a single section)",
    )
    p_ground.add_argument(
        "--budget-tokens", "--budget", dest="budget_tokens", type=_positive_int, default=None, help="Token budget"
    )
    p_ground.add_argument("--json", action="store_true", help="Output as JSON")
    p_ground.set_defaults(func=cmd_ground)

    # status
    p_status = subparsers.add_parser("status", parents=[vault_parent], help="Report vault status and verification debt")
    p_status.add_argument("--lint", action="store_true", help="Include every lint finding, as vault_status(lint=true); exits 0")
    p_status.add_argument("--json", action="store_true", help="Output as JSON")
    p_status.set_defaults(func=cmd_status)

    # graph
    p_graph = subparsers.add_parser(
        "graph", parents=[vault_parent], help="Link structure without note text: a note's neighborhood, or --mocs"
    )
    graph_what = p_graph.add_mutually_exclusive_group(required=True)
    graph_what.add_argument("target", nargs="?", type=_non_blank, help="Note CID, stem, path or wikilink to centre on")
    graph_what.add_argument("--mocs", action="store_true", help="Every MOC's link counts and overlaps, for splits and merges")
    p_graph.add_argument("--limit", type=_positive_int, default=DEFAULT_LIMIT, help=f"Max entries per list, at least 1 (default: {DEFAULT_LIMIT})")
    p_graph.add_argument("--json", action="store_true", help="Output as JSON")
    p_graph.set_defaults(func=cmd_graph)

    # scaffold
    p_scaffold = subparsers.add_parser("scaffold", parents=[vault_parent], help="Scaffold a new note")
    p_scaffold.add_argument("title", help="Note title")
    p_scaffold.add_argument("--type", required=True, help="Note type (e.g. concept, entity, or domain-specific type)")
    p_scaffold.add_argument("--desc", "--description", dest="desc", required=True, help="Note description")
    p_scaffold.add_argument("--domain", default="wiki", help="Cognitive domain (default: wiki)")
    p_scaffold.add_argument(
        "--path", help="Custom relative path within domain or vault; ending in '/' names a folder (stem from the title)"
    )
    p_scaffold.add_argument("--tags", help="Comma-separated tags")
    p_scaffold.add_argument("--aliases", help="Comma-separated search aliases and acronyms")
    p_scaffold.add_argument("--sources", help="Comma-separated raw sources")
    p_scaffold.add_argument(
        "--status",
        choices=NOTE_STATUSES,
        default="draft",
        help="Initial note lifecycle status (default: draft)",
    )
    p_scaffold.add_argument(
        "--frontmatter",
        help="JSON custom frontmatter fields (never `verified`, `generated`, or a field with its own flag, like --tags)",
    )
    p_scaffold.add_argument("--body", help="Initial note body (Markdown); exclusive with --template")
    p_scaffold.add_argument("--json", action="store_true", help="Output the result as JSON (the MCP payload)")
    p_scaffold.add_argument(
        "--template",
        help="Obsidian template whose body seeds the note; {{title}} is substituted",
    )
    p_scaffold.add_argument("--actor", type=parse_cli_actor, help=f"Actor identity (default: {CLI_DEFAULT_ACTOR})")
    p_scaffold.set_defaults(func=cmd_scaffold)

    # update
    p_update = subparsers.add_parser("update", parents=[vault_parent], help="Update a note")
    p_update.add_argument("cid", help="Note CID or path")
    p_update.add_argument("--patch-frontmatter", help="JSON frontmatter patch (never `verified` or `generated`)")
    p_update.add_argument(
        "--edits",
        help='JSON list of body edits applied atomically, e.g. \'[{"op":"replace_text","old":"..","new":".."}]\'',
    )
    p_update.add_argument("--expected-hash", help="Expected file hash for concurrency safety")
    p_update.add_argument("--json", action="store_true", help="Output the result as JSON (the MCP payload)")
    p_update.add_argument("--actor", type=parse_cli_actor, help=f"Actor identity (default: {CLI_DEFAULT_ACTOR})")
    p_update.set_defaults(func=cmd_update)

    # move
    p_move = subparsers.add_parser("move", parents=[vault_parent], help="Move or rename a note and repoint its links")
    p_move.add_argument("cid", help="Note CID, path, stem or wikilink to move")
    p_move.add_argument(
        "to",
        help="Destination vault-relative path; a bare name renames in place, and a trailing '/' moves into that folder",
    )
    p_move.add_argument("--apply", action="store_true", help="Write the move (default: dry run)")
    p_move.add_argument(
        "--approve-stale",
        action="append",
        metavar="CID",
        help="A human-reviewed note allowed to go stale-verified (repeat the flag for each)",
    )
    p_move.add_argument("--plan-token", help="The dry run's plan_token: --apply refuses if the plan has changed since")
    p_move.add_argument("--actor", type=parse_cli_actor, help=f"Actor identity (default: {CLI_DEFAULT_ACTOR})")
    p_move.add_argument("--json", action="store_true", help="Output as JSON")
    p_move.set_defaults(func=cmd_move)

    # verify
    p_verify = subparsers.add_parser("verify", parents=[vault_parent], help="Stamp content-bound verification")
    p_verify.add_argument("cid", help="Note CID or path")
    p_verify.add_argument("--method", help="Verification method")
    p_verify.add_argument("--json", action="store_true", help="Output the result as JSON (the MCP payload)")
    who = p_verify.add_mutually_exclusive_group()
    who.add_argument("--human", action="store_true", help="Stamp as human:<user> (requires TTY)")
    who.add_argument("--agent", help="Stamp as agent:<name>")
    p_verify.set_defaults(func=cmd_verify)

    # Operator commands, never exposed over MCP (§5.1).
    # init
    p_init = subparsers.add_parser("init", parents=[vault_parent], help="Scaffold a new vault")
    p_init.add_argument("target_path", nargs="?", type=Path, help="Target directory for new vault (defaults to --vault or current directory)")
    p_init.add_argument("--name", help="Name of the vault")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing files")
    p_init.add_argument("--obsidian", action="store_true", help="Scaffold Obsidian configuration")
    p_init.add_argument(
        "--obsidian-templates",
        action="store_true",
        help="Also write starter note templates and declare their folder (implies --obsidian)",
    )
    p_init.set_defaults(func=cmd_init)

    # install
    p_install = subparsers.add_parser("install", parents=[vault_parent], help="Install harness integrations (§7.6)")
    p_install.add_argument("--antigravity", action="store_true", help="Install Antigravity plugin")
    p_install.add_argument("--claude", action="store_true", help="Install Claude Code MCP configuration")
    p_install.add_argument("--all", action="store_true", help="Install into all supported harnesses")
    p_install.add_argument(
        "--global",
        dest="is_global",
        action="store_true",
        help=(
            "Install into user-global harness configurations instead of the active vault "
            "workspace. Unbound by default: the server resolves the vault per launch. "
            "Add --vault to bind the global install to one vault instead."
        ),
    )
    p_install.add_argument("--uninstall", action="store_true", help="Remove Cadabby harness configurations")
    p_install.add_argument("--dry-run", action="store_true", help="Print changes without modifying files")
    p_install.add_argument("--path", help="Override destination install path")
    p_install.add_argument("--force", action="store_true", help="Overwrite conflicting harness configuration entries")
    p_install.set_defaults(func=cmd_install)

    # mcp
    p_mcp = subparsers.add_parser("mcp", parents=[vault_parent], help="Run Model Context Protocol stdio server")
    p_mcp.set_defaults(func=cmd_mcp)

    # lint
    p_lint = subparsers.add_parser("lint", parents=[vault_parent], help="Run six-gate epistemic linting")
    p_lint.add_argument("--json", action="store_true", help="Output as JSON")
    p_lint.set_defaults(func=cmd_lint)

    # sync
    p_sync = subparsers.add_parser("sync", parents=[vault_parent], help="Sync ephemeral cache and regenerate the index.md gap report")
    p_sync.add_argument("--force", action="store_true", help="Force full rescan")
    p_sync.add_argument("--rebuild", action="store_true", help="Discard cache.db before rescan (§5.3)")
    p_sync.set_defaults(func=cmd_sync)

    # audit
    p_audit = subparsers.add_parser("audit", parents=[vault_parent], help="Audit human verification Git provenance")
    p_audit.add_argument("--require-signed", action="store_true", help="Require signed commits")
    p_audit.add_argument("--json", action="store_true", help="Output as JSON")
    p_audit.set_defaults(func=cmd_audit)

    return parser


def main(argv: list[str] | None = None) -> None:
    """CLI entry point.

    Failures exit with a status derived from the error taxonomy (§5.4) so a
    script can tell "the vault has findings" from "your vault path is wrong"
    from "another process holds the lock, try again". Previously every one of
    those was exit 1.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        sys.exit(args.func(args))
    except BrokenPipeError:
        # `cadabby search x | head -1`: the reader left, which is not a failure
        # of ours. Point stdout at devnull so the interpreter's final flush
        # cannot raise again, and exit cleanly.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        sys.exit(EXIT_OK)
    except Exception as e:  # noqa: BLE001
        info = classify(e)
        print(f"Error [{info.code}]: {info.message}", file=sys.stderr)
        if info.retryable:
            print("This is transient; the same command may succeed if retried.", file=sys.stderr)
        sys.exit(exit_code_for(info.code))


if __name__ == "__main__":
    main()

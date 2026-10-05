"""Command Line Interface (CLI) for Cadabby.

Conforms strictly to Cadabby Technical Specification §5.3.
"""

from __future__ import annotations

import argparse
import getpass
import json
import shutil
import sys
from pathlib import Path

from cadabby.audit import run_vault_audit
from cadabby.cache import VaultCache
from cadabby.constants import (
    DIR_AGENTS,
    DIR_CLAUDE,
    DIR_OBSIDIAN,
    DIR_RAW,
    DIR_WIKI,
    FILE_AGENTS,
    FILE_CLAUDE,
    FILE_CONFIG,
    FILE_GEMINI,
    FILE_INDEX,
    FILE_LOG,
    FILE_MCP,
    FILE_STYLE,
    NOTE_TYPES,
    TRUST_TIERS,
)
from cadabby.errors import (
    EXIT_ENVIRONMENT,
    EXIT_FINDINGS,
    EXIT_OK,
    EXIT_USAGE,
    classify,
    exit_code_for,
)
from cadabby.fsutil import atomic_write
from cadabby.graph import get_note_graph, resolve_link_target
from cadabby.indexer import append_vault_log, rotate_vault_log, sync_vault_index
from cadabby.installer import get_assets_dir, make_mcp_server_entry
from cadabby.lint import run_vault_lint
from cadabby.ops import (
    ground_notes,
    scaffold_note,
    update_note,
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


# The two persona runbooks shipped in assets/skills (§7.1). Named here rather
# than globbed so `init` creates the directories before copying into them.
PERSONAS = ("librarian", "technician")


def cmd_init(args: argparse.Namespace) -> int:
    """Scaffold a fresh vault structure conforming to §2.1, §5.3, and §9."""
    raw_target = getattr(args, "target_path", None) or getattr(args, "vault", None)
    target_dir = Path(raw_target).resolve() if raw_target else Path.cwd()
    vault_name = args.name or target_dir.name or "vault"
    force = getattr(args, "force", False)

    assets = get_assets_dir()
    vault_tpl = assets / "vault"

    # Create directories
    (target_dir / DIR_RAW).mkdir(parents=True, exist_ok=True)
    (target_dir / DIR_WIKI).mkdir(parents=True, exist_ok=True)
    (target_dir / DIR_CLAUDE / "commands").mkdir(parents=True, exist_ok=True)
    for persona in PERSONAS:
        (target_dir / DIR_AGENTS / "skills" / persona).mkdir(parents=True, exist_ok=True)

    def copy_template_file(src: Path, dst: Path) -> bool:
        """Copy a template into the vault, honoring --force. True if dst was written."""
        if not src.exists():
            return False
        if dst.exists():
            if not force:
                print(f"Existing file left untouched: {dst.name} (use --force to overwrite)")
                return False
            shutil.copy(src, dst)
            print(f"Overwrote existing file: {dst.name}")
            return True
        shutil.copy(src, dst)
        return True

    def bind_mcp_config(dst: Path) -> None:
        """Point an MCP config at this vault and this interpreter.

        Callers gate this on having actually written the template: rewriting
        one that --force just declined to touch would contradict the message
        copy_template_file printed. Use 'cadabby install' to re-bind an
        existing config.
        """
        entry = {"mcpServers": {"cadabby": make_mcp_server_entry(target_dir)}}
        atomic_write(dst, json.dumps(entry, indent=2) + "\n")

    # 1. Config .cadabby.json
    cfg_src = vault_tpl / FILE_CONFIG
    cfg_dst = target_dir / FILE_CONFIG
    if cfg_dst.exists() and not force:
        print(f"Existing file left untouched: {FILE_CONFIG} (use --force to overwrite)")
    elif cfg_src.exists():
        cfg_data = json.loads(cfg_src.read_text("utf-8"))
        cfg_data["vault_name"] = vault_name
        atomic_write(cfg_dst, json.dumps(cfg_data, indent=2) + "\n")

    # 2. Files from vault template
    wrote_mcp_json = False
    for filename in (".gitignore", FILE_MCP, FILE_AGENTS, FILE_CLAUDE, FILE_GEMINI, FILE_STYLE, FILE_INDEX):
        written = copy_template_file(vault_tpl / filename, target_dir / filename)
        if filename == FILE_MCP:
            wrote_mcp_json = written

    # Bind so Claude Code can mount the workspace server with no further setup.
    if wrote_mcp_json:
        bind_mcp_config(target_dir / FILE_MCP)

    # 3. Touch empty log.md
    log_file = target_dir / FILE_LOG
    if not log_file.exists():
        atomic_write(log_file, "# Activity Ledger\n\n")

    # 4. Copy canonical persona skills to .agents/skills/
    skills_dir = assets / "skills"
    if skills_dir.exists():
        for persona in PERSONAS:
            copy_template_file(
                skills_dir / persona / "SKILL.md",
                target_dir / DIR_AGENTS / "skills" / persona / "SKILL.md",
            )

    # 5. Copy slash command templates to .claude/commands/
    cmds_dir = assets / "commands"
    if cmds_dir.exists():
        for cmd_file in sorted(cmds_dir.glob("*.md")):
            copy_template_file(cmd_file, target_dir / DIR_CLAUDE / "commands" / cmd_file.name)

    # 6. Copy Antigravity vault-scoped plugin & MCP configuration to .agents/plugins/cadabby/
    plugin_src = assets / "plugins" / "cadabby"
    if plugin_src.exists():
        dst_plugin = target_dir / DIR_AGENTS / "plugins" / "cadabby"
        wrote_mcp_cfg = False
        for p in sorted(plugin_src.rglob("*")):
            if p.is_file():
                rel = p.relative_to(plugin_src)
                target_file = dst_plugin / rel
                target_file.parent.mkdir(parents=True, exist_ok=True)
                written = copy_template_file(p, target_file)
                if rel.as_posix() == "mcp_config.json":
                    wrote_mcp_cfg = written

        if wrote_mcp_cfg:
            bind_mcp_config(dst_plugin / "mcp_config.json")

    # 7. Optional Obsidian config
    if args.obsidian:
        obsidian_dir = target_dir / DIR_OBSIDIAN
        obsidian_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / DIR_RAW / "attachments").mkdir(parents=True, exist_ok=True)
        copy_template_file(vault_tpl / "obsidian" / "app.json", obsidian_dir / "app.json")

    print(f"Initialized Cadabby vault '{vault_name}' in {target_dir}")
    return EXIT_OK


def cmd_sync(args: argparse.Namespace) -> int:
    """Run incremental cache scan and regenerate the index.md gap report."""
    vault = resolve_cli_vault(args)
    rebuild = getattr(args, "rebuild", False)
    if rebuild:
        cache_path = vault.cache_db_path
        if cache_path.exists():
            cache_path.unlink()
        for ext in ("-wal", "-shm"):
            extra = Path(str(cache_path) + ext)
            if extra.exists():
                extra.unlink()

    with VaultCache(vault) as cache:
        ins, upd, deleted, total = cache.scan(force=(args.force or rebuild))
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
        results = cache.search(
            query=args.query,
            type_=args.type,
            status=args.status,
            trust=args.trust,
            tag=args.tag,
            domain=getattr(args, "domain", None),
            limit=args.limit,
        )

    if args.json:
        out = [r.to_dict() for r in results]
        print(json.dumps(out, indent=2))
        return EXIT_OK

    if not results:
        print(f"No results found matching '{args.query}'.")
        return EXIT_OK

    print(f"\nFound {len(results)} matches for '{args.query}':\n")
    print(f"{'CID':<42} {'TRUST':<18} {'STATUS':<12} {'SCORE':<8} {'TITLE'}")
    print("-" * 100)
    for r in results:
        tier = r.trust_tier or "unverified"
        st = r.status or "active"
        title = (r.title or "")[:35]
        print(f"{r.cid:<42} {tier:<18} {st:<12} {r.score:<8.3f} {title}")
    print()
    return EXIT_OK


def cmd_ground(args: argparse.Namespace) -> int:
    """Retrieve full content and 1-hop graph for specified CIDs."""
    vault = resolve_cli_vault(args)
    with VaultCache(vault) as cache:
        cache.scan()
        grounded = ground_notes(vault, args.cids, budget_tokens=args.budget_tokens, cache=cache)

    if args.json:
        print(json.dumps(grounded, indent=2))
        return EXIT_OK

    for item in grounded:
        print("=" * 80)
        print(f"CID: {item['cid']} ({item['trust_tier']})")
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
    sources = [s.strip() for s in args.sources.split(",") if s.strip()] if args.sources else None

    path = scaffold_note(
        vault=vault,
        title=args.title,
        type_=args.type,
        description=args.desc,
        tags=tags,
        sources=sources,
        actor=args.actor or f"human:{getpass.getuser()}",
        domain=getattr(args, "domain", "wiki"),
        path=getattr(args, "path", None),
    )
    print(f"Scaffolded note: {vault.rel_path(path)}")
    return EXIT_OK


def cmd_update(args: argparse.Namespace) -> int:
    """Update note frontmatter or sections non-destructively."""
    vault = resolve_cli_vault(args)
    patch = json.loads(args.patch_frontmatter) if args.patch_frontmatter else None

    append_sec = None
    if args.append_section:
        parts = args.append_section.split(":", 1)
        append_sec = (parts[0], parts[1] if len(parts) > 1 else "")

    replace_sec = None
    if args.replace_section:
        parts = args.replace_section.split(":", 1)
        replace_sec = (parts[0], parts[1] if len(parts) > 1 else "")

    path = update_note(
        vault=vault,
        cid_or_path=args.cid,
        frontmatter_patch=patch,
        append_section=append_sec,
        replace_section=replace_sec,
        expected_hash=args.expected_hash,
        actor=args.actor or f"human:{getpass.getuser()}",
    )
    print(f"Updated note: {vault.rel_path(path)}")
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    """Stamp a content-bound verification attestation."""
    vault = resolve_cli_vault(args)

    if args.human and args.agent:
        print("Error: --human and --agent are mutually exclusive.", file=sys.stderr)
        return EXIT_USAGE

    if args.human:
        if not sys.stdin.isatty():
            print("Error: --human requires an interactive TTY.", file=sys.stderr)
            return EXIT_USAGE
        actor = f"human:{getpass.getuser()}"
        is_human = True
    elif args.agent:
        actor = args.agent if args.agent.startswith(("agent:", "process:")) else f"agent:{args.agent}"
        is_human = False
    else:
        # Default to process:cli unless interactive (§5.3)
        if sys.stdin.isatty():
            actor = f"human:{getpass.getuser()}"
            is_human = True
        else:
            actor = "process:cli"
            is_human = False

    res = verify_note(
        vault=vault,
        cid_or_path=args.cid,
        actor=actor,
        method=args.method or ("manual-review" if is_human else "automated-check"),
        is_human_authorized=is_human,
    )
    status_label = "already verified" if res.get("already_verified") else "verified"
    print(f"{status_label.capitalize()}: {res['cid']} as '{res['actor']}' -> tier: '{res['trust_tier']}' ({res['of'][:16]}...)")
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    """Print high-level vault status, distribution, and verification debt."""
    vault = resolve_cli_vault(args)
    with VaultCache(vault) as cache:
        cache.scan()
        status_data = cache.get_status()

    if args.json:
        print(json.dumps(status_data, indent=2))
        return EXIT_OK

    print(f"\n=== Vault Status: {status_data['vault_name']} ===")
    print(f"Path: {vault.root}")
    print(
        f"Total Notes: {status_data['total_notes']} | Raw Sources: {status_data['total_raw']} "
        f"({status_data['unprocessed_raw']} unprocessed)"
    )
    print(f"Verification Debt (stale): {status_data['verification_debt']}")
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
    print()
    return EXIT_OK


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
    for f in findings:
        loc = f"{f.rel_path}:{f.line}" if f.line else f.rel_path
        sev_color = "ERROR" if f.severity == "error" else "WARN "
        print(f"[{sev_color}] {f.code:<22} {loc:<45} {f.message}")
    print()

    return EXIT_FINDINGS if errors else EXIT_OK


def cmd_audit(args: argparse.Namespace) -> int:
    """Run Git provenance audit against human:* endorsements."""
    vault = resolve_cli_vault(args)
    findings, notice = run_vault_audit(vault, require_signed=args.require_signed)

    if notice:
        if args.json:
            print(json.dumps({"notice": notice, "findings": []}))
        else:
            print(f"Notice: {notice}")
        return EXIT_OK

    if args.json:
        out = [
            {
                "code": f.code,
                "rel_path": f.rel_path,
                "line": f.line,
                "actor": f.actor,
                "commit": f.commit,
                "message": f.message,
            }
            for f in findings
        ]
        print(json.dumps(out, indent=2))
        return EXIT_FINDINGS if findings else EXIT_OK

    if not findings:
        print("Audit clean: all human:* endorsements match Git author provenance.")
        return EXIT_OK

    print(f"\nAudit Findings: {len(findings)} issue(s) detected\n")
    for f in findings:
        print(f"[{f.code}] {f.rel_path}:{f.line} ({f.actor} in {f.commit[:8]}) - {f.message}")
    print()
    return EXIT_FINDINGS


def cmd_log(args: argparse.Namespace) -> int:
    """Append a timestamped entry to the active log.md ledger (§5.3)."""
    vault = resolve_cli_vault(args)
    actor = args.actor
    if not actor:
        if sys.stdin.isatty():
            actor = f"human:{getpass.getuser()}"
        else:
            actor = "process:cli"
    append_vault_log(vault, args.message, actor=actor)
    print(f"Logged entry to {vault.rel_path(vault.log_path)}")
    return EXIT_OK


def cmd_graph(args: argparse.Namespace) -> int:
    """Display 1-hop and 2-hop neighbors, co-citations, and links for a note (§5.3)."""
    vault = resolve_cli_vault(args)
    with VaultCache(vault) as cache:
        cache.scan()
        conn = cache.get_connection()
        cid = args.cid
        # Resolve target stem if not a full CID
        cids = [r["cid"] for r in conn.execute("SELECT cid FROM notes").fetchall()]
        resolved = resolve_link_target(cid, cids)
        if resolved:
            cid = resolved

        graph_data = get_note_graph(conn, cid)
        if not graph_data:
            print(f"Error: Note not found: '{args.cid}'", file=sys.stderr)
            return EXIT_ENVIRONMENT

        if args.json:
            print(json.dumps(graph_data, indent=2))
            return EXIT_OK

        note = graph_data["note"]
        print(f"Graph for [[{note['cid']}]] ({note['type']}, {note['trust_tier']}):\n")

        print("Forward Links (1-hop):")
        if graph_data["forward_links"]:
            for fl in graph_data["forward_links"]:
                tgt = fl["target_cid"] or f"{fl['target_raw']} (unresolved)"
                print(f"  -> [[{tgt}]]")
        else:
            print("  (none)")

        print("\nBacklinks (1-hop):")
        if graph_data["backlinks"]:
            for bl in graph_data["backlinks"]:
                print(f"  <- [[{bl['source_cid']}]]")
        else:
            print("  (none)")

        print("\n2-Hop Forward:")
        if graph_data["two_hop_forward"]:
            for th in graph_data["two_hop_forward"]:
                print(f"  -> [[{th['target_cid']}]] (via [[{th['via']}]])")
        else:
            print("  (none)")

        print("\n2-Hop Backlinks:")
        if graph_data["two_hop_backlinks"]:
            for th in graph_data["two_hop_backlinks"]:
                print(f"  <- [[{th['source_cid']}]] (via [[{th['via']}]])")
        else:
            print("  (none)")

        print("\nCo-Citations:")
        if graph_data["co_citations"]:
            for cc in graph_data["co_citations"]:
                print(f"  * [[{cc['cid']}]] ({cc['count']} shared citations)")
        else:
            print("  (none)")

        return EXIT_OK


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
            raise FileNotFoundError(
                "No Cadabby vault found in current working directory. "
                "Run inside a vault, pass --vault <path>, or pass --global for user-level installation."
            )
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


def build_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser."""
    vault_parent = argparse.ArgumentParser(add_help=False)
    vault_parent.add_argument(
        "--vault",
        type=Path,
        default=argparse.SUPPRESS,
        help="Path to vault root (default: $CADABBY_VAULT, else discovered from cwd)",
    )

    parser = argparse.ArgumentParser(
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

    # init
    p_init = subparsers.add_parser("init", parents=[vault_parent], help="Scaffold a new vault")
    p_init.add_argument("target_path", nargs="?", type=Path, help="Target directory for new vault (defaults to --vault or current directory)")
    p_init.add_argument("--name", help="Name of the vault")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing files")
    p_init.add_argument("--obsidian", action="store_true", help="Scaffold Obsidian configuration")
    p_init.set_defaults(func=cmd_init)

    # sync
    p_sync = subparsers.add_parser("sync", parents=[vault_parent], help="Sync ephemeral cache and regenerate the index.md gap report")
    p_sync.add_argument("--force", action="store_true", help="Force full rescan")
    p_sync.add_argument("--rebuild", action="store_true", help="Discard cache.db before rescan (§5.3)")
    p_sync.set_defaults(func=cmd_sync)

    # search
    p_search = subparsers.add_parser("search", parents=[vault_parent], help="Search vault notes")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument(
        "--domain",
        help="Filter by cognitive domain (e.g. wiki, customers, projects); 'raw' searches unprocessed sources",
    )
    p_search.add_argument("--type", help="Filter by note type")
    p_search.add_argument("--status", help="Filter by note status")
    p_search.add_argument("--trust", help="Filter by trust tier")
    p_search.add_argument("--tag", help="Filter by tag")
    p_search.add_argument("--limit", type=int, default=20, help="Maximum results (default: 20)")
    p_search.add_argument("--json", action="store_true", help="Output as JSON")
    p_search.set_defaults(func=cmd_search)

    # ground
    p_ground = subparsers.add_parser("ground", parents=[vault_parent], help="Retrieve full content and 1-hop graph")
    p_ground.add_argument("cids", nargs="+", help="One or more note CIDs, note stems, or wikilinks")
    p_ground.add_argument("--budget-tokens", "--budget", dest="budget_tokens", type=int, default=None, help="Token budget")
    p_ground.add_argument("--json", action="store_true", help="Output as JSON")
    p_ground.set_defaults(func=cmd_ground)

    # scaffold
    p_scaffold = subparsers.add_parser("scaffold", parents=[vault_parent], help="Scaffold a new note")
    p_scaffold.add_argument("title", help="Note title")
    p_scaffold.add_argument("--type", required=True, help="Note type (e.g. concept, entity, or domain-specific type)")
    p_scaffold.add_argument("--desc", "--description", dest="desc", required=True, help="Note description")
    p_scaffold.add_argument("--domain", default="wiki", help="Cognitive domain (default: wiki)")
    p_scaffold.add_argument("--path", help="Custom relative path within domain or vault")
    p_scaffold.add_argument("--tags", help="Comma-separated tags")
    p_scaffold.add_argument("--sources", help="Comma-separated raw sources")
    p_scaffold.add_argument("--actor", help="Actor identity (default: human:<user>)")
    p_scaffold.set_defaults(func=cmd_scaffold)

    # update
    p_update = subparsers.add_parser("update", parents=[vault_parent], help="Update a note")
    p_update.add_argument("cid", help="Note CID or path")
    p_update.add_argument("--patch-frontmatter", help="JSON frontmatter patch")
    p_update.add_argument("--append-section", help="Heading:Body to append")
    p_update.add_argument("--replace-section", help="Heading:Body to replace")
    p_update.add_argument("--expected-hash", help="Expected file hash for concurrency safety")
    p_update.add_argument("--actor", help="Actor identity")
    p_update.set_defaults(func=cmd_update)

    # verify
    p_verify = subparsers.add_parser("verify", parents=[vault_parent], help="Stamp content-bound verification")
    p_verify.add_argument("cid", help="Note CID or path")
    p_verify.add_argument("--method", help="Verification method")
    p_verify.add_argument("--human", action="store_true", help="Stamp as human:<user> (requires TTY)")
    p_verify.add_argument("--agent", help="Stamp as agent:<name>")
    p_verify.set_defaults(func=cmd_verify)

    # status
    p_status = subparsers.add_parser("status", parents=[vault_parent], help="Report vault status and verification debt")
    p_status.add_argument("--json", action="store_true", help="Output as JSON")
    p_status.set_defaults(func=cmd_status)

    # lint
    p_lint = subparsers.add_parser("lint", parents=[vault_parent], help="Run six-gate epistemic linting")
    p_lint.add_argument("--json", action="store_true", help="Output as JSON")
    p_lint.set_defaults(func=cmd_lint)

    # audit
    p_audit = subparsers.add_parser("audit", parents=[vault_parent], help="Audit human verification Git provenance")
    p_audit.add_argument("--require-signed", action="store_true", help="Require signed commits")
    p_audit.add_argument("--json", action="store_true", help="Output as JSON")
    p_audit.set_defaults(func=cmd_audit)

    # log
    p_log = subparsers.add_parser("log", parents=[vault_parent], help="Append a timestamped entry to log.md")
    p_log.add_argument("message", help="Log message text")
    p_log.add_argument("--actor", help="Actor identity (default: human:<user> or process:cli)")
    p_log.set_defaults(func=cmd_log)

    # graph
    p_graph = subparsers.add_parser("graph", parents=[vault_parent], help="Display 1-hop and 2-hop neighbors and co-citations")
    p_graph.add_argument("cid", help="Note CID, stem, or path")
    p_graph.add_argument("--json", action="store_true", help="Output as JSON")
    p_graph.set_defaults(func=cmd_graph)

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

    return parser


def main() -> None:
    """CLI entry point.

    Failures exit with a status derived from the error taxonomy (§5.4) so a
    script can tell "the vault has findings" from "your vault path is wrong"
    from "another process holds the lock, try again". Previously every one of
    those was exit 1.
    """
    parser = build_parser()
    args = parser.parse_args()
    try:
        sys.exit(args.func(args))
    except Exception as e:  # noqa: BLE001
        info = classify(e)
        print(f"Error [{info.code}]: {info.message}", file=sys.stderr)
        if info.retryable:
            print("This is transient; the same command may succeed if retried.", file=sys.stderr)
        sys.exit(exit_code_for(info.code))


if __name__ == "__main__":
    main()

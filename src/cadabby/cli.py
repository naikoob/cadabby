"""Command Line Interface (CLI) for Cadabby.

Conforms strictly to Cadabby Technical Specification §6.1.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from cadabby.audit import run_vault_audit
from cadabby.cache import VaultCache
from cadabby.constants import DIR_RAW, DIR_WIKI, NOTE_TYPES
from cadabby.fsutil import atomic_write
from cadabby.indexer import sync_vault_index
from cadabby.lint import run_vault_lint
from cadabby.ops import TYPE_TO_DIR, ground_notes, scaffold_note, update_note, verify_note
from cadabby.vault import Vault, find_vault_root


def get_assets_dir() -> Path:
    """Return path to assets directory."""
    # Look next to package or repository root
    pkg_root = Path(__file__).resolve().parent.parent.parent
    assets_dir = pkg_root / "assets"
    if assets_dir.exists():
        return assets_dir
    # Fallback to current working directory
    cwd_assets = Path.cwd() / "assets"
    if cwd_assets.exists():
        return cwd_assets
    raise RuntimeError(f"Cadabby assets directory not found at {assets_dir}")


def cmd_init(args: argparse.Namespace) -> int:
    """Scaffold a fresh vault structure conforming to §2.1 and §9."""
    target_dir = Path(args.vault).resolve() if args.vault else Path.cwd()
    vault_name = args.name or target_dir.name or "vault"

    assets = get_assets_dir()
    vault_tpl = assets / "vault"

    # Create directories
    (target_dir / DIR_RAW).mkdir(parents=True, exist_ok=True)
    for sub in TYPE_TO_DIR.values():
        (target_dir / DIR_WIKI / sub).mkdir(parents=True, exist_ok=True)
    (target_dir / ".claude" / "commands").mkdir(parents=True, exist_ok=True)
    (target_dir / ".agents" / "skills" / "librarian").mkdir(parents=True, exist_ok=True)
    (target_dir / ".agents" / "skills" / "technician").mkdir(parents=True, exist_ok=True)

    # 1. Config .cadabby.json
    cfg_src = vault_tpl / ".cadabby.json"
    if cfg_src.exists():
        cfg_data = json.loads(cfg_src.read_text("utf-8"))
        cfg_data["vault_name"] = vault_name
        atomic_write(target_dir / ".cadabby.json", json.dumps(cfg_data, indent=2) + "\n")

    # 2. Files from vault template
    for filename in (".gitignore", ".mcp.json", "AGENTS.md", "CLAUDE.md", "GEMINI.md", "STYLE.md", "index.md"):
        src = vault_tpl / filename
        dst = target_dir / filename
        if src.exists() and not dst.exists():
            shutil.copy(src, dst)

    # 3. Touch empty log.md
    log_file = target_dir / "log.md"
    if not log_file.exists():
        atomic_write(log_file, "# Activity Ledger\n\n")

    # 4. Copy canonical persona skills to .agents/skills/
    skills_dir = assets / "skills"
    if skills_dir.exists():
        for skill_name in ("librarian", "technician"):
            src_skill = skills_dir / skill_name / "SKILL.md"
            dst_skill = target_dir / ".agents" / "skills" / skill_name / "SKILL.md"
            if src_skill.exists() and not dst_skill.exists():
                shutil.copy(src_skill, dst_skill)

    # 5. Copy slash command templates to .claude/commands/
    cmds_dir = assets / "commands"
    if cmds_dir.exists():
        for cmd_file in cmds_dir.glob("*.md"):
            dst_cmd = target_dir / ".claude" / "commands" / cmd_file.name
            if not dst_cmd.exists():
                shutil.copy(cmd_file, dst_cmd)

    # 6. Optional Obsidian config
    if args.obsidian:
        obsidian_dir = target_dir / ".obsidian"
        obsidian_dir.mkdir(parents=True, exist_ok=True)
        obs_src = vault_tpl / "obsidian" / "app.json"
        if obs_src.exists():
            shutil.copy(obs_src, obsidian_dir / "app.json")

    print(f"Initialized Cadabby vault '{vault_name}' in {target_dir}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    """Run incremental cache scan and catalog sync."""
    vault = Vault(args.vault)
    cache = VaultCache(vault)
    ins, upd, deleted, total = cache.scan(force=args.force)
    sync_vault_index(vault)

    print(f"Vault sync complete: {total} total tracked ({ins} inserted, {upd} updated, {deleted} deleted).")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """Search vault notes with normalized epistemic BM25 ranking."""
    vault = Vault(args.vault)
    cache = VaultCache(vault)
    results = cache.search(
        query=args.query,
        type_=args.type,
        status=args.status,
        trust=args.trust,
        tag=args.tag,
        limit=args.limit,
    )

    if args.json:
        out = [
            {
                "cid": r.cid,
                "title": r.title,
                "description": r.description,
                "type": r.type,
                "status": r.status,
                "trust_tier": r.trust_tier,
                "score": round(r.score, 4),
                "snippet": r.snippet,
            }
            for r in results
        ]
        print(json.dumps(out, indent=2))
        return 0

    if not results:
        print(f"No results found matching '{args.query}'.")
        return 0

    print(f"\nFound {len(results)} matches for '{args.query}':\n")
    print(f"{'CID':<42} {'TRUST':<18} {'STATUS':<12} {'SCORE':<8} {'TITLE'}")
    print("-" * 100)
    for r in results:
        tier = r.trust_tier or "unverified"
        st = r.status or "active"
        title = (r.title or "")[:35]
        print(f"{r.cid:<42} {tier:<18} {st:<12} {r.score:<8.3f} {title}")
    print()
    return 0


def cmd_ground(args: argparse.Namespace) -> int:
    """Retrieve full content and 1-hop graph for specified CIDs."""
    vault = Vault(args.vault)
    grounded = ground_notes(vault, args.cids, budget_tokens=args.budget_tokens)

    if args.json:
        print(json.dumps(grounded, indent=2))
        return 0

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
    return 0


def cmd_scaffold(args: argparse.Namespace) -> int:
    """Scaffold a new note."""
    vault = Vault(args.vault)
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
    )
    print(f"Scaffolded note: {vault.rel_path(path)}")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    """Update note frontmatter or sections non-destructively."""
    vault = Vault(args.vault)
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
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Stamp a content-bound verification attestation."""
    vault = Vault(args.vault)

    if args.human:
        if not sys.stdin.isatty():
            print("Error: --human requires an interactive TTY.", file=sys.stderr)
            return 1
        actor = f"human:{getpass.getuser()}"
        is_human = True
    elif args.agent:
        actor = args.agent if args.agent.startswith(("agent:", "process:")) else f"agent:{args.agent}"
        is_human = False
    else:
        # Default to agent:cli unless interactive
        if sys.stdin.isatty():
            actor = f"human:{getpass.getuser()}"
            is_human = True
        else:
            actor = "agent:cli"
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
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Print high-level vault status, distribution, and verification debt."""
    vault = Vault(args.vault)
    cache = VaultCache(vault)
    cache.scan()
    conn = cache.get_connection()

    # Query counts
    cur = conn.execute("SELECT COUNT(*) FROM notes WHERE layer = 'wiki';")
    total_wiki = cur.fetchone()[0]

    cur = conn.execute("SELECT COUNT(*) FROM notes WHERE layer = 'raw';")
    total_raw = cur.fetchone()[0]

    # Unprocessed raw files
    cur = conn.execute(
        """
        SELECT COUNT(*) FROM notes r
        WHERE r.layer = 'raw'
          AND NOT EXISTS (SELECT 1 FROM sources s WHERE s.raw_path = r.rel_path);
        """
    )
    unprocessed_raw = cur.fetchone()[0]

    # Trust tiers
    cur = conn.execute(
        "SELECT trust_tier, COUNT(*) as cnt FROM notes WHERE layer = 'wiki' GROUP BY trust_tier;"
    )
    tier_counts = {r["trust_tier"] or "unverified": r["cnt"] for r in cur.fetchall()}

    # Verification debt
    stale_count = tier_counts.get("stale-verified", 0)

    # Note types
    cur = conn.execute(
        "SELECT type, COUNT(*) as cnt FROM notes WHERE layer = 'wiki' GROUP BY type;"
    )
    type_counts = {r["type"] or "unknown": r["cnt"] for r in cur.fetchall()}

    # Note statuses
    cur = conn.execute(
        "SELECT status, COUNT(*) as cnt FROM notes WHERE layer = 'wiki' GROUP BY status;"
    )
    status_counts = {r["status"] or "unknown": r["cnt"] for r in cur.fetchall()}

    status_data = {
        "vault_name": vault.config.get("vault_name", "vault"),
        "root": str(vault.root),
        "total_notes": total_wiki,
        "total_raw": total_raw,
        "unprocessed_raw": unprocessed_raw,
        "verification_debt": stale_count,
        "trust_tiers": tier_counts,
        "types": type_counts,
        "statuses": status_counts,
        "integrity": vault.config.get("integrity", "mtime_size"),
    }

    if args.json:
        print(json.dumps(status_data, indent=2))
        return 0

    print(f"\n=== Vault Status: {status_data['vault_name']} ===")
    print(f"Path: {vault.root}")
    print(f"Total Notes: {total_wiki} | Raw Sources: {total_raw} ({unprocessed_raw} unprocessed)")
    print(f"Verification Debt (stale): {stale_count}")
    print("\nTrust Tiers:")
    for tier in ("human-reviewed", "machine-confirmed", "stale-verified", "unverified"):
        print(f"  - {tier:<20}: {tier_counts.get(tier, 0)}")
    print("\nTypes:")
    for t in NOTE_TYPES:
        print(f"  - {t:<20}: {type_counts.get(t, 0)}")
    print("\nStatuses:")
    for s, count in status_counts.items():
        print(f"  - {s:<20}: {count}")
    print()
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    """Run six-gate epistemic linting."""
    vault = Vault(args.vault)
    findings = run_vault_lint(vault)

    if args.json:
        out = [
            {
                "code": f.code,
                "severity": f.severity,
                "rel_path": f.rel_path,
                "line": f.line,
                "message": f.message,
            }
            for f in findings
        ]
        print(json.dumps(out, indent=2))
        has_errors = any(f.severity == "error" for f in findings)
        return 1 if has_errors else 0

    errors = [f for f in findings if f.severity == "error"]
    warnings = [f for f in findings if f.severity == "warning"]

    if not findings:
        print("Vault is clean. All six normative gates passed.")
        return 0

    print(f"\nEpistemic Lint Results: {len(errors)} error(s), {len(warnings)} warning(s)\n")
    for f in findings:
        loc = f"{f.rel_path}:{f.line}" if f.line else f.rel_path
        sev_color = "ERROR" if f.severity == "error" else "WARN "
        print(f"[{sev_color}] {f.code:<22} {loc:<45} {f.message}")
    print()

    return 1 if errors else 0


def cmd_audit(args: argparse.Namespace) -> int:
    """Run Git provenance audit against human:* endorsements."""
    vault = Vault(args.vault)
    findings, notice = run_vault_audit(vault, require_signed=args.require_signed)

    if notice:
        if args.json:
            print(json.dumps({"notice": notice, "findings": []}))
        else:
            print(f"Notice: {notice}")
        return 0

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
        return 1 if findings else 0

    if not findings:
        print("Audit clean: all human:* endorsements match Git author provenance.")
        return 0

    print(f"\nAudit Findings: {len(findings)} issue(s) detected\n")
    for f in findings:
        print(f"[{f.code}] {f.rel_path}:{f.line} ({f.actor} in {f.commit[:8]}) - {f.message}")
    print()
    return 1


def cmd_install(args: argparse.Namespace) -> int:
    """Install Cadabby configurations into target AI harnesses."""
    installed_any = False

    if args.antigravity:
        # Install into Antigravity plugins or MCP
        dest_plugin = Path.home() / ".gemini" / "antigravity" / "plugins" / "cadabby"
        repo_plugin = Path(__file__).resolve().parent.parent.parent / "plugins" / "cadabby"
        if repo_plugin.exists():
            dest_plugin.parent.mkdir(parents=True, exist_ok=True)
            if dest_plugin.is_symlink() or dest_plugin.exists():
                if dest_plugin.is_symlink():
                    dest_plugin.unlink()
                else:
                    shutil.rmtree(dest_plugin)
            dest_plugin.symlink_to(repo_plugin)
            print(f"Linked Antigravity plugin to {dest_plugin}")
            installed_any = True

    if args.claude:
        # Install into Claude Code or Claude Desktop
        claude_cfg_path = Path.home() / ".claude.json"
        cfg_data: dict[str, Any] = {}
        if claude_cfg_path.exists():
            try:
                cfg_data = json.loads(claude_cfg_path.read_text("utf-8"))
            except Exception:
                pass
        mcp_servers = cfg_data.setdefault("mcpServers", {})
        mcp_servers["cadabby"] = {
            "command": "python3",
            "args": ["-m", "cadabby", "mcp"],
        }
        atomic_write(claude_cfg_path, json.dumps(cfg_data, indent=2) + "\n")
        print(f"Registered Cadabby MCP server in {claude_cfg_path}")
        installed_any = True

    if not installed_any:
        print("Please specify a target harness: --antigravity or --claude")
        return 1
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    """Run the Model Context Protocol stdio server."""
    from cadabby.mcp import run_mcp_server

    vault = Vault(args.vault)
    return run_mcp_server(vault)


def build_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser."""
    vault_parent = argparse.ArgumentParser(add_help=False)
    vault_parent.add_argument(
        "--vault",
        type=Path,
        default=None,
        help="Path to vault root (default: discovered from cwd)",
    )

    parser = argparse.ArgumentParser(
        prog="cadabby",
        description="Epistemically auditable LLM Wiki engine for humans and AI agents.",
        parents=[vault_parent],
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # init
    p_init = subparsers.add_parser("init", parents=[vault_parent], help="Scaffold a new vault")
    p_init.add_argument("--name", help="Name of the vault")
    p_init.add_argument("--obsidian", action="store_true", help="Scaffold Obsidian configuration")
    p_init.set_defaults(func=cmd_init)

    # sync
    p_sync = subparsers.add_parser("sync", parents=[vault_parent], help="Sync ephemeral cache and index catalog")
    p_sync.add_argument("--force", action="store_true", help="Force full rescan")
    p_sync.set_defaults(func=cmd_sync)

    # search
    p_search = subparsers.add_parser("search", parents=[vault_parent], help="Search vault notes")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--type", help="Filter by note type")
    p_search.add_argument("--status", help="Filter by note status")
    p_search.add_argument("--trust", help="Filter by trust tier")
    p_search.add_argument("--tag", help="Filter by tag")
    p_search.add_argument("--limit", type=int, default=20, help="Maximum results (default: 20)")
    p_search.add_argument("--json", action="store_true", help="Output as JSON")
    p_search.set_defaults(func=cmd_search)

    # ground
    p_ground = subparsers.add_parser("ground", parents=[vault_parent], help="Retrieve full content and 1-hop graph")
    p_ground.add_argument("cids", nargs="+", help="One or more note CIDs")
    p_ground.add_argument("--budget-tokens", type=int, default=None, help="Token budget")
    p_ground.add_argument("--json", action="store_true", help="Output as JSON")
    p_ground.set_defaults(func=cmd_ground)

    # scaffold
    p_scaffold = subparsers.add_parser("scaffold", parents=[vault_parent], help="Scaffold a new note")
    p_scaffold.add_argument("title", help="Note title")
    p_scaffold.add_argument("--type", required=True, choices=NOTE_TYPES, help="Note type")
    p_scaffold.add_argument("--desc", required=True, help="Note description")
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

    # install
    p_install = subparsers.add_parser("install", parents=[vault_parent], help="Install harness integrations")
    p_install.add_argument("--antigravity", action="store_true", help="Install Antigravity plugin")
    p_install.add_argument("--claude", action="store_true", help="Install Claude Code MCP configuration")
    p_install.set_defaults(func=cmd_install)

    # mcp
    p_mcp = subparsers.add_parser("mcp", parents=[vault_parent], help="Run Model Context Protocol stdio server")
    p_mcp.set_defaults(func=cmd_mcp)

    return parser


def main() -> None:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args()
    try:
        sys.exit(args.func(args))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

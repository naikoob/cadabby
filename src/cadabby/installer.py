"""Harness configuration installer and idempotent non-destructive merge.

Conforms strictly to Cadabby Technical Specification §7.6.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from cadabby.constants import (
    DIR_AGENTS,
    DIR_CLAUDE,
    DIR_OBSIDIAN,
    DIR_RAW,
    DIR_TEMPLATES,
    DIR_WIKI,
    FILE_AGENTS,
    FILE_CLAUDE,
    FILE_CONFIG,
    FILE_GEMINI,
    FILE_INDEX,
    FILE_LOG,
    FILE_MCP,
    FILE_OBSIDIAN_TEMPLATES,
    FILE_STYLE,
    LEDGER_HEADER,
)
from cadabby.errors import EXIT_ENVIRONMENT, EXIT_OK, INVALID_ARGUMENT, CadabbyError, InvalidArgumentError
from cadabby.fsutil import atomic_replace_checked, atomic_write, compute_bytes_sha256, contained
from cadabby.vault import obsidian_template_dir


def mcp_launch_argv() -> list[str]:
    """Resolve the argv a harness should spawn to start the MCP server.

    A harness launches this as a bare subprocess with no activated environment,
    so the command has to name an interpreter that can actually import cadabby.
    A bare "python3" cannot: the harness resolves it from its own PATH, which
    for any isolated install (pipx, uv tool, venv) has no cadabby on it.

    Prefers the console script sitting beside the running interpreter, which is
    by construction the one for this environment, and falls back to the running
    interpreter itself -- importable by definition, since it is executing now.
    """
    # Deliberately not resolve(): a venv's bin/python is a symlink to the base
    # interpreter, so resolving would leave the venv and miss its console script.
    bin_dir = Path(sys.executable).parent
    script = shutil.which("cadabby", path=str(bin_dir))
    if script:
        return [script, "mcp"]
    return [sys.executable, "-m", "cadabby", "mcp"]


def make_mcp_server_entry(vault_path: Path | str | None = None) -> dict[str, Any]:
    """Generate the MCP server configuration entry, optionally bound to a vault."""
    command, *args = mcp_launch_argv()
    if vault_path is not None:
        args += ["--vault", str(Path(vault_path).resolve())]
    return {"command": command, "args": args}


def get_assets_dir() -> Path:
    """Return the packaged scaffolding assets directory.

    Assets ship inside the package rather than at the repository root, so they
    resolve identically from a wheel, an editable install, and a plain checkout.
    Resolving them relative to the repository (or the cwd) only ever worked for
    a source checkout, which left `cadabby init` broken for installed users.
    """
    assets_dir = Path(__file__).resolve().parent / "assets"
    if not assets_dir.is_dir():
        raise RuntimeError(
            f"Cadabby assets directory not found at {assets_dir}. "
            "The installation looks incomplete; reinstall the cadabby package."
        )
    return assets_dir


def get_repo_plugin_dir() -> Path:
    """Return path to the bundled Antigravity plugin directory."""
    plugin_dir = get_assets_dir() / "plugins" / "cadabby"
    if not plugin_dir.is_dir():
        raise RuntimeError(
            f"Cadabby plugin directory not found at {plugin_dir}. "
            "The installation looks incomplete; reinstall the cadabby package."
        )
    return plugin_dir


# Vault files the engine owns and may rewrite at any time, as (asset subdir,
# vault-relative destination). The thin-shim rule (§7.4) is what makes that
# safe: a shim names behavior rather than carrying it, so regenerating one
# cannot destroy a user edit. Everything absent from this list -- .cadabby.json,
# AGENTS.md, CLAUDE.md, GEMINI.md, STYLE.md, index.md -- is user-owned, written
# once by `init` and never by `install`. Collapsing the two categories under a
# single --force switch is what makes `init --force` a reset rather than a
# refresh, which is precisely why `install` exists as a separate verb.
ENGINE_OWNED_SHIMS: tuple[tuple[str, str], ...] = (
    ("commands", f"{DIR_CLAUDE}/commands"),
    ("skills", f"{DIR_AGENTS}/skills"),
)


def _write_within(root: Path, dst: Path, data: str | bytes) -> None:
    """atomic_write to `dst`, refusing a destination a symlink sends outside `root` (§8).

    A vault is a folder anyone can hand you, and `install` is the documented
    re-run verb: `.agents/skills/librarian/SKILL.md -> ~/.ssh/authorized_keys`
    made it overwrite the user's keys with skill text. A symlink that stays
    inside `root` (`CLAUDE.md -> AGENTS.md`) is still written through.
    """
    target = contained(root, dst)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, data)


def _sync_tree(
    src_root: Path,
    dest_root: Path,
    dry_run: bool = False,
    skip: frozenset[str] = frozenset(),
    *,
    within: Path | None = None,
) -> list[str]:
    """Copy src_root onto dest_root, writing only files whose bytes differ.

    Returns the vault-relative paths actually changed, so an unchanged install
    can report itself as a no-op (§7.6 idempotency) rather than claiming work.
    Every write must land inside `within` (default: dest_root itself).
    """
    root = within if within is not None else dest_root
    changed: list[str] = []
    for src in sorted(src_root.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(src_root).as_posix()
        if rel in skip:
            continue
        dst = dest_root / rel
        data = src.read_bytes()
        if dst.exists() and dst.read_bytes() == data:
            continue
        changed.append(rel)
        if not dry_run:
            _write_within(root, dst, data)
    return changed


def _write_bound_plugin(repo_plugin: Path, dest: Path, mcp_cfg: dict[str, Any]) -> None:
    """Copy the bundled plugin tree to dest and write its vault-bound mcp_config.json."""
    _sync_tree(repo_plugin, dest, skip=frozenset({"mcp_config.json"}))
    _write_within(dest, dest / "mcp_config.json", json.dumps(mcp_cfg, indent=2) + "\n")


def refresh_vault_shims(vault_path: Path | str, dry_run: bool = False) -> tuple[bool, str]:
    """Bring a vault's engine-owned shims up to the running version (§7.4).

    Without this there is no non-destructive way to pick up a fixed slash
    command or persona runbook: `init` declines to touch files that exist, and
    `init --force` overwrites the user-owned documents alongside them.
    """
    vault = Path(vault_path).resolve()
    assets = get_assets_dir()

    changed: list[str] = []
    for subdir, rel_dest in ENGINE_OWNED_SHIMS:
        src_root = assets / subdir
        if src_root.is_dir():
            changed += [f"{rel_dest}/{rel}" for rel in _sync_tree(src_root, vault / rel_dest, dry_run, within=vault)]

    if not changed:
        return True, f"Vault shims already up-to-date in {vault} (unchanged)."
    prefix = "[dry-run] Would refresh" if dry_run else "Refreshed"
    return True, f"{prefix} {len(changed)} vault shim(s) in {vault}: {', '.join(changed)}"


class InitRefusedError(CadabbyError, ValueError):
    """`init` declined to proceed because doing so would hide existing notes (§7.5)
    or plant a vault inside another one (§5.3)."""

    code = INVALID_ARGUMENT


def enclosing_vault(target: Path) -> Path | None:
    """The nearest ancestor of `target` that holds `.cadabby.json`, or None (§5.3).

    Only the Cadabby marker counts: a plain `.obsidian/` folder above is a
    notes tree nobody scans, while a Cadabby vault above would index the new
    vault's CLAUDE.md, STYLE.md, index.md... as notes and fail gate 1.
    Deliberately not find_vault_root(): that also accepts `.obsidian/` and
    consults $CADABBY_VAULT, neither of which says where `target` sits.
    """
    return next((p for p in target.parents if (p / FILE_CONFIG).is_file()), None)


# User-owned vault documents (§7.6): written once by `init`, overwritten only
# with --force, never touched by `install`.
_USER_OWNED_FILES: tuple[str, ...] = (".gitignore", FILE_AGENTS, FILE_CLAUDE, FILE_GEMINI, FILE_STYLE, FILE_INDEX)


def init_vault(
    target: Path | str,
    *,
    vault_name: str | None = None,
    force: bool = False,
    obsidian: bool = False,
    obsidian_templates: bool = False,
) -> list[str]:
    """Scaffold a vault at `target` (§2.1, §7.6) and return the report lines to show.

    Three ownership tiers, three rules. Engine-owned shims (`.claude/commands`,
    `.agents/skills`, the Antigravity plugin's files) are synced exactly as
    `install` syncs them. User-owned documents and the two vault-bound MCP
    configs are written once and replaced only with `force`: a hand-edited
    `.mcp.json` must not be silently rebound. Generated files are seeded.

    Raises:
        InitRefusedError: if `obsidian_templates` would reserve a folder that
            already holds notes, which would drop a domain out of the vault;
            or if `target` lies inside another Cadabby vault, whose scans
            would read this vault's files as notes. `force` overrides neither.
    """
    root = Path(target).resolve()
    name = vault_name or root.name or "vault"
    assets = get_assets_dir()
    vault_tpl = assets / "vault"
    report: list[str] = []

    def write_once(src: Path, dst: Path) -> bool:
        """Copy a user-owned template, honoring `force`. True if dst was written."""
        if not src.exists():
            return False
        if dst.exists():
            if not force:
                report.append(f"Existing file left untouched: {dst.name} (use --force to overwrite)")
                return False
            report.append(f"Overwrote existing file: {dst.name}")
        _write_within(root, dst, src.read_bytes())
        return True

    def bind_mcp(dst: Path) -> None:
        _write_within(root, dst, json.dumps({"mcpServers": {"cadabby": make_mcp_server_entry(root)}}, indent=2) + "\n")

    # Refuse before writing anything, so a refused init leaves no half-vault.
    if (outer := enclosing_vault(root)) is not None:
        raise InitRefusedError(
            f"Refusing to create a vault inside the vault at {outer}: its scans would read this "
            f"vault's files as notes and fail. To keep this material in {outer.name}, make it a "
            f"cognitive domain folder there (it needs no init); otherwise choose a path outside it."
        )
    declared = obsidian_template_dir(root)
    tpl_dest = declared if declared is not None else root / DIR_TEMPLATES
    if obsidian_templates and declared is None and tpl_dest.is_dir() and any(tpl_dest.rglob("*.md")):
        raise InitRefusedError(
            f"Refusing to use {DIR_TEMPLATES}/ for templates: it already holds notes, and declaring it "
            f"would hide them from the vault. Point Obsidian at another folder in "
            f"{DIR_OBSIDIAN}/{FILE_OBSIDIAN_TEMPLATES} and re-run."
        )

    for folder in (DIR_RAW, DIR_WIKI):
        (root / folder).mkdir(parents=True, exist_ok=True)

    # User-owned: the config carries the vault name, so it is rendered, not copied.
    cfg_dst = root / FILE_CONFIG
    if cfg_dst.exists() and not force:
        report.append(f"Existing file left untouched: {FILE_CONFIG} (use --force to overwrite)")
    else:
        cfg_data = json.loads((vault_tpl / FILE_CONFIG).read_text("utf-8"))
        cfg_data["vault_name"] = name
        _write_within(root, cfg_dst, _dump_json_like(cfg_data))
    for filename in _USER_OWNED_FILES:
        write_once(vault_tpl / filename, root / filename)

    # Vault-bound MCP configs: write-once like user files, then bound to this vault.
    if write_once(vault_tpl / FILE_MCP, root / FILE_MCP):
        bind_mcp(root / FILE_MCP)
    plugin_dst = root / DIR_AGENTS / "plugins" / "cadabby"
    if write_once(get_repo_plugin_dir() / "mcp_config.json", plugin_dst / "mcp_config.json"):
        bind_mcp(plugin_dst / "mcp_config.json")

    # Engine-owned: identical to what `install` refreshes.
    for subdir, rel_dest in ENGINE_OWNED_SHIMS:
        _sync_tree(assets / subdir, root / rel_dest, within=root)
    _sync_tree(get_repo_plugin_dir(), plugin_dst, skip=frozenset({"mcp_config.json"}), within=root)

    # Generated: seeded empty, maintained by the engine from here on.
    if not (root / FILE_LOG).exists():
        _write_within(root, root / FILE_LOG, LEDGER_HEADER)

    # Obsidian. --obsidian-templates implies --obsidian: starter templates are
    # useless to a vault Obsidian cannot open.
    if obsidian or obsidian_templates:
        (root / DIR_OBSIDIAN).mkdir(parents=True, exist_ok=True)
        (root / DIR_RAW / "attachments").mkdir(parents=True, exist_ok=True)
        write_once(vault_tpl / "obsidian" / "app.json", root / DIR_OBSIDIAN / "app.json")

    # Starter templates are opt-in because they reserve a folder. An existing
    # declaration always wins, including under --force (§7.5).
    if obsidian_templates:
        tpl_dest.mkdir(parents=True, exist_ok=True)
        for src in sorted((vault_tpl / DIR_TEMPLATES).glob("*.md")):
            write_once(src, tpl_dest / src.name)
        if declared is None:
            write_once(
                vault_tpl / "obsidian" / FILE_OBSIDIAN_TEMPLATES,
                root / DIR_OBSIDIAN / FILE_OBSIDIAN_TEMPLATES,
            )

    report.append(f"Initialized Cadabby vault '{name}' in {root}")
    return report


def _is_cadabby_entry(entry: Any) -> bool:
    """Return True if an mcpServers entry looks like one Cadabby itself wrote.

    Either form counts: a console script named cadabby, or `-m cadabby`.
    """
    if not isinstance(entry, dict):
        return False
    raw_args = entry.get("args")
    args = [str(a) for a in raw_args] if isinstance(raw_args, (list, tuple)) else []
    return Path(str(entry.get("command", ""))).name.startswith("cadabby") or "cadabby" in args


def _entry_vault(entry: Any) -> str | None:
    """Return the vault an mcpServers entry is bound to, or None if unbound."""
    if not isinstance(entry, dict):
        return None
    raw_args = entry.get("args")
    args = [str(a) for a in raw_args] if isinstance(raw_args, (list, tuple)) else []
    if "--vault" in args:
        index = args.index("--vault")
        if index + 1 < len(args):
            return args[index + 1]
    return None


def _is_stale_self(existing: Any, desired: dict[str, Any]) -> bool:
    """Return True if `existing` is Cadabby's own entry for the same vault.

    This is the case `install` exists to repair: the recorded interpreter path
    is absolute (§7.2), so rebuilding a venv or upgrading Python leaves an entry
    that names a binary which no longer exists, and the MCP server simply fails
    to start. Refreshing it is not a conflict -- Cadabby wrote it, it points at
    the same vault, and its only defect is naming a dead interpreter. An entry
    bound to a *different* vault, or one Cadabby did not write, is a real
    conflict and still requires --force.
    """
    return _is_cadabby_entry(existing) and _entry_vault(existing) == _entry_vault(desired)


def default_antigravity_path(vault_path: Path | str | None = None, is_global: bool = False) -> Path:
    """Default path for Antigravity plugin installation.

    When is_global is False and a vault_path is provided, targets the workspace
    plugin at <vault>/.agents/plugins/cadabby. Otherwise targets the user-global
    directory at ~/.gemini/antigravity/plugins/cadabby.
    """
    if is_global or vault_path is None:
        return Path.home() / ".gemini" / "antigravity" / "plugins" / "cadabby"
    return Path(vault_path).resolve() / DIR_AGENTS / "plugins" / "cadabby"


def default_claude_path(vault_path: Path | str | None = None, is_global: bool = False) -> Path:
    """Default path for Claude Code configuration.

    When is_global is False and a vault_path is provided, targets the workspace
    config at <vault>/.mcp.json. Otherwise targets the user-global file at
    ~/.claude.json.
    """
    if is_global or vault_path is None:
        return Path.home() / ".claude.json"
    return Path(vault_path).resolve() / FILE_MCP


def _wrong_kind(dest: Path, want: str) -> str | None:
    """Reject a destination whose kind cannot hold what we are about to write.

    The two harnesses take structurally different destinations -- Antigravity a
    plugin directory, Claude a JSON config file -- so a path that is correct for
    one is a crash for the other. Checking before any write turns a raw errno
    from deep inside a copytree or a read_text into a message that names the
    mistake, and keeps --dry-run from previewing an install that cannot happen.

    Deliberately not overridable by --force: the flag means "overwrite the
    Cadabby thing you found", not "delete whatever unrelated file or directory
    happens to sit at this path".
    """
    if want == "dir" and dest.exists() and not dest.is_dir():
        return (
            f"Conflict: {dest} is a file, but the Antigravity plugin must be a "
            f"directory. Point --path at a directory."
        )
    if want == "dir" and (outer := enclosing_vault(dest)) is not None:
        top = dest.relative_to(outer).parts[0]
        if not top.startswith("."):
            return (
                f"Conflict: {dest} sits inside vault {outer} under '{top}/', which is scanned as a "
                f"cognitive domain. Omit --path to use {outer / DIR_AGENTS / 'plugins' / 'cadabby'}."
            )
    if want == "file" and dest.is_dir():
        return (
            f"Conflict: {dest} is a directory, but the Claude MCP configuration "
            f"must be a JSON file. Point --path at a file."
        )
    return None


def _is_replaceable_plugin_dir(dest: Path) -> bool:
    """Whether `dest` is a real directory Cadabby may delete: empty, or a Cadabby plugin.

    `--force` means "overwrite the Cadabby thing you found" (§7.6) and
    `--uninstall` "removes exactly what install added". Neither may rmtree an
    arbitrary directory that a mistyped --path happens to name, so a non-empty
    directory qualifies only if its plugin.json names the cadabby plugin.
    Symlinks are excluded: callers unlink those, which never touches the target.
    """
    if dest.is_symlink() or not dest.is_dir():
        return False
    if not any(dest.iterdir()):
        return True
    try:
        manifest = json.loads((dest / "plugin.json").read_text("utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(manifest, dict) and manifest.get("name") == "cadabby"


def _refuse_to_delete(dest: Path, action: str) -> str:
    return (
        f"Conflict: {dest} is not a Cadabby plugin directory (no plugin.json naming 'cadabby'); "
        f"refusing to delete it{action}. Point --path at the plugin directory."
    )


def install_antigravity(
    dest_path: Path | None = None,
    vault_path: Path | str | None = None,
    is_global: bool = False,
    force: bool = False,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Install Antigravity plugin with conflict detection and idempotency.

    If vault_path is provided, configures mcp_config.json with the absolute
    path to the vault so Antigravity can start the MCP server without relying on
    process working directory.
    """
    dest = (dest_path or default_antigravity_path(vault_path, is_global)).absolute()
    if (problem := _wrong_kind(dest, "dir")) is not None:
        return False, problem
    repo_plugin = get_repo_plugin_dir().resolve()
    desired_entry = make_mcp_server_entry(vault_path)
    if vault_path is None:
        return _install_antigravity_unbound(dest, repo_plugin, desired_entry, force=force, dry_run=dry_run)
    return _install_antigravity_bound(
        dest, repo_plugin, desired_entry, Path(vault_path).resolve(), force=force, dry_run=dry_run
    )


def _install_antigravity_unbound(
    dest: Path, repo_plugin: Path, desired_entry: dict[str, Any], *, force: bool, dry_run: bool
) -> tuple[bool, str]:
    """No vault: link (or, where links fail, copy) the packaged plugin directory."""
    if dest.is_symlink():
        target = dest.resolve()
        if target == repo_plugin:
            return True, f"Antigravity plugin already installed at {dest} (unchanged)."
        elif not force:
            return False, f"Conflict: {dest} links to {target}. Use --force to overwrite."
    elif dest.exists():
        if _plugin_entry_lenient(dest / "mcp_config.json") == desired_entry:
            return True, f"Antigravity plugin already installed at {dest} (unchanged)."
        if not force:
            return False, f"Conflict: Directory already exists at {dest}. Use --force to overwrite."
        if not _is_replaceable_plugin_dir(dest):
            return False, _refuse_to_delete(dest, ", even with --force")

    if dry_run:
        return True, f"[dry-run] Would link {dest} -> {repo_plugin}"

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink() or dest.exists():
        if dest.is_symlink():
            dest.unlink()
        else:
            shutil.rmtree(dest)

    try:
        dest.symlink_to(repo_plugin)
        action = "Linked"
    except OSError:
        shutil.copytree(repo_plugin, dest)
        action = "Copied"

    return True, f"{action} Antigravity plugin to {dest}"


def _plugin_entry_lenient(mcp_file: Path) -> Any:
    """The `mcpServers.cadabby` entry of an existing plugin config, or None if absent or unreadable.

    Only the unbound install's "already installed?" probe uses this, and an
    unreadable file there just means "not ours as-is". The bound install
    refuses to rewrite a file it cannot read instead.
    """
    if not mcp_file.exists():
        return None
    try:
        cur_cfg = json.loads(mcp_file.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(cur_cfg, dict):
        return None
    mcp_servers = cur_cfg.get("mcpServers")
    return mcp_servers.get("cadabby") if isinstance(mcp_servers, dict) else None


def _install_antigravity_bound(
    dest: Path,
    repo_plugin: Path,
    desired_entry: dict[str, Any],
    resolved_vault: Path,
    *,
    force: bool,
    dry_run: bool,
) -> tuple[bool, str]:
    """A vault: a copied plugin whose mcp_config.json names that vault."""
    desired_mcp_cfg = {"mcpServers": {"cadabby": desired_entry}}
    configured = f"Configured Antigravity plugin at {dest} (vault: {resolved_vault})"

    if dest.is_symlink():
        if not force:
            return False, f"Conflict: {dest} links to {dest.resolve()}. Use --force to overwrite with vault-configured plugin."
        if dry_run:
            return True, f"[dry-run] Would replace symlink at {dest} with vault-configured plugin (vault: {resolved_vault})"
        dest.unlink()
        _write_bound_plugin(repo_plugin, dest, desired_mcp_cfg)
        return True, configured

    if not dest.exists():
        if dry_run:
            return True, f"[dry-run] Would copy plugin to {dest} and configure vault: {resolved_vault}"
        _write_bound_plugin(repo_plugin, dest, desired_mcp_cfg)
        return True, configured

    mcp_file = dest / "mcp_config.json"
    if not mcp_file.exists():
        if not force:
            return False, f"Conflict: Directory already exists at {dest}. Use --force to overwrite."
        if not _is_replaceable_plugin_dir(dest):
            return False, _refuse_to_delete(dest, ", even with --force")
        if dry_run:
            return True, f"[dry-run] Would configure Antigravity plugin at {dest} (vault: {resolved_vault})"
        shutil.rmtree(dest)
        _write_bound_plugin(repo_plugin, dest, desired_mcp_cfg)
        return True, configured

    try:
        cur_cfg = json.loads(mcp_file.read_text("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        # Same refusal as install_claude: rewriting a file we could not
        # read would silently discard every other server it configures.
        return False, f"Failed to parse existing JSON at {mcp_file}: {e}. Aborting."
    if not isinstance(cur_cfg, dict) or not isinstance(cur_cfg.get("mcpServers", {}), dict):
        return False, f"Unexpected structure in {mcp_file}: expected an object with 'mcpServers'. Aborting."
    existing_entry = cur_cfg.get("mcpServers", {}).get("cadabby")
    entry_stale = existing_entry != desired_entry
    if entry_stale and existing_entry is not None and not _is_stale_self(existing_entry, desired_entry) and not force:
        return False, f"Conflict: Antigravity plugin at {dest} is configured for another vault. Use --force to overwrite."

    # Re-sync the plugin's own files, not just its MCP entry. They are
    # engine-owned shims (§7.4), and without this a vault installed once
    # keeps its original skill and agent manifests forever -- the exact
    # staleness this command is supposed to cure.
    changed = _sync_tree(repo_plugin, dest, dry_run, skip=frozenset({"mcp_config.json"}))

    if not changed and not entry_stale:
        return True, f"Antigravity plugin already installed at {dest} (unchanged)."
    if dry_run:
        return True, f"[dry-run] Would update {dest} (vault: {resolved_vault})"

    if entry_stale:
        cur_cfg.setdefault("mcpServers", {})["cadabby"] = desired_entry
        _write_within(dest, mcp_file, _dump_json_like(cur_cfg, mcp_file.read_text("utf-8")))
    return True, configured


def uninstall_antigravity(
    dest_path: Path | None = None,
    vault_path: Path | str | None = None,
    is_global: bool = False,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Uninstall Antigravity plugin."""
    dest = (dest_path or default_antigravity_path(vault_path, is_global)).absolute()
    if (problem := _wrong_kind(dest, "dir")) is not None:
        return False, problem
    if not dest.exists() and not dest.is_symlink():
        return True, f"Antigravity plugin not found at {dest} (nothing to uninstall)."
    if not dest.is_symlink() and not _is_replaceable_plugin_dir(dest):
        return False, _refuse_to_delete(dest, "; nothing removed")

    if dry_run:
        return True, f"[dry-run] Would remove {dest}"

    if dest.is_symlink():
        dest.unlink()
    else:
        shutil.rmtree(dest)

    return True, f"Removed Antigravity plugin from {dest}"


def _dump_json_like(data: Any, original: str = "") -> str:
    """Serialize `data` in the style `original` used: indent, compactness, trailing newline, raw non-ASCII.

    `~/.claude.json` belongs to the user and to Claude Code (§7.6), so a merge
    must not reformat what it did not change. For files written by
    JavaScript's JSON.stringify this round-trips byte for byte apart from the
    edited key: both print numbers in their shortest form. A brand-new file
    gets two-space indent and a trailing newline.
    """
    stripped = original.strip()
    if stripped and "\n" not in stripped:
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    else:
        match = re.search(r"\n([ \t]+)\S", original)
        indent: str | int = match.group(1) if match else 2
        text = json.dumps(data, ensure_ascii=False, indent=indent)
    return text + ("\n" if not original or original.endswith("\n") else "")


def _read_claude_config(dest: Path) -> tuple[dict[str, Any] | None, str, str | None, str | None]:
    """Read and validate a Claude MCP config file: (cfg_data, raw_text, error, token).

    `token` is the sha256 of the bytes read, so the write can refuse to replace
    a file another program (Claude Code rewrites it constantly) changed since.
    """
    if not dest.exists():
        return {}, "", None, None
    try:
        data = dest.read_bytes()
        raw = data.decode("utf-8")
        cfg = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
        return None, "", f"Failed to parse existing JSON at {dest}: {e}. Aborting.", None
    if not isinstance(cfg, dict) or not isinstance(cfg.get("mcpServers", {}), dict):
        return None, "", f"Unexpected structure in {dest}: expected an object with 'mcpServers'. Aborting.", None
    return cfg, raw, None, compute_bytes_sha256(data)


def _claude_dest(dest_path: Path | None, vault_path: Path | str | None, is_global: bool) -> Path:
    """Where the Claude config lives. The vault's own `.mcp.json` must stay inside the vault (§8)."""
    if dest_path is not None:
        return dest_path.resolve()  # chosen explicitly by the user
    default = default_claude_path(vault_path, is_global)
    if vault_path is not None and not is_global:
        return contained(Path(vault_path), default)
    return default.resolve()


def install_claude(
    dest_path: Path | None = None,
    vault_path: Path | str | None = None,
    is_global: bool = False,
    force: bool = False,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Non-destructively register Cadabby MCP server in Claude configuration."""
    dest = _claude_dest(dest_path, vault_path, is_global)
    if (problem := _wrong_kind(dest, "file")) is not None:
        return False, problem
    cfg_data, existing_content, err, token = _read_claude_config(dest)
    if err is not None or cfg_data is None:
        return False, err or ""

    mcp_servers = cfg_data.setdefault("mcpServers", {})
    existing_entry = mcp_servers.get("cadabby")
    desired_entry = make_mcp_server_entry(vault_path)

    if existing_entry == desired_entry:
        return True, f"Claude MCP server already registered in {dest} (unchanged)."

    rebinding = _is_stale_self(existing_entry, desired_entry)
    if existing_entry is not None and not rebinding and not force:
        return False, f"Conflict: mcpServers.cadabby already defined in {dest}. Use --force to overwrite."

    mcp_servers["cadabby"] = desired_entry
    new_content = _dump_json_like(cfg_data, existing_content)

    if existing_content == new_content:
        return True, f"Claude configuration in {dest} already up-to-date (unchanged)."

    if dry_run:
        return True, f"[dry-run] Would update {dest} with mcpServers.cadabby"

    dest.parent.mkdir(parents=True, exist_ok=True)
    atomic_replace_checked(dest, new_content, expected_hash=token)
    vault_msg = f" (vault: {Path(vault_path).resolve()})" if vault_path else ""
    verb = "Re-bound stale Cadabby MCP entry in" if rebinding else "Registered Cadabby MCP server in"
    return True, f"{verb} {dest}{vault_msg}"


def uninstall_claude(
    dest_path: Path | None = None,
    vault_path: Path | str | None = None,
    is_global: bool = False,
    dry_run: bool = False,
    force: bool = False,
) -> tuple[bool, str]:
    """Uninstall Cadabby MCP entry from Claude configuration, preserving other keys."""
    dest = _claude_dest(dest_path, vault_path, is_global)
    if (problem := _wrong_kind(dest, "file")) is not None:
        return False, problem
    if not dest.exists():
        return True, f"Claude configuration not found at {dest} (nothing to uninstall)."

    cfg_data, existing_content, err, token = _read_claude_config(dest)
    if err is not None or cfg_data is None:
        return False, err or ""
    if not existing_content.strip():
        return True, f"Claude configuration at {dest} is empty."

    mcp_servers = cfg_data.get("mcpServers", {})
    if "cadabby" not in mcp_servers:
        return True, f"No Cadabby MCP entry in {dest} (nothing to uninstall)."
    if not _is_cadabby_entry(mcp_servers["cadabby"]) and not force:
        # Install refused this entry as a conflict; uninstall must not delete it
        # either (§7.6: --uninstall removes exactly what install added).
        return False, f"Conflict: mcpServers.cadabby in {dest} was not written by Cadabby. Use --force to remove it."

    del mcp_servers["cadabby"]
    new_content = _dump_json_like(cfg_data, existing_content)

    if dry_run:
        return True, f"[dry-run] Would remove mcpServers.cadabby from {dest}"

    atomic_replace_checked(dest, new_content, expected_hash=token)
    return True, f"Removed Cadabby MCP entry from {dest}"


def run_install(
    antigravity: bool = False,
    claude: bool = False,
    all_targets: bool = False,
    is_global: bool = False,
    uninstall: bool = False,
    dry_run: bool = False,
    dest_path: Path | None = None,
    vault_path: Path | str | None = None,
    force: bool = False,
) -> int:
    """Execute harness installer based on CLI parameters."""
    target_ag = antigravity or all_targets
    target_claude = claude or all_targets

    if not target_ag and not target_claude:
        raise InvalidArgumentError("Please specify a target harness: --antigravity, --claude, or --all")

    # One --path cannot serve both harnesses: Antigravity wants a plugin
    # directory and Claude wants a JSON config file, so no single value is
    # correct for both. Refusing here rather than per-target matters because
    # the targets are applied in sequence -- Antigravity would create its
    # directory, Claude would then fail on it, and there is no rollback.
    if dest_path is not None and target_ag and target_claude:
        raise InvalidArgumentError(
            "--path takes a single destination, but this installs to two targets of "
            "different kinds (an Antigravity plugin directory and a Claude JSON config "
            "file). Run the command once per harness."
        )

    success = True
    messages: list[str] = []

    if target_ag:
        if uninstall:
            ok, msg = uninstall_antigravity(
                dest_path=dest_path,
                vault_path=vault_path,
                is_global=is_global,
                dry_run=dry_run,
            )
        else:
            ok, msg = install_antigravity(
                dest_path=dest_path,
                vault_path=vault_path,
                is_global=is_global,
                force=force,
                dry_run=dry_run,
            )
        success = success and ok
        messages.append(msg)

    if target_claude:
        if uninstall:
            ok, msg = uninstall_claude(
                dest_path=dest_path,
                vault_path=vault_path,
                is_global=is_global,
                dry_run=dry_run,
                force=force,
            )
        else:
            ok, msg = install_claude(
                dest_path=dest_path,
                vault_path=vault_path,
                is_global=is_global,
                force=force,
                dry_run=dry_run,
            )
        success = success and ok
        messages.append(msg)

    # Refresh the vault-local shims that belong to no single harness. Skipped
    # for --global (which configures the user environment, not a vault) and for
    # --uninstall (which removes what install added to harness config, not the
    # vault's own scaffolding -- that is what deleting the vault is for).
    if success and not uninstall and not is_global and vault_path is not None:
        ok, msg = refresh_vault_shims(vault_path, dry_run=dry_run)
        success = success and ok
        messages.append(msg)

    for msg in messages:
        print(msg)

    # A refused install (a conflicting entry without --force, an unparseable
    # config) is "fix the environment", never exit 1, which means the vault
    # has findings (§5.4).
    return EXIT_OK if success else EXIT_ENVIRONMENT


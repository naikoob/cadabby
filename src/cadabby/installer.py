"""Harness configuration installer and idempotent non-destructive merge.

Conforms strictly to Cadabby Technical Specification §7.6.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

from cadabby.fsutil import atomic_write


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
    ("commands", ".claude/commands"),
    ("skills", ".agents/skills"),
)


def _sync_tree(src_root: Path, dest_root: Path, dry_run: bool = False, skip: frozenset[str] = frozenset()) -> list[str]:
    """Copy src_root onto dest_root, writing only files whose bytes differ.

    Returns the vault-relative paths actually changed, so an unchanged install
    can report itself as a no-op (§7.6 idempotency) rather than claiming work.
    """
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
            dst.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(dst, data.decode("utf-8"))
    return changed


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
            changed += [f"{rel_dest}/{rel}" for rel in _sync_tree(src_root, vault / rel_dest, dry_run)]

    if not changed:
        return True, f"Vault shims already up-to-date in {vault} (unchanged)."
    prefix = "[dry-run] Would refresh" if dry_run else "Refreshed"
    return True, f"{prefix} {len(changed)} vault shim(s) in {vault}: {', '.join(changed)}"


def _is_cadabby_entry(entry: Any) -> bool:
    """Return True if an mcpServers entry looks like one Cadabby itself wrote.

    Either form counts: a console script named cadabby, or `-m cadabby`.
    """
    if not isinstance(entry, dict):
        return False
    args = [str(a) for a in entry.get("args") or []]
    return Path(str(entry.get("command", ""))).name.startswith("cadabby") or "cadabby" in args


def _entry_vault(entry: Any) -> str | None:
    """Return the vault an mcpServers entry is bound to, or None if unbound."""
    if not isinstance(entry, dict):
        return None
    args = [str(a) for a in entry.get("args") or []]
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
    return Path(vault_path).resolve() / ".agents" / "plugins" / "cadabby"


def default_claude_path(vault_path: Path | str | None = None, is_global: bool = False) -> Path:
    """Default path for Claude Code configuration.

    When is_global is False and a vault_path is provided, targets the workspace
    config at <vault>/.mcp.json. Otherwise targets the user-global file at
    ~/.claude.json.
    """
    if is_global or vault_path is None:
        return Path.home() / ".claude.json"
    return Path(vault_path).resolve() / ".mcp.json"


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
    if want == "file" and dest.is_dir():
        return (
            f"Conflict: {dest} is a directory, but the Claude MCP configuration "
            f"must be a JSON file. Point --path at a file."
        )
    return None


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
    desired_mcp_cfg = {"mcpServers": {"cadabby": desired_entry}}

    # Case 1: Unbound install (no specific vault)
    if vault_path is None:
        if dest.is_symlink():
            target = dest.resolve()
            if target == repo_plugin:
                return True, f"Antigravity plugin already installed at {dest} (unchanged)."
            elif not force:
                return False, f"Conflict: {dest} links to {target}. Use --force to overwrite."
        elif dest.exists():
            mcp_file = dest / "mcp_config.json"
            if mcp_file.exists():
                try:
                    cur_cfg = json.loads(mcp_file.read_text("utf-8"))
                    if cur_cfg.get("mcpServers", {}).get("cadabby") == desired_entry:
                        return True, f"Antigravity plugin already installed at {dest} (unchanged)."
                except Exception:
                    pass
            if not force:
                return False, f"Conflict: Directory already exists at {dest}. Use --force to overwrite."

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

    # Case 2: Bound install (vault_path is provided)
    resolved_vault = Path(vault_path).resolve()
    if dest.is_symlink():
        if not force:
            return False, f"Conflict: {dest} links to {dest.resolve()}. Use --force to overwrite with vault-configured plugin."
        if dry_run:
            return True, f"[dry-run] Would replace symlink at {dest} with vault-configured plugin (vault: {resolved_vault})"
        dest.unlink()
        shutil.copytree(repo_plugin, dest)
        atomic_write(dest / "mcp_config.json", json.dumps(desired_mcp_cfg, indent=2) + "\n")
        return True, f"Configured Antigravity plugin at {dest} (vault: {resolved_vault})"

    if dest.exists():
        mcp_file = dest / "mcp_config.json"
        if mcp_file.exists():
            try:
                cur_cfg = json.loads(mcp_file.read_text("utf-8"))
            except Exception:
                cur_cfg = {}
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
                atomic_write(mcp_file, json.dumps(cur_cfg, indent=2) + "\n")
            return True, f"Configured Antigravity plugin at {dest} (vault: {resolved_vault})"
        else:
            if not force:
                return False, f"Conflict: Directory already exists at {dest}. Use --force to overwrite."
            if dry_run:
                return True, f"[dry-run] Would configure Antigravity plugin at {dest} (vault: {resolved_vault})"
            shutil.rmtree(dest)
            shutil.copytree(repo_plugin, dest)
            atomic_write(dest / "mcp_config.json", json.dumps(desired_mcp_cfg, indent=2) + "\n")
            return True, f"Configured Antigravity plugin at {dest} (vault: {resolved_vault})"

    # dest does not exist
    if dry_run:
        return True, f"[dry-run] Would copy plugin to {dest} and configure vault: {resolved_vault}"

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(repo_plugin, dest)
    atomic_write(dest / "mcp_config.json", json.dumps(desired_mcp_cfg, indent=2) + "\n")
    return True, f"Configured Antigravity plugin at {dest} (vault: {resolved_vault})"


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

    if dry_run:
        return True, f"[dry-run] Would remove {dest}"

    if dest.is_symlink():
        dest.unlink()
    else:
        shutil.rmtree(dest)

    return True, f"Removed Antigravity plugin from {dest}"


def install_claude(
    dest_path: Path | None = None,
    vault_path: Path | str | None = None,
    is_global: bool = False,
    force: bool = False,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Non-destructively register Cadabby MCP server in Claude configuration."""
    dest = (dest_path or default_claude_path(vault_path, is_global)).resolve()
    if (problem := _wrong_kind(dest, "file")) is not None:
        return False, problem
    existing_content = ""
    cfg_data: dict[str, Any] = {}

    if dest.exists():
        existing_content = dest.read_text("utf-8")
        if existing_content.strip():
            try:
                cfg_data = json.loads(existing_content)
            except (json.JSONDecodeError, ValueError) as e:
                return False, f"Failed to parse existing JSON at {dest}: {e}. Aborting."

    mcp_servers = cfg_data.setdefault("mcpServers", {})
    existing_entry = mcp_servers.get("cadabby")
    desired_entry = make_mcp_server_entry(vault_path)

    if existing_entry == desired_entry:
        return True, f"Claude MCP server already registered in {dest} (unchanged)."

    rebinding = _is_stale_self(existing_entry, desired_entry)
    if existing_entry is not None and not rebinding and not force:
        return False, f"Conflict: mcpServers.cadabby already defined in {dest}. Use --force to overwrite."

    mcp_servers["cadabby"] = desired_entry
    new_content = json.dumps(cfg_data, indent=2) + "\n"

    if existing_content == new_content:
        return True, f"Claude configuration in {dest} already up-to-date (unchanged)."

    if dry_run:
        return True, f"[dry-run] Would update {dest} with mcpServers.cadabby"

    dest.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(dest, new_content)
    vault_msg = f" (vault: {Path(vault_path).resolve()})" if vault_path else ""
    verb = "Re-bound stale Cadabby MCP entry in" if rebinding else "Registered Cadabby MCP server in"
    return True, f"{verb} {dest}{vault_msg}"


def uninstall_claude(
    dest_path: Path | None = None,
    vault_path: Path | str | None = None,
    is_global: bool = False,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Uninstall Cadabby MCP entry from Claude configuration, preserving other keys."""
    dest = (dest_path or default_claude_path(vault_path, is_global)).resolve()
    if (problem := _wrong_kind(dest, "file")) is not None:
        return False, problem
    if not dest.exists():
        return True, f"Claude configuration not found at {dest} (nothing to uninstall)."

    existing_content = dest.read_text("utf-8")
    if not existing_content.strip():
        return True, f"Claude configuration at {dest} is empty."

    try:
        cfg_data = json.loads(existing_content)
    except (json.JSONDecodeError, ValueError) as e:
        return False, f"Failed to parse existing JSON at {dest}: {e}. Aborting."

    mcp_servers = cfg_data.get("mcpServers", {})
    if "cadabby" not in mcp_servers:
        return True, f"No Cadabby MCP entry in {dest} (nothing to uninstall)."

    del mcp_servers["cadabby"]
    new_content = json.dumps(cfg_data, indent=2) + "\n"

    if dry_run:
        return True, f"[dry-run] Would remove mcpServers.cadabby from {dest}"

    atomic_write(dest, new_content)
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
        print("Please specify a target harness: --antigravity, --claude, or --all", file=sys.stderr)
        return 1

    # One --path cannot serve both harnesses: Antigravity wants a plugin
    # directory and Claude wants a JSON config file, so no single value is
    # correct for both. Refusing here rather than per-target matters because
    # the targets are applied in sequence -- Antigravity would create its
    # directory, Claude would then fail on it, and there is no rollback.
    if dest_path is not None and target_ag and target_claude:
        print(
            "--path takes a single destination, but this installs to two targets of "
            "different kinds (an Antigravity plugin directory and a Claude JSON config "
            "file). Run the command once per harness.",
            file=sys.stderr,
        )
        return 1

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

    return 0 if success else 1


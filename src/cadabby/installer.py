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

CADABBY_MCP_SERVER_ENTRY = {
    "command": "python3",
    "args": ["-m", "cadabby", "mcp"],
}


def make_mcp_server_entry(vault_path: Path | str | None = None) -> dict[str, Any]:
    """Generate the MCP server configuration entry, optionally bound to a vault."""
    if vault_path is None:
        return {
            "command": "python3",
            "args": ["-m", "cadabby", "mcp"],
        }
    resolved = Path(vault_path).resolve()
    return {
        "command": "python3",
        "args": ["-m", "cadabby", "mcp", "--vault", str(resolved)],
    }


def get_repo_plugin_dir() -> Path:
    """Return path to bundled plugins/cadabby directory."""
    pkg_root = Path(__file__).resolve().parent.parent.parent
    for candidate in (
        pkg_root / "plugins" / "cadabby",
        pkg_root / "assets" / "plugins" / "cadabby",
        Path.cwd() / "plugins" / "cadabby",
        Path.cwd() / "assets" / "plugins" / "cadabby",
    ):
        if candidate.exists() and candidate.is_dir():
            return candidate
    raise RuntimeError(f"Cadabby plugin directory not found")


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
            if existing_entry == desired_entry:
                return True, f"Antigravity plugin already installed at {dest} (unchanged)."
            if existing_entry is not None and not force:
                return False, f"Conflict: Antigravity plugin at {dest} is configured for another vault. Use --force to overwrite."

            if dry_run:
                return True, f"[dry-run] Would update {mcp_file} with vault: {resolved_vault}"

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

    if existing_entry is not None and not force:
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
    return True, f"Registered Cadabby MCP server in {dest}{vault_msg}"


def uninstall_claude(
    dest_path: Path | None = None,
    vault_path: Path | str | None = None,
    is_global: bool = False,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Uninstall Cadabby MCP entry from Claude configuration, preserving other keys."""
    dest = (dest_path or default_claude_path(vault_path, is_global)).resolve()
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

    for msg in messages:
        print(msg)

    return 0 if success else 1


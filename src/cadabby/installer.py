"""Harness configuration installer and idempotent non-destructive merge.

Conforms strictly to Cadabby Technical Specification §7.6.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from cadabby.fsutil import atomic_write

CADABBY_MCP_SERVER_ENTRY = {
    "command": "python3",
    "args": ["-m", "cadabby", "mcp"],
}


def get_repo_plugin_dir() -> Path:
    """Return path to bundled plugins/cadabby directory."""
    pkg_root = Path(__file__).resolve().parent.parent.parent
    plugin_dir = pkg_root / "plugins" / "cadabby"
    if plugin_dir.exists():
        return plugin_dir
    cwd_plugin = Path.cwd() / "plugins" / "cadabby"
    if cwd_plugin.exists():
        return cwd_plugin
    raise RuntimeError(f"Cadabby plugin directory not found at {plugin_dir}")


def default_antigravity_path() -> Path:
    """Default path for Antigravity plugin installation."""
    return Path.home() / ".gemini" / "antigravity" / "plugins" / "cadabby"


def default_claude_path() -> Path:
    """Default path for Claude Code configuration."""
    return Path.home() / ".claude.json"


def install_antigravity(
    dest_path: Path | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Install Antigravity plugin with conflict detection and idempotency."""
    dest = (dest_path or default_antigravity_path()).absolute()
    repo_plugin = get_repo_plugin_dir().resolve()

    # Check if already installed identically
    if dest.is_symlink():
        target = dest.resolve()
        if target == repo_plugin:
            return True, f"Antigravity plugin already installed at {dest} (unchanged)."
        elif not force:
            return False, f"Conflict: {dest} links to {target}. Use --force to overwrite."
    elif dest.exists():
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
        # Fallback to copy if symlinks not permitted
        shutil.copytree(repo_plugin, dest)
        action = "Copied"

    return True, f"{action} Antigravity plugin to {dest}"


def uninstall_antigravity(
    dest_path: Path | None = None,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Uninstall Antigravity plugin."""
    dest = (dest_path or default_antigravity_path()).absolute()
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
    force: bool = False,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Non-destructively register Cadabby MCP server in Claude configuration."""
    dest = (dest_path or default_claude_path()).absolute()
    existing_content = ""
    cfg_data: dict[str, Any] = {}

    if dest.exists():
        existing_content = dest.read_text("utf-8")
        if existing_content.strip():
            try:
                cfg_data = json.loads(existing_content)
            except Exception as e:
                return False, f"Failed to parse existing JSON at {dest}: {e}. Aborting."

    mcp_servers = cfg_data.setdefault("mcpServers", {})
    existing_entry = mcp_servers.get("cadabby")

    if existing_entry == CADABBY_MCP_SERVER_ENTRY:
        return True, f"Claude MCP server already registered in {dest} (unchanged)."

    if existing_entry is not None and not force:
        return False, f"Conflict: mcpServers.cadabby already defined in {dest}. Use --force to overwrite."

    mcp_servers["cadabby"] = CADABBY_MCP_SERVER_ENTRY
    new_content = json.dumps(cfg_data, indent=2) + "\n"

    if existing_content == new_content:
        return True, f"Claude configuration in {dest} already up-to-date (unchanged)."

    if dry_run:
        return True, f"[dry-run] Would update {dest} with mcpServers.cadabby"

    dest.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(dest, new_content)
    return True, f"Registered Cadabby MCP server in {dest}"


def uninstall_claude(
    dest_path: Path | None = None,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Uninstall Cadabby MCP entry from Claude configuration, preserving other keys."""
    dest = (dest_path or default_claude_path()).absolute()
    if not dest.exists():
        return True, f"Claude configuration not found at {dest} (nothing to uninstall)."

    existing_content = dest.read_text("utf-8")
    if not existing_content.strip():
        return True, f"Claude configuration at {dest} is empty."

    try:
        cfg_data = json.loads(existing_content)
    except Exception as e:
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
    uninstall: bool = False,
    dry_run: bool = False,
    dest_path: Path | None = None,
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
            ok, msg = uninstall_antigravity(dest_path=dest_path, dry_run=dry_run)
        else:
            ok, msg = install_antigravity(dest_path=dest_path, force=force, dry_run=dry_run)
        success = success and ok
        messages.append(msg)

    if target_claude:
        if uninstall:
            ok, msg = uninstall_claude(dest_path=dest_path, dry_run=dry_run)
        else:
            ok, msg = install_claude(dest_path=dest_path, force=force, dry_run=dry_run)
        success = success and ok
        messages.append(msg)

    for msg in messages:
        print(msg)

    return 0 if success else 1

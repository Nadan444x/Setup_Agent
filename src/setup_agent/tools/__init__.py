"""Tool registry: JSON schemas the LLM sees + the dispatch table that runs them.
"""

from __future__ import annotations

import sys
from ..state import load_profile_text
from .config import account_info, append_powershell_config, append_shell_config, configure_git, setup_github
from .macos import set_macos_default
from .packages import (
    brew_install,
    brew_uninstall,
    brew_upgrade,
    check_installed,
    install_background,
    jobs_status,
    rescan,
    search_brew,
    search_winget,
    winget_install,
    winget_uninstall,
    winget_upgrade,
)
from .system import run_shell
from .windows import set_windows_registry


def read_profile() -> str:
    text = load_profile_text()
    return text if text else "No Setup.md exists yet — run the scan first (setup-agent scan)."


FUNCS = {
    "check_installed": check_installed,
    "search_brew": search_brew,
    "search_winget": search_winget,
    "install_background": install_background,
    "jobs_status": jobs_status,
    "brew_install": brew_install,
    "brew_uninstall": brew_uninstall,
    "brew_upgrade": brew_upgrade,
    "winget_install": winget_install,
    "winget_uninstall": winget_uninstall,
    "winget_upgrade": winget_upgrade,
    "scan_system": rescan,
    "read_profile": read_profile,
    "configure_git": configure_git,
    "account_info": account_info,
    "setup_github": setup_github,
    "append_shell_config": append_shell_config,
    "append_powershell_config": append_powershell_config,
    "set_macos_default": set_macos_default,
    "set_windows_registry": set_windows_registry,
    "run_shell": run_shell,
}


def _tool(name: str, description: str, params: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": params,
                "required": required,
            },
        },
    }


TOOLS = [
    _tool(
        "check_installed",
        "Check whether an app or command is already installed (PATH, Homebrew/Winget, /Applications). "
        "ALWAYS call this before installing anything.",
        {"name": {"type": "string", "description": "App or tool name, e.g. 'zoom' or 'git'"}},
        ["name"],
    ),
    _tool(
        "search_brew",
        "Search Homebrew (macOS) or Winget (Windows) for the exact package name/ID. Never guess tokens.",
        {"query": {"type": "string", "description": "Friendly name to search, e.g. 'whatsapp'"}},
        ["query"],
    ),
    _tool(
        "install_background",
        "THE way to install apps/tools. Gather EVERYTHING the user wants installed and call this ONCE. "
        "All of them install in parallel in the background; it returns immediately (no waiting), "
        "each notifies + updates Setup.md when done.",
        {
            "casks": {"type": "array", "items": {"type": "string"}, "description": "GUI-app cask tokens (macOS)"},
            "formulae": {"type": "array", "items": {"type": "string"}, "description": "CLI/library formula tokens (macOS)"},
            "packages": {"type": "array", "items": {"type": "string"}, "description": "Package names or Winget IDs (Windows)"},
        },
        [],
    ),
    _tool(
        "jobs_status",
        "Report the status of background install jobs started with install_background.",
        {},
        [],
    ),
    _tool(
        "brew_upgrade",
        "Upgrade an already-installed package to its latest version. Use for 'update X' / 'upgrade X'.",
        {
            "name": {"type": "string", "description": "Exact package token to upgrade, e.g. 'codex'"},
            "cask": {"type": "boolean", "description": "cask or formula"},
        },
        ["name"],
    ),
    _tool(
        "brew_uninstall",
        "Uninstall one package. Only when the user explicitly asks to remove something.",
        {
            "name": {"type": "string", "description": "Exact package token to remove"},
            "cask": {"type": "boolean", "description": "true for GUI apps, false for CLI tools"},
        },
        ["name"],
    ),
    _tool(
        "scan_system",
        "Fresh inventory of the machine: installed packages, runtimes, git identity.",
        {},
        [],
    ),
    _tool(
        "read_profile",
        "Read the current Setup.md profile (the desired state of this machine).",
        {},
        [],
    ),
    _tool(
        "configure_git",
        "Set the global git identity (user.name and user.email).",
        {
            "name": {"type": "string", "description": "Git user.name"},
            "email": {"type": "string", "description": "Git user.email"},
        },
        ["name", "email"],
    ),
    _tool(
        "account_info",
        "Read-only: show the git identity (name + email) and the GitHub username.",
        {},
        [],
    ),
    _tool(
        "setup_github",
        "Set up GitHub on this machine: create SSH key, log in to gh CLI, and register key.",
        {"email": {"type": "string", "description": "email for the SSH key comment (optional)"}},
        [],
    ),
    _tool(
        "append_shell_config",
        "Append one line to shell config (~/.zshrc on Mac, $PROFILE on Windows).",
        {"snippet": {"type": "string", "description": "The exact shell line"}},
        ["snippet"],
    ),
    _tool(
        "set_macos_default",
        "Set one macOS user preference via `defaults write` (macOS only).",
        {
            "domain": {"type": "string", "description": "e.g. com.apple.dock"},
            "key": {"type": "string", "description": "e.g. autohide"},
            "value": {"type": "string", "description": "e.g. true or 2"},
            "value_type": {"type": "string", "enum": ["bool", "int", "float", "string"], "description": "type of value"},
        },
        ["domain", "key", "value", "value_type"],
    ),
    _tool(
        "set_windows_registry",
        "Set one user-domain Windows Registry setting (Windows only).",
        {
            "key_path": {"type": "string", "description": "e.g. HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize"},
            "value_name": {"type": "string", "description": "e.g. AppsUseLightTheme"},
            "value": {"type": "string", "description": "e.g. 0 for dark mode"},
            "value_type": {"type": "string", "enum": ["string", "dword", "int", "bool"], "description": "type of value"},
        },
        ["key_path", "value_name", "value", "value_type"],
    ),
    _tool(
        "run_shell",
        "Escape hatch: run a shell command when no other tool fits.",
        {
            "command": {"type": "string", "description": "the exact command"},
            "purpose": {"type": "string", "description": "one line: why this command"},
        },
        ["command"],
    ),
]


def dispatch(name: str, arguments: dict) -> str:
    func = FUNCS.get(name)
    if func is None:
        return f"ERROR: unknown tool '{name}'. Available: {', '.join(sorted(FUNCS))}."
    try:
        return str(func(**(arguments or {})))
    except TypeError as exc:
        return f"ERROR: bad arguments for {name}: {exc}"
    except Exception as exc:
        return f"ERROR: {name} failed: {exc}"

"""Tools for finding and installing software via Homebrew (macOS) or Winget (Windows)."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import sys
from pathlib import Path

from rich.prompt import Confirm

from ..console import console
from ..jobs import JOBS
from ..safety import get_policy, guarded_batch, guarded_run_argv, refusal_or_none, run_readonly, succeeded
from ..state import record_change, record_removal, scan_system

_CRITICAL = {"ollama", "git", "python", "python3", "pipx", "brew", "winget"}

# macOS Homebrew cache
_brew_cache: dict[str, list[str]] = {}

def _brew_list(kind: str) -> list[str]:
    if kind not in _brew_cache:
        code, out, _ = run_readonly(f"brew list --{kind}")
        _brew_cache[kind] = out.split() if code == 0 else []
    return _brew_cache[kind]

def _invalidate_cache() -> None:
    _brew_cache.clear()

# Windows Package Aliases -> Exact Winget Package IDs
_WIN_ALIASES = {
    "vscode": "Microsoft.VisualStudioCode",
    "vs code": "Microsoft.VisualStudioCode",
    "vs-code": "Microsoft.VisualStudioCode",
    "code": "Microsoft.VisualStudioCode",
    "chrome": "Google.Chrome",
    "google chrome": "Google.Chrome",
    "brave": "Brave.Brave",
    "firefox": "Mozilla.Firefox",
    "edge": "Microsoft.Edge",
    "zoom": "Zoom.Zoom",
    "slack": "SlackTechnologies.Slack",
    "whatsapp": "WhatsApp.WhatsApp",
    "telegram": "Telegram.TelegramDesktop",
    "discord": "Discord.Discord",
    "teams": "Microsoft.Teams",
    "git": "Git.Git",
    "node": "OpenJS.NodeJS",
    "nodejs": "OpenJS.NodeJS",
    "python": "Python.Python.3.12",
    "ollama": "Ollama.Ollama",
    "docker": "Docker.DockerDesktop",
    "canva": "Canva.Canva",
    "jq": "jqlang.jq",
    "uv": "astral-sh.uv",
    "go": "GoLang.Go",
    "postman": "Postman.Postman",
    "spotify": "Spotify.Spotify",
    "vlc": "VideoLAN.VLC",
    "7zip": "7zip.7zip",
    "notion": "Notion.Notion",
}


def check_installed(name: str) -> str:
    """Is `name` already on this machine? Checks PATH, Homebrew/Winget, and Applications."""
    name_clean = name.strip()
    hits: list[str] = []

    exe = shutil.which(name_clean)
    if exe:
        hits.append(f"command `{name_clean}` on PATH at {exe}")

    if sys.platform == "win32":
        code, out, _ = run_readonly(f"winget list --query {shlex.quote(name_clean)}", timeout=30)
        if code == 0 and name_clean.lower() in out.lower():
            hits.append(f"Winget package `{name_clean}`")
    else:
        token = name_clean.lower().replace(" ", "-")
        if token in _brew_list("formula"):
            hits.append(f"Homebrew formula `{token}`")
        if token in _brew_list("cask"):
            hits.append(f"Homebrew cask `{token}`")
        flat = name_clean.lower().replace(" ", "").replace("-", "")
        for apps_dir in (Path("/Applications"), Path.home() / "Applications"):
            if apps_dir.is_dir():
                for app in apps_dir.glob("*.app"):
                    if flat in app.stem.lower().replace(" ", "").replace("-", "").replace(".", ""):
                        hits.append(f"app {app.name} in {apps_dir}")
                        break

    if hits:
        return f"INSTALLED: {name_clean} — found as: " + "; ".join(hits) + ". Do not reinstall."
    return f"NOT INSTALLED: {name_clean} was not found on PATH or in Package Manager."


def search_brew(query: str) -> str:
    """Find package for a friendly name (Homebrew on Mac, Winget on Windows)."""
    if sys.platform == "win32":
        return search_winget(query)

    code, out, err = run_readonly(f"brew search {shlex.quote(query)}", timeout=90)
    if code != 0:
        return f"brew search failed: {err.strip() or 'unknown error'}"
    lines = [l.strip() for l in out.splitlines() if l.strip() and not l.startswith("==>")]
    if not lines:
        return f"No Homebrew match for '{query}'."
    q = query.strip().lower()
    exact = next((l for l in lines if l.lower() == q), None)
    if exact:
        others = ", ".join(l for l in lines if l != exact)[:200]
        return f"EXACT MATCH — install with token `{exact}`.\nOther: {others}"
    return "\n".join(lines[:30])


def search_winget(query: str) -> str:
    """Find exact Winget package ID on Windows."""
    code, out, err = run_readonly(f"winget search --query {shlex.quote(query)} --accept-source-agreements", timeout=60)
    if code != 0:
        return f"winget search failed: {err.strip() or 'unknown error'}"
    lines = [l.strip() for l in out.splitlines() if l.strip() and not l.startswith("-")]
    if not lines:
        return f"No Winget match for '{query}'."
    return "\n".join(lines[:25])


def brew_install(name: str, cask: bool = False) -> str:
    """Install a package on Mac (brew) or Windows (winget)."""
    if sys.platform == "win32":
        return winget_install(name)

    token = name.strip()
    argv = ["brew", "install"] + (["--cask"] if cask else []) + [token]
    result = guarded_run_argv(argv, purpose=f"install {token}")
    if succeeded(result):
        _invalidate_cache()
        section = "Other GUI apps (Homebrew casks)" if cask else "Dev tools (Homebrew formulae)"
        pretty = token.replace("-", " ").title() if cask else token
        item = f"{pretty} (`{token}`)" if cask else f"`{token}`"
        note = record_change(section, item, f"installed {'cask' if cask else 'formula'} `{token}`")
        result += f"\n{note}"
    return result


def winget_install(name: str) -> str:
    """Install a package using Winget on Windows."""
    token = _WIN_ALIASES.get(name.strip().lower(), name.strip())
    argv = [
        "winget", "install", "--id", token, "--exact",
        "--accept-package-agreements", "--accept-source-agreements", "--source", "winget"
    ]
    result = guarded_run_argv(argv, purpose=f"install {token}")
    if succeeded(result):
        pretty = token.split(".")[-1] if "." in token else token
        item = f"{pretty} (`{token}`)"
        note = record_change("Applications & Packages (Winget)", item, f"installed package `{token}`")
        result += f"\n{note}"
    return result


def brew_upgrade(name: str, cask: bool = False) -> str:
    """Upgrade an installed package."""
    if sys.platform == "win32":
        return winget_upgrade(name)

    token = name.strip()
    argv = ["brew", "upgrade"] + (["--cask"] if cask else []) + [token]
    result = guarded_run_argv(argv, purpose=f"upgrade {token}")
    if succeeded(result):
        _invalidate_cache()
        section = "Other GUI apps (Homebrew casks)" if cask else "Dev tools (Homebrew formulae)"
        pretty = token.replace("-", " ").title() if cask else token
        item = f"{pretty} (`{token}`)" if cask else f"`{token}`"
        note = record_change(section, item, f"upgraded {'cask' if cask else 'formula'} `{token}`")
        result += f"\n{note}"
        from ..notify import notify
        notify("SetUp Agent", f"{token} upgraded ✓")
    return result


def winget_upgrade(name: str) -> str:
    token = _WIN_ALIASES.get(name.strip().lower(), name.strip())
    argv = [
        "winget", "upgrade", "--id", token, "--exact",
        "--accept-package-agreements", "--accept-source-agreements"
    ]
    result = guarded_run_argv(argv, purpose=f"upgrade {token}")
    if succeeded(result):
        pretty = token.split(".")[-1] if "." in token else token
        item = f"{pretty} (`{token}`)"
        note = record_change("Applications & Packages (Winget)", item, f"upgraded package `{token}`")
        result += f"\n{note}"
        from ..notify import notify
        notify("SetUp Agent", f"{token} upgraded ✓")
    return result


def brew_uninstall(name: str, cask: bool = False) -> str:
    """Uninstall a package."""
    if sys.platform == "win32":
        return winget_uninstall(name)

    token = name.strip()
    low = token.lower()
    if low in _CRITICAL:
        return f"REFUSED: `{token}` is required by SetUp Agent or the system."

    base = ["brew", "uninstall"] + (["--cask"] if cask else [])
    result = guarded_run_argv(base + [token], purpose=f"uninstall {token}")
    if succeeded(result):
        _invalidate_cache()
        note = record_removal(token, f"uninstalled `{token}`")
        result += f"\n{note}"
        from ..notify import notify
        notify("SetUp Agent", f"{token} uninstalled ✓")
    return result


def winget_uninstall(name: str) -> str:
    token = _WIN_ALIASES.get(name.strip().lower(), name.strip())
    low = token.lower()
    if any(crit in low for crit in _CRITICAL):
        return f"REFUSED: `{token}` is required by SetUp Agent or system."

    argv = ["winget", "uninstall", "--id", token, "--exact"]
    result = guarded_run_argv(argv, purpose=f"uninstall {token}")
    if succeeded(result):
        note = record_removal(token, f"uninstalled package `{token}`")
        result += f"\n{note}"
        from ..notify import notify
        notify("SetUp Agent", f"{token} uninstalled ✓")
    return result


def install_background(casks: list[str] | None = None,
                       formulae: list[str] | None = None,
                       packages: list[str] | None = None) -> str:
    """Install packages in the BACKGROUND without blocking."""
    if sys.platform == "win32":
        all_names = (casks or []) + (formulae or []) + (packages or [])
        names = [n.strip() for n in all_names if n.strip()]
        if not names:
            return "Nothing to install."
        running = JOBS.active_tokens()
        to_start = []
        seen = set()
        for name in names:
            token = _WIN_ALIASES.get(name.lower(), name)
            key = f"winget:{token.lower()}"
            if key in running or key in seen:
                continue
            seen.add(key)
            argv = [
                "winget", "install", "--id", token, "--exact",
                "--accept-package-agreements", "--accept-source-agreements", "--source", "winget"
            ]
            to_start.append((token, argv, "winget", token))

        if not to_start:
            return "Nothing to install."

        policy = get_policy()
        if policy.dry_run:
            return "DRY-RUN: would start in background: " + ", ".join(t[0] for t in to_start)

        for label, argv, kind, token in to_start:
            JOBS.spawn_detached(label, argv, kind, token)

        return f"Started {len(to_start)} background install(s) via Winget."

    # macOS Homebrew
    c_list = [c.strip() for c in (casks or []) if c.strip()]
    f_list = [f.strip() for f in (formulae or []) if f.strip()]
    p_list = [p.strip() for p in (packages or []) if p.strip()]
    if not c_list and not f_list and not p_list:
        return "Nothing to install."

    to_start = []
    for c in c_list:
        to_start.append((c, ["brew", "install", "--cask", c], "cask", c))
    for f in f_list + p_list:
        to_start.append((f, ["brew", "install", f], "formula", f))

    policy = get_policy()
    if policy.dry_run:
        return "DRY-RUN: would start in background: " + ", ".join(t[0] for t in to_start)

    for label, argv, kind, token in to_start:
        JOBS.spawn_detached(label, argv, kind, token)

    return f"Started {len(to_start)} background install(s) via Homebrew."


def jobs_status() -> str:
    jobs = JOBS.all_spawned()
    if not jobs:
        return "No background jobs this session."
    parts = [f"{j.token or j.label}: {j.status}" for j in jobs]
    return "Background jobs — " + "; ".join(parts)


def rescan() -> str:
    state = scan_system()
    return f"System scan complete for {sys.platform}."

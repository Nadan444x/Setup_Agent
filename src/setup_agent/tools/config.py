"""Tools for git identity, shell configuration (.zshrc on Mac, $PROFILE on Windows), and GitHub (gh + SSH) setup."""

from __future__ import annotations

import os
import re
import shutil
import socket
import sys
from datetime import datetime
from pathlib import Path

from rich.prompt import Confirm

from ..console import command_preview, console
from ..safety import (
    get_policy,
    guarded_run_argv,
    guarded_run_interactive,
    refusal_or_none,
    run_readonly,
    succeeded,
)
from ..state import record_change

MARKER = "# added by setup-agent"


def configure_git(name: str, email: str) -> str:
    """Set the global git identity."""
    results = []
    for field, value in (("user.name", name), ("user.email", email)):
        result = guarded_run_argv(
            ["git", "config", "--global", field, value], purpose=f"set git {field}"
        )
        results.append(f"{field}: {result}")
        if succeeded(result):
            record_change("Git identity", f"user.{field.split('.')[1]} = {value}", f"git {field} set to {value}")
    return "\n".join(results)


def _gh_authed() -> bool:
    return run_readonly("gh auth status")[0] == 0


def _github_username() -> str | None:
    if not shutil.which("gh"):
        return None
    code, out, _ = run_readonly("gh api user --jq .login", timeout=15)
    if code == 0 and out.strip():
        return out.strip()
    _, o2, e2 = run_readonly("gh auth status", timeout=15)
    m = re.search(r"account\s+(\S+)", o2 + e2)
    return m.group(1) if m else None


def account_info() -> str:
    """Read-only: report git identity (name + email) and the GitHub username."""
    name = run_readonly("git config --global user.name")[1].strip()
    email = run_readonly("git config --global user.email")[1].strip()
    lines = [
        f"git user.name: {name or '(not set)'}",
        f"git user.email: {email or '(not set)'}",
    ]
    gh_user = _github_username()
    lines.append(f"GitHub username: {gh_user}" if gh_user else "GitHub: not logged in")
    return "\n".join(lines)


def _github_ssh_works() -> bool:
    _, out, err = run_readonly(
        "ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -T git@github.com", timeout=20)
    return "successfully authenticated" in (out + err).lower()


def _any_pubkey() -> Path | None:
    ssh = Path.home() / ".ssh"
    if not ssh.is_dir():
        return None
    preferred = ssh / "id_ed25519.pub"
    if preferred.exists():
        return preferred
    keys = sorted(ssh.glob("id_*.pub"))
    return keys[0] if keys else None


def setup_github(email: str = "") -> str:
    """Set up GitHub on this machine: create SSH key, log in to gh CLI, and register key."""
    if not shutil.which("gh"):
        return ("NOT READY: the GitHub CLI `gh` isn't installed. Install it first, "
                "then call setup_github again.")

    steps: list[str] = []
    changed = False

    ssh_ready = _github_ssh_works()
    pub = _any_pubkey()
    if ssh_ready:
        steps.append("SSH: already authenticates to GitHub ✓ — leaving it alone")
    elif pub is None:
        (Path.home() / ".ssh").mkdir(mode=0o700, exist_ok=True)
        key = Path.home() / ".ssh" / "id_ed25519"
        r = guarded_run_argv(
            ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-C", email or socket.gethostname()],
            purpose="generate a new SSH key (no passphrase)")
        if succeeded(r):
            steps.append("SSH key: created ~/.ssh/id_ed25519"); pub = key.with_suffix(".pub"); changed = True
        else:
            steps.append(f"SSH key: could not generate ({r.splitlines()[0]})")
    else:
        steps.append(f"SSH key: found {pub.name} (will try to register)")

    if _gh_authed():
        u = _github_username()
        steps.append(f"gh auth: already logged in{f' as {u}' if u else ''}")
    else:
        r = guarded_run_interactive(
            ["gh", "auth", "login", "--git-protocol", "ssh", "--web"],
            purpose="log in to GitHub (opens a browser / shows a device code)")
        if succeeded(r):
            steps.append("gh auth: logged in"); changed = True
        else:
            steps.append(f"gh auth: not completed ({r})")

    if not ssh_ready and pub and _gh_authed():
        r = guarded_run_argv(
            ["gh", "ssh-key", "add", str(pub), "--title", socket.gethostname()],
            purpose="register this SSH key on your GitHub account")
        if succeeded(r):
            steps.append("SSH key on GitHub: registered"); changed = True
        elif "already" in r.lower():
            steps.append("SSH key on GitHub: already registered")
        else:
            steps.append(f"SSH key on GitHub: not added ({r.splitlines()[0]})")

    if changed:
        record_change("Developer accounts", "GitHub (`gh` + SSH)", "set up GitHub CLI auth + SSH key")

    return "GitHub setup:\n- " + "\n- ".join(steps)


def append_powershell_config(snippet: str) -> str:
    """Append a line to PowerShell $PROFILE (Windows)."""
    docs = Path.home() / "Documents"
    ps7_prof = docs / "PowerShell" / "Microsoft.PowerShell_profile.ps1"
    ps5_prof = docs / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1"
    profile_file = ps7_prof if ps7_prof.exists() else (ps5_prof if ps5_prof.exists() else ps7_prof)
    return _append_to_file(profile_file, snippet, "PowerShell $PROFILE")


def append_shell_config(snippet: str) -> str:
    """Append a line to shell config (.zshrc on Mac, $PROFILE on Windows)."""
    if sys.platform == "win32":
        return append_powershell_config(snippet)
    
    zshrc = Path.home() / ".zshrc"
    return _append_to_file(zshrc, snippet, "~/.zshrc")


def _append_to_file(file_path: Path, snippet: str, label: str) -> str:
    snippet = snippet.strip()
    policy = get_policy()

    refusal = refusal_or_none(snippet)
    if refusal:
        return refusal

    if file_path.exists() and snippet in file_path.read_text(encoding="utf-8"):
        return f"SKIPPED: {label} already contains: {snippet}"

    if policy.dry_run:
        return f"DRY-RUN: would append to {label} ({file_path}): `{snippet}`. Assume success."

    console.print(f"[bold]about to append to {label} ({file_path})[/bold]:")
    command_preview(snippet)
    if not Confirm.ask("append this line?", default=False):
        return "DECLINED: the user said no to this shell config change."

    file_path.parent.mkdir(parents=True, exist_ok=True)
    if file_path.exists():
        backup = file_path.with_suffix(f".setup-agent-{datetime.now():%Y%m%d}.bak")
        if not backup.exists():
            backup.write_text(file_path.read_text(encoding="utf-8"), encoding="utf-8")

    with file_path.open("a", encoding="utf-8") as fh:
        fh.write(f"\n{snippet}  {MARKER}\n")

    note = record_change("Shell", f"`{snippet}`", f"appended to {label}: {snippet}")
    return f"exit=0\nappended to {label} (backup kept). {note}"

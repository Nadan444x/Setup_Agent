r"""The safety layer: every command the LLM wants to run passes through here.

Order of checks for a mutating command:
  1. catastrophic guard      -> hard refusal (rm -rf /, rmdir C:\, Format-Volume...); never runs
  2. dry-run                 -> print what WOULD run, return simulated ok, execute nothing
  3. elevated guard          -> sudo / runas / admin / internet scripts: ⚠️ warn + mandatory y/N
  4. confirmation            -> routine commands: show + wait for y/N (skippable with --yes)
  5. execution               -> subprocess with a timeout, output captured for the LLM
"""

from __future__ import annotations

import concurrent.futures
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass

from rich.live import Live
from rich.prompt import Confirm
from rich.table import Table

from .console import command_preview, console, warn

# ---------------------------------------------------------------- policy ----

@dataclass
class Policy:
    dry_run: bool = False
    auto_yes: bool = False
    bypass: bool = False


_policy = Policy()


def set_policy(dry_run: bool = False, auto_yes: bool = False, bypass: bool = False) -> None:
    _policy.dry_run = dry_run
    _policy.auto_yes = auto_yes or bypass
    _policy.bypass = bypass


def get_policy() -> Policy:
    return _policy


# ------------------------------------------------------------- blocklist ----

_RM_MAC = r"\brm\b(?:\s+-{1,2}[\w-]+)*\s+-{0,2}\w*[rRfF]"
_PROTECTED_MAC = r"(/(\s|$)|/\*|~(\s|$)|\$HOME(\s|$)|(?:~|\$HOME)/|/(System|Applications|Library|Users|usr|bin|sbin|etc|var|opt|private)(/|\s|$))"

_PROTECTED_WIN = r"(C:\\|C:\/|C:\\Windows|C:\\Program Files|\$HOME|%USERPROFILE%)"

_CATASTROPHIC_UNIX: list[tuple[re.Pattern, str]] = [
    (re.compile(rf"{_RM_MAC}[\w-]*\s+.*{_PROTECTED_MAC}"), "recursively deletes / , home, or system path"),
    (re.compile(r"\bfind\b.*\s-delete\b"), "mass-deletes files via find -delete"),
    (re.compile(r"\bmkfs\b|\bnewfs\b"), "formats a filesystem"),
    (re.compile(r"\bdd\b.*\bof=/dev/"), "raw-writes to a device"),
    (re.compile(r"\bdiskutil\s+(erase|partition|reformat)"), "erases or repartitions a disk"),
    (re.compile(r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;"), "fork bomb"),
    (re.compile(r"\b(shutdown|reboot|halt)\b"), "shuts down or reboots"),
]

_CATASTROPHIC_WIN: list[tuple[re.Pattern, str]] = [
    (re.compile(rf"\b(Remove-Item|rmdir|rd|del)\b.*(-Recurse|-r|-s|-Force|-f).*{_PROTECTED_WIN}", re.I), "recursively deletes a protected system or root path"),
    (re.compile(r"\bFormat-(Volume|Disk)\b|\bClear-Disk\b", re.I), "formats or clears a disk volume"),
    (re.compile(r"\bdiskpart\b", re.I), "executes diskpart partition utility"),
    (re.compile(r"\breg\s+delete\s+hklm\\(system|software)\b", re.I), "deletes critical HKLM registry keys"),
    (re.compile(r"\b(Stop-Computer|Restart-Computer|shutdown\s+/[sf])\b", re.I), "shuts down or reboots the computer"),
]

_ELEVATED_UNIX: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(^|[\s;&|(])sudo\b"), "it needs administrator rights (sudo)"),
    (re.compile(r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z|da|k)?sh\b"), "it runs a script downloaded from internet"),
]

_ELEVATED_WIN: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(runas|Start-Process.*-Verb\s+RunAs)\b", re.I), "it requires administrator privileges"),
    (re.compile(r"\bSet-ExecutionPolicy\b", re.I), "it changes PowerShell execution policy"),
    (re.compile(r"\b(iwr|Invoke-WebRequest|curl|wget)\b.*\|\s*(iex|Invoke-Expression)\b", re.I), "it executes a script directly from internet"),
]

ALLOWLIST_PREFIXES: tuple[str, ...] = (
    "brew ",
    "winget ",
    "git ",
    "defaults write ",
    "defaults read",
    "Set-ItemProperty ",
    "reg add ",
    "reg query ",
    "mkdir ",
    "ln -s",
    "open ",
    "ollama ",
    "npm install -g",
    "pipx install",
    "uv tool install",
    "powershell ",
)

_SHELL_METACHARS = re.compile(r"[;&|`\n]|\$\(|>|<")


def refusal_or_none(command: str) -> str | None:
    stripped = command.strip()
    rules = _CATASTROPHIC_WIN if sys.platform == "win32" else _CATASTROPHIC_UNIX
    for pattern, why in rules:
        if pattern.search(stripped):
            return f"REFUSED: this command is blocked because it {why}. Choose a safer approach."
    return None


def elevated_reason(command: str) -> str | None:
    stripped = command.strip()
    rules = _ELEVATED_WIN if sys.platform == "win32" else _ELEVATED_UNIX
    for pattern, why in rules:
        if pattern.search(stripped):
            return why
    return None


def is_allowlisted(command: str) -> bool:
    stripped = command.strip()
    if _SHELL_METACHARS.search(stripped):
        return False
    return stripped.lower().startswith(ALLOWLIST_PREFIXES)


def _env_with_brew() -> dict:
    env = os.environ.copy()
    if sys.platform == "darwin":
        path = env.get("PATH", "")
        for extra in ("/opt/homebrew/bin", "/usr/local/bin"):
            if extra not in path.split(":"):
                path = f"{path}:{extra}" if path else extra
        env["PATH"] = path
        env.setdefault("HOMEBREW_NO_AUTO_UPDATE", "1")
    return env


def run_readonly(command: str, timeout: int = 120) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, env=_env_with_brew(),
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except OSError as exc:
        return 1, "", str(exc)


def _screen(display: str, purpose: str) -> str | None:
    refusal = refusal_or_none(display)
    if refusal:
        warn(f"blocked: {display}")
        return refusal

    if _policy.dry_run:
        console.print("[bold yellow][dry-run][/bold yellow] would run:")
        command_preview(display)
        return f"DRY-RUN: command was NOT executed: `{display}`. Assume success."

    elevated = elevated_reason(display)
    if elevated:
        if _policy.bypass:
            console.print(f"[bold red]⚠ elevated[/bold red] [dim]({purpose}, bypass)[/dim] — {elevated}:")
            command_preview(display)
            return None
        console.print(f"[bold red]⚠ elevated command[/bold red] [dim]({purpose})[/dim] — {elevated}:")
        command_preview(display)
        if not Confirm.ask("[bold red]this needs elevated permission — run it?[/bold red]", default=False):
            return "DECLINED: the user declined this elevated command."
        return None

    console.print(f"[bold]about to run[/bold] [dim]({purpose})[/dim]:")
    command_preview(display)
    if not (_policy.bypass or (_policy.auto_yes and is_allowlisted(display))):
        if not Confirm.ask("run this command?", default=False):
            return "DECLINED: the user said no to this command."
    return None


def _format_result(returncode: int, stdout: str, stderr: str) -> str:
    out = (stdout or "").strip()
    err = (stderr or "").strip()
    result = f"exit={returncode}"
    if out:
        result += f"\nstdout:\n{out[:3000]}"
    if err:
        result += f"\nstderr:\n{err[:1500]}"
    return result


def _busy_label(display: str) -> str:
    short = display if len(display) <= 70 else display[:67] + "…"
    return f"[cyan]running[/cyan] {short} [dim](working…)[/dim]"


def guarded_run(command: str, purpose: str, timeout: int = 1800) -> str:
    short_circuit = _screen(command, purpose)
    if short_circuit is not None:
        return short_circuit
    try:
        with console.status(_busy_label(command), spinner="dots"):
            proc = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                timeout=timeout, env=_env_with_brew(),
            )
    except subprocess.TimeoutExpired:
        return f"ERROR: `{command}` timed out after {timeout}s and was killed."
    except OSError as exc:
        return f"ERROR: could not start `{command}`: {exc}"
    return _format_result(proc.returncode, proc.stdout, proc.stderr)


def guarded_run_argv(argv: list[str], purpose: str, timeout: int = 1800) -> str:
    display = " ".join(shlex.quote(a) for a in argv)
    short_circuit = _screen(display, purpose)
    if short_circuit is not None:
        return short_circuit
    try:
        with console.status(_busy_label(display), spinner="dots"):
            proc = subprocess.run(
                argv, shell=False, capture_output=True, text=True,
                timeout=timeout, env=_env_with_brew(),
            )
    except subprocess.TimeoutExpired:
        return f"ERROR: `{display}` timed out after {timeout}s and was killed."
    except OSError as exc:
        return f"ERROR: could not start `{display}`: {exc}"
    return _format_result(proc.returncode, proc.stdout, proc.stderr)


def guarded_run_interactive(argv: list[str], purpose: str, timeout: int = 1800) -> str:
    display = " ".join(shlex.quote(a) for a in argv)
    short_circuit = _screen(display, purpose)
    if short_circuit is not None:
        return short_circuit
    console.print("[dim](interactive — follow any prompt in your terminal or browser)[/dim]")
    try:
        proc = subprocess.run(argv, timeout=timeout, env=_env_with_brew())
    except subprocess.TimeoutExpired:
        return f"ERROR: `{display}` timed out after {timeout}s."
    except OSError as exc:
        return f"ERROR: could not start `{display}`: {exc}"
    return f"exit={proc.returncode}"


def succeeded(result: str) -> bool:
    return result.startswith("exit=0")


def guarded_batch(jobs: list[dict], purpose: str, concurrency: int = 3, timeout: int = 3600) -> dict[str, str]:
    results: dict[str, str] = {}
    runnable: list[dict] = []

    for job in jobs:
        display = " ".join(shlex.quote(a) for a in job["argv"])
        refusal = refusal_or_none(display)
        if refusal:
            warn(f"blocked: {display}")
            results[job["label"]] = refusal
        else:
            runnable.append(job)
    if not runnable:
        return results

    if _policy.dry_run:
        for job in runnable:
            results[job["label"]] = f"DRY-RUN: not executed. Assume `{job['label']}` installed."
        return results

    status: dict[str, str] = {job["label"]: "queued" for job in runnable}
    lock = __import__("threading").Lock()

    def work(job: dict) -> None:
        label = job["label"]
        with lock:
            status[label] = "installing"
        try:
            proc = subprocess.run(job["argv"], shell=False, capture_output=True, text=True, timeout=timeout, env=_env_with_brew())
            code, out, err = proc.returncode, proc.stdout, proc.stderr
        except Exception as exc:
            code, out, err = 1, "", str(exc)
        with lock:
            status[label] = "done" if code == 0 else "failed"
        results[label] = _format_result(code, out, err)

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(work, job) for job in runnable]
        for f in futures:
            f.result()

    return results

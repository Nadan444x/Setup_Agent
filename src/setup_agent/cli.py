"""setup-agent CLI — Typer commands wiring everything together.

  scan    inspect this machine -> write/refresh Setup.md
  doctor  preflight: brew/winget? ollama? model? Setup.md?
  setup   read Setup.md -> install gaps + apply config/prefs
  run     one-shot natural-language goal
  chat    interactive REPL
  profile print the current Setup.md
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import Annotated, Optional

import typer

from . import DEFAULT_MODEL, __version__
from .console import console, error, info, rule, status_table, success

app = typer.Typer(
    name="setup-agent",
    help="Terminal agent that provisions a fresh Mac or Windows PC, driven by a local LLM (Ollama).",
    no_args_is_help=True,
    add_completion=False,
)

ModelOpt = Annotated[str, typer.Option("--model", "-m", envvar="SETUP_AGENT_MODEL", help="Ollama model to use")]
ProfileOpt = Annotated[Optional[str], typer.Option("--profile", "-p", help="Path to Setup.md")]
DryRunOpt = Annotated[bool, typer.Option("--dry-run", help="Preview: execute nothing, change nothing")]
ConfirmOpt = Annotated[bool, typer.Option(
    "--confirm", help="Ask y/N before every change (default: routine changes run without asking)")]
BypassOpt = Annotated[bool, typer.Option(
    "--bypass", envvar="SETUP_AGENT_BYPASS",
    help="Run EVERYTHING without asking, incl. elevated/installers.")]


def _configure(profile: Optional[str], dry_run: bool = False, confirm: bool = False,
               bypass: bool = False) -> None:
    from .safety import set_policy
    from .state import set_profile_path

    set_profile_path(profile)
    set_policy(dry_run=dry_run, auto_yes=not confirm, bypass=bypass)
    if dry_run:
        info("[dry-run] nothing will be executed or written")
    if bypass:
        from .console import warn
        warn("BYPASS MODE — commands run WITHOUT confirmation.")


@app.command()
def scan(profile: ProfileOpt = None) -> None:
    """Inspect this machine and generate/refresh the living Setup.md (read-only scan)."""
    _configure(profile)
    from .state import profile_path, write_initial_profile

    target = profile_path()
    if target.exists():
        info(f"refreshing existing profile at {target}")
    with console.status("scanning the machine…"):
        written = write_initial_profile(target)
    success(f"profile written: {written}")
    info("open it, adjust anything you like, then try:  setup-agent setup --dry-run")


@app.command()
def doctor(model: ModelOpt = DEFAULT_MODEL, profile: ProfileOpt = None) -> None:
    """Preflight check: is everything the agent needs present?"""
    _configure(profile)
    from . import llm
    from .state import profile_path

    pkg_mgr = "Winget" if sys.platform == "win32" else "Homebrew"
    pkg_bin = shutil.which("winget") if sys.platform == "win32" else shutil.which("brew")
    ollama_bin = shutil.which("ollama")
    server = llm.server_up() if ollama_bin else False
    model_ok = llm.has_model(model) if server else False
    prof = profile_path()

    rows = [
        (pkg_mgr, bool(pkg_bin), pkg_bin or f"{pkg_mgr} package manager"),
        ("Ollama binary", bool(ollama_bin), ollama_bin or "install ollama"),
        ("Ollama server", server, "ok" if server else "ollama serve"),
        (f"model {model}", model_ok, "pulled" if model_ok else f"ollama pull {model}"),
        ("Setup.md profile", prof.exists(), str(prof) if prof.exists() else "setup-agent scan"),
    ]
    console.print(status_table(rows))
    if all(ok for _, ok, _ in rows):
        success("everything ready — try:  setup-agent run \"install jq\" --dry-run")
    else:
        bootstrap_cmd = "bootstrap.ps1" if sys.platform == "win32" else "bootstrap.sh"
        error(f"fix the ✗ rows above ({bootstrap_cmd} handles all of them on a fresh machine)")


@app.command()
def setup(
    model: ModelOpt = DEFAULT_MODEL,
    profile: ProfileOpt = None,
    dry_run: DryRunOpt = False,
    confirm: ConfirmOpt = False,
    bypass: BypassOpt = False,
) -> None:
    """Provision this machine to match Setup.md (install gaps, apply config + prefs)."""
    _configure(profile, dry_run, confirm, bypass)
    from .agent import run_goal
    from .state import profile_path

    if not profile_path().exists():
        error(f"no profile at {profile_path()} — run `setup-agent scan` first")
        raise typer.Exit(1)

    run_goal(
        "Provision this machine to match the Setup.md profile. FIRST call read_profile to "
        "see the full list. Then work section by section: (1) install every listed app or "
        "tool that is missing or marked ⬜ — check each first; (2) apply the git identity if "
        "not set; (3) apply the shell lines; (4) apply the preferences. Skip everything "
        "already ✅/present. Finish with a summary of installed / already present / skipped / failed.",
        model=model,
    )


@app.command()
def run(
    goal: Annotated[str, typer.Argument(help='e.g. "install zoom and slack"')],
    model: ModelOpt = DEFAULT_MODEL,
    profile: ProfileOpt = None,
    dry_run: DryRunOpt = False,
    confirm: ConfirmOpt = False,
    bypass: BypassOpt = False,
) -> None:
    """One-shot natural-language goal, e.g. `setup-agent run "install zoom"`."""
    _configure(profile, dry_run, confirm, bypass)
    from .agent import run_goal

    run_goal(goal, model=model)


@app.command()
def chat(
    model: ModelOpt = DEFAULT_MODEL,
    profile: ProfileOpt = None,
    dry_run: DryRunOpt = False,
    confirm: ConfirmOpt = False,
    bypass: BypassOpt = False,
) -> None:
    """Interactive session — talk to the agent, it remembers the conversation."""
    _configure(profile, dry_run, confirm, bypass)
    from .agent import chat_repl

    chat_repl(model=model)


@app.command()
def profile(profile: ProfileOpt = None) -> None:
    """Print the current Setup.md."""
    _configure(profile)
    from .state import load_profile_text, profile_path

    text = load_profile_text()
    if text is None:
        error(f"no profile at {profile_path()} — run `setup-agent scan` first")
        raise typer.Exit(1)
    console.print(text)


@app.command()
def jobs(wait: Annotated[bool, typer.Option("--wait", help="Block until running jobs finish")] = False) -> None:
    """List background install jobs and their status."""
    import time

    from .jobs import load_persisted_jobs

    def render() -> None:
        rows = load_persisted_jobs()
        if not rows:
            info("no background jobs recorded")
            return
        table = status_table(
            [
                (
                    r.get("token") or r.get("label", "?"),
                    r.get("status") == "done",
                    f"{r.get('status')} · started {r.get('started_at', '?')}"
                    + (f" · log {r.get('log_path')}" if r.get("status") == "failed" else ""),
                )
                for r in rows[-20:]
            ],
            title="background jobs",
        )
        console.print(table)

    render()
    if wait:
        from .jobs import _alive

        while any(r.get("status") == "running" and _alive(r.get("pid")) for r in load_persisted_jobs()):
            time.sleep(2)
        info("all jobs finished (or no longer running)")
        render()


@app.command(hidden=True)
def runjob(job_id: str) -> None:
    """Internal: run one detached background install job (spawned by install_background)."""
    from .jobs import run_job

    raise typer.Exit(run_job(job_id))


@app.command()
def version() -> None:
    """Show the setup-agent version."""
    console.print(f"setup-agent {__version__} · default model {DEFAULT_MODEL} · "
                  f"host {os.environ.get('OLLAMA_HOST', 'http://localhost:11434')}")


if __name__ == "__main__":
    app()

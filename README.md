# Setup Agent 🖥️🤖

**A terminal agent that sets up a fresh Mac or Windows PC for you — powered by a local LLM (Ollama). No cloud, no API keys.**

You type plain English in your terminal (zsh, bash, or PowerShell):

```bash
setup-agent run "install zoom, slack and whatsapp"
setup-agent setup        # provision the whole machine from Setup.md
```

…and a model running **on your own machine** figures out what's missing, installs it with
Homebrew (macOS) or Winget (Windows), configures git + your shell, applies your system preferences — and keeps a living record of everything so your *next* computer sets itself up.

---

## The two layers

| | What | Why |
|---|---|---|
| **Layer 0** | `bootstrap.sh` (macOS) / `bootstrap.ps1` (Windows) | A bare machine needs prerequisites before the smart agent can run. One command installs prerequisites, then hands off. |
| **Layer 1** | `setup-agent` — the smart agent | LLM-driven provisioning: understands English, checks before installing, records everything. |

### Fresh Mac? One command in Terminal:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Nadan444x/Setup_Agent/main/bootstrap.sh)"
```

### Fresh Windows PC? One command in PowerShell:

```powershell
iwr -useb https://raw.githubusercontent.com/Nadan444x/Setup_Agent/main/bootstrap.ps1 | iex
```

It installs (skipping anything already present): Homebrew / Winget → Python 3 + pipx → Ollama +
its server → the default model (`qwen2.5:7b`) → `setup-agent` itself. Safe to re-run any time.

---

## How the brain works: the agent loop

A script is a fixed list of commands. An **agent** decides the next step from what it just
learned. The safety trick: **the LLM never runs anything itself** — it can only *request* a
tool; the Python side decides whether that request actually executes.

```
    ┌──────────────────────────────────────────────────────────┐
    │  Setup.md (living system file)  +  your typed request      │
    └───────────────────────────┬──────────────────────────────┘
                                 ▼
┌────────────────┐  "call install_background(zoom)"  ┌──────────────────┐
│  LOCAL LLM      │ ─────────────────────────────────▶│  SAFETY LAYER     │
│  (Ollama)       │                                   │  dry-run? blocked? │
│  proposes a     │ ◀───────────────────────────────── │  sudo/admin? ask  │
│  TOOL CALL      │  result: ok / present / no        └────────┬─────────┘
└────────────────┘                                             │ approved
         ▲                                                     ▼
         │                                            ┌──────────────────┐
         │            result fed back                 │  EXECUTOR         │
         │  ◀──────────────────────────────────────   │  runs `brew…` or  │
         │                                            │  `winget…`        │
         │                                            └────────┬─────────┘
         │                                                     │ on success
         └───────────── loop until done ─────────────── UPDATE Setup.md (+ changelog)
```

Every turn: the model gets the conversation + tool schemas → replies with a tool call →
the safety layer screens it → the executor runs it → the output is appended as a
`role:"tool"` message → the model sees it and picks the next step. When it stops calling
tools and answers in plain text, the goal is done.

## The living `Setup.md`

One file, two jobs: the **inventory** of this machine *and* the **recipe** to rebuild it.

- `setup-agent scan` generates it by inspecting the real machine (read-only): installed packages,
  runtimes, npm globals, system preferences, git identity, shell.
- **Every successful install or setting change writes itself back into the file** with a
  timestamped changelog line. You never update it by hand; it never goes stale.
- New laptop/PC? Copy `Setup.md` over, run `setup-agent setup`, and the machine rebuilds to match it.

## Commands

```bash
setup-agent scan                 # inspect this machine → write/refresh Setup.md
setup-agent doctor               # preflight: brew/winget / ollama / model / profile
setup-agent setup                # provision the machine from Setup.md
setup-agent setup --dry-run      # preview everything, change nothing
setup-agent run "install zoom"   # one-shot goal in plain English
setup-agent chat                 # interactive conversation
setup-agent profile              # print the current Setup.md
```

Shared flags: `--model/-m` (or `SETUP_AGENT_MODEL`), `--profile/-p` (or `SETUP_AGENT_PROFILE`),
`--dry-run`, `--yes/-y`, `--bypass`.

## Safety model

Commands fall into three tiers:

| Tier | Examples | Behavior |
|---|---|---|
| **Catastrophic** | `rm -rf /`, `rmdir /s /q C:\`, `Format-Volume`, `diskpart` | **Hard-refused, always** |
| **Elevated** | `sudo …`, `runas`, internet script execution | Shown with a **⚠️ warning and a mandatory y/N** |
| **Routine** | `brew install`, `winget install`, `git config` | Normal confirm; `--yes` / `--bypass` may skip |

## Dev setup

```bash
python3 -m venv .venv
source .venv/bin/activate  # or .\.venv\Scripts\activate on Windows
pip install -e .
setup-agent doctor
```

#!/bin/bash
# =============================================================================
#  SetUp Agent — Layer 0 bootstrap (macOS)
#
#  Run this ONE command on a bare Mac; it installs only the prerequisites the
#  smart agent needs (Homebrew, Python+pipx, Ollama + a model, this package),
#  then hands off. Dumb on purpose: no AI, fully deterministic, idempotent —
#  every step is skipped if the thing is already there, so re-running is safe.
#
#    fresh Mac:  bash -c "$(curl -fsSL https://raw.githubusercontent.com/Nadan444x/Setup_Agent/main/bootstrap.sh)"
#    local:      bash bootstrap.sh
# =============================================================================
set -euo pipefail

MODEL="${SETUP_AGENT_MODEL:-qwen2.5:7b}"
REPO_URL="${SETUP_AGENT_REPO:-https://github.com/Nadan444x/Setup_Agent}"
REPO_DIR="$HOME/Projects/Setup_Agent"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m ✓  %s\033[0m\n' "$*"; }
skip() { printf '\033[0;90m ·  %s (already present)\033[0m\n' "$*"; }

# --- 0. find the repo (self-clone if piped from curl outside it) -------------
if [ -f "./pyproject.toml" ] && grep -q 'name = "setup-agent"' ./pyproject.toml 2>/dev/null; then
  REPO_DIR="$(pwd)"
else
  if [ ! -d "$REPO_DIR" ]; then
    say "cloning SetUp Agent into $REPO_DIR"
    if ! command -v git >/dev/null 2>&1; then
      say "git not found — triggering Xcode Command Line Tools install"
      xcode-select --install 2>/dev/null || true
      printf ' waiting for Command Line Tools (git)'
      for _ in $(seq 1 600); do            # up to ~10 min
        command -v git >/dev/null 2>&1 && break
        printf '.'; sleep 1
      done
      echo
      command -v git >/dev/null 2>&1 || {
        echo "git still unavailable. Finish the Xcode Command Line Tools install, then re-run." >&2
        exit 1; }
    fi
    git clone "$REPO_URL" "$REPO_DIR"
  fi
fi

# --- 1. Homebrew (its installer also pulls the Xcode Command Line Tools) -----
if command -v brew >/dev/null 2>&1; then
  skip "Homebrew"
else
  say "installing Homebrew (this also installs Xcode Command Line Tools)"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi
# make brew visible in THIS shell (Apple Silicon vs Intel)
if [ -x /opt/homebrew/bin/brew ]; then eval "$(/opt/homebrew/bin/brew shellenv)"; fi
if [ -x /usr/local/bin/brew ];    then eval "$(/usr/local/bin/brew shellenv)";    fi

# --- 2. Python 3 + pipx -------------------------------------------------------
if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'; then
  skip "Python 3.11+ ($(python3 --version 2>&1))"
else
  say "installing Python 3.12"
  brew install python@3.12
fi
if command -v pipx >/dev/null 2>&1; then
  skip "pipx"
else
  say "installing pipx"
  brew install pipx
  pipx ensurepath >/dev/null || true
fi

# --- 3. Ollama installed + server running ------------------------------------
if command -v ollama >/dev/null 2>&1; then
  skip "Ollama"
else
  say "installing Ollama"
  brew install ollama
fi
if curl -s --max-time 2 http://localhost:11434/api/tags >/dev/null 2>&1; then
  skip "Ollama server"
else
  say "starting the Ollama server (brew service)"
  brew services start ollama
  printf ' waiting for the API'
  for _ in $(seq 1 30); do
    if curl -s --max-time 2 http://localhost:11434/api/tags >/dev/null 2>&1; then break; fi
    printf '.'; sleep 1
  done
  echo
  curl -s --max-time 2 http://localhost:11434/api/tags >/dev/null 2>&1 || {
    echo "Ollama server did not come up — start it manually with: ollama serve" >&2; exit 1; }
fi
ok "Ollama server answering on localhost:11434"

# --- 4. the local model (the agent's brain) -----------------------------------
if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$MODEL"; then
  skip "model $MODEL"
else
  say "pulling model $MODEL (a few GB — one-time download)"
  ollama pull "$MODEL"
fi

# --- 5. the agent itself -------------------------------------------------------
if command -v setup-agent >/dev/null 2>&1; then
  say "updating setup-agent from $REPO_DIR"
  pipx uninstall setup-agent >/dev/null 2>&1 || true
  pipx install "$REPO_DIR" >/dev/null
else
  say "installing setup-agent from $REPO_DIR"
  pipx install "$REPO_DIR"
fi
ok "setup-agent installed: $(setup-agent version 2>/dev/null || echo 'open a new terminal if not on PATH')"

# --- 6. handoff ----------------------------------------------------------------
printf '\n\033[1;32m Prerequisites ready.\033[0m Now let the smart agent take over:\n\n'
printf '   setup-agent scan     # generate Setup.md from this machine\n'
printf '   setup-agent doctor   # verify everything is wired up\n'
printf '   setup-agent setup    # provision the machine from Setup.md\n\n'

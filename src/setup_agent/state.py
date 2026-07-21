"""The living system file: scan machine (macOS or Windows), generate Setup.md, keep it updated.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .safety import run_readonly

_STATE_LOCK = threading.Lock()
_LOCK_FILE = Path.home() / ".setup-agent" / "setup.md.lock"


@contextmanager
def _state_locked():
    with _STATE_LOCK:
        _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        fh = None
        try:
            fh = open(_LOCK_FILE, "a+b")
        except OSError:
            yield
            return

        if sys.platform == "win32":
            import msvcrt
            try:
                fh.seek(0)
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                except OSError:
                    pass
                yield
            finally:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
                fh.close()
        else:
            import fcntl
            try:
                fcntl.flock(fh, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(fh, fcntl.LOCK_UN)
                finally:
                    fh.close()



def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)

# ------------------------------------------------------------ file lookup ----

_profile_path: Path | None = None


def set_profile_path(path: str | Path | None) -> None:
    global _profile_path
    _profile_path = Path(path).expanduser() if path else None


def profile_path() -> Path:
    if _profile_path:
        return _profile_path
    env = os.environ.get("SETUP_AGENT_PROFILE")
    if env:
        return Path(env).expanduser()
    cwd_file = Path.cwd() / "Setup.md"
    if cwd_file.exists():
        return cwd_file
    return Path.home() / "Setup.md"


# ----------------------------------------------------------------- scan ----

_COMMUNICATION = {"slack", "zoom", "whatsapp", "telegram", "discord", "microsoft-teams", "signal"}
_BROWSERS = {"google-chrome", "brave-browser", "firefox", "microsoft-edge", "arc", "safari-technology-preview"}

_MACOS_PREF_KEYS = [
    ("NSGlobalDomain", "AppleInterfaceStyle", "string"),
    ("NSGlobalDomain", "AppleAccentColor", "int"),
    ("NSGlobalDomain", "AppleHighlightColor", "string"),
    ("NSGlobalDomain", "AppleInterfaceStyleSwitchesAutomatically", "bool"),
    ("NSGlobalDomain", "NSTableViewDefaultSizeMode", "int"),
    ("com.apple.dock", "autohide", "bool"),
    ("com.apple.dock", "tilesize", "int"),
    ("com.apple.dock", "magnification", "bool"),
    ("com.apple.dock", "largesize", "int"),
    ("com.apple.dock", "orientation", "string"),
    ("NSGlobalDomain", "KeyRepeat", "int"),
    ("NSGlobalDomain", "InitialKeyRepeat", "int"),
    ("NSGlobalDomain", "AppleShowAllExtensions", "bool"),
    ("com.apple.finder", "AppleShowAllFiles", "bool"),
    ("com.apple.finder", "ShowPathbar", "bool"),
    ("com.apple.finder", "ShowStatusBar", "bool"),
    ("com.apple.screencapture", "location", "string"),
]

_WIN_PREF_KEYS = [
    ("HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize", "AppsUseLightTheme", "dword"),
    ("HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize", "SystemUsesLightTheme", "dword"),
    ("HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "HideFileExt", "dword"),
    ("HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "Hidden", "dword"),
]

_RUNTIME_PROBES = [
    ("Node.js", "node", "node -v"),
    ("npm", "npm", "npm -v"),
    ("pnpm", "pnpm", "pnpm -v"),
    ("Python", "python3" if sys.platform != "win32" else "python", "python --version" if sys.platform == "win32" else "python3 --version"),
    ("uv", "uv", "uv --version"),
    ("Go", "go", "go version"),
    ("Java", "java", "java -version"),
    ("Git", "git", "git --version"),
    ("Docker", "docker", "docker --version"),
    ("Ollama", "ollama", "ollama --version"),
]

_IGNORE_PATTERNS = [
    "Microsoft.VCRedist", "Microsoft.VCLibs", "Microsoft.UI.Xaml",
    "Microsoft.NET", "Microsoft.Net", "Microsoft.DirectX", "Microsoft.DotNet",
    "Microsoft.Windows.AppRuntime", "Microsoft.WindowsAppRuntime", "Microsoft.DesktopAppInstaller",
    "Microsoft.AppInstaller", "Microsoft.GameInput", "Microsoft.Advertising",
    "Microsoft.Bing", "Microsoft.GetHelp", "Microsoft.Getstarted",
    "Microsoft.HEIFImageExtension", "Microsoft.VP9VideoExtensions",
    "Microsoft.WebMediaExtensions", "Microsoft.WebpImageExtension",
    "Microsoft.AV1VideoExtension", "Microsoft.MPEG2VideoExtension",
    "Microsoft.RawImageExtension", "Microsoft.WindowsFeedbackHub",
    "Microsoft.YourPhone", "Microsoft.ZuneMusic", "Microsoft.ZuneVideo",
    "Microsoft.WindowsStore", "Microsoft.WindowsMaps", "Microsoft.WindowsCamera",
    "Microsoft.WindowsAlarms", "Microsoft.WindowsCalculator", "Microsoft.WindowsSoundRecorder",
    "Nvidia.", "PhysX", "Redistributable", "Runtime Package", "System Software",
    "Desktop Runtime", "SDK", "Driver", "Framework Package", "Native Framework",
]


def _lines(command: str) -> list[str]:
    code, out, _ = run_readonly(command)
    if code != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _parse_winget_list(out: str) -> list[tuple[str, str]]:
    lines = out.splitlines()
    header_idx = -1
    id_pos = -1
    for i, line in enumerate(lines):
        if "Name" in line and "Id" in line:
            header_idx = i
            id_pos = line.find("Id")
            break
    if header_idx == -1 or header_idx + 1 >= len(lines):
        return []

    results = []
    seen = set()
    for line in lines[header_idx + 2:]:
        if not line.strip() or line.strip().startswith("-"):
            continue
        if len(line) <= id_pos:
            parts = line.strip().split()
            if len(parts) >= 2:
                name, pkg_id = parts[0], parts[1]
            else:
                continue
        else:
            name = line[:id_pos].strip()
            rest = line[id_pos:].strip().split()
            pkg_id = rest[0] if rest else ""

        if not name or not pkg_id:
            continue

        combined = f"{name} {pkg_id}".lower()
        if any(pat.lower() in combined for pat in _IGNORE_PATTERNS):
            continue

        # Clean ARP / MSIX prefix from pkg_id for display
        clean_id = pkg_id
        for prefix in ("ARP\\Machine\\X86\\", "ARP\\Machine\\X64\\", "ARP\\User\\X86\\", "ARP\\User\\X64\\", "MSIX\\"):
            if clean_id.upper().startswith(prefix.upper()):
                clean_id = clean_id[len(prefix):]
                break

        if clean_id not in seen:
            seen.add(clean_id)
            results.append((name, clean_id))
    return results



def _scan_start_menu_apps() -> list[str]:
    ps_cmd = (
        "Get-ChildItem -Path '$env:APPDATA\\Microsoft\\Windows\\Start Menu\\Programs', "
        "'$env:ProgramData\\Microsoft\\Windows\\Start Menu\\Programs' -Recurse -Filter '*.lnk' | "
        "Select-Object -ExpandProperty BaseName"
    )
    code, out, _ = run_readonly(f"powershell -NoProfile -Command \"{ps_cmd}\"", timeout=15)
    if code != 0 or not out.strip():
        return []

    ignore_shortcuts = {
        "administrative tools", "command prompt", "control panel", "run", "task manager",
        "file explorer", "snoretoast", "uninstall", "uninstall node.js", "vlc media player - reset preferences and cache files",
        "vlc media player skinned", "videolan website", "documentation", "release notes",
        "user guide", "console rar manual", "what is new in the latest version", "winrar help",
        "character map", "component services", "computer management", "disk cleanup", "event viewer",
        "iscsi initiator", "memory diagnostics tool", "odbc data sources (32-bit)", "odbc data sources (64-bit)",
        "performance monitor", "recoverydrive", "registry editor", "resource monitor", "services",
        "system configuration", "system information", "task scheduler", "windows defender firewall with advanced security",
        "steps recorder", "windows media player legacy", "remote desktop connection", "dfrgui",
        "windows powershell", "windows powershell (x86)", "windows powershell ise", "windows powershell ise (x86)",
        "developer command prompt for vs 2022", "developer powershell for vs 2022", "debuggable package manager",
        "idle (python 3.12 64-bit)", "python 3.12 (64-bit)", "python 3.12 manuals (64-bit)", "python 3.12 module docs (64-bit)",
        "install additional tools for node.js", "node.js command prompt"
    }

    apps = []
    seen = set()
    for line in out.splitlines():
        name = line.strip()
        if not name:
            continue
        if name.lower() in ignore_shortcuts:
            continue
        if name not in seen:
            seen.add(name)
            apps.append(name)
    return apps


def scan_system() -> dict:
    """Inventory the machine (macOS or Windows). Read-only."""
    state: dict = {}
    state["os"] = "windows" if sys.platform == "win32" else "macos"

    if sys.platform == "win32":
        code, out, _ = run_readonly("winget list --accept-source-agreements", timeout=45)
        winget_pkgs = _parse_winget_list(out) if code == 0 else []
        state["winget_packages"] = winget_pkgs[:100]
        state["start_menu_apps"] = _scan_start_menu_apps()
        prefs: list[tuple[str, str, str, str]] = []
        for key_path, val_name, v_type in _WIN_PREF_KEYS:
            ps_cmd = f"(Get-ItemProperty -Path '{key_path}' -Name '{val_name}' -ErrorAction SilentlyContinue).{val_name}"
            code, out, _ = run_readonly(f"powershell -NoProfile -Command \"{ps_cmd}\"", timeout=10)
            if code == 0 and out.strip():
                prefs.append((key_path, val_name, v_type, out.strip()))
        state["windows_prefs"] = prefs
        ps_profile = Path.home() / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1"
        state["shell"] = {"shell": "PowerShell", "profile_exists": ps_profile.exists()}
    else:

        state["formulae"] = _lines("brew leaves")
        state["casks"] = _lines("brew list --cask")
        apps: set[str] = set()
        for apps_dir in (Path("/Applications"), Path.home() / "Applications"):
            if apps_dir.is_dir():
                apps.update(p.stem for p in apps_dir.glob("*.app"))
        state["apps"] = sorted(apps)
        mac_prefs: list[tuple[str, str, str, str]] = []
        for domain, key, type_hint in _MACOS_PREF_KEYS:
            code, out, _ = run_readonly(f"defaults read {domain} {key} 2>/dev/null", timeout=15)
            if code == 0 and out.strip():
                mac_prefs.append((domain, key, type_hint, out.strip()))
        state["macos_prefs"] = mac_prefs
        state["shell"] = {
            "shell": os.environ.get("SHELL", "/bin/zsh"),
            "oh_my_zsh": (Path.home() / ".oh-my-zsh").is_dir(),
        }

    runtimes: dict[str, str] = {}
    for label, binary, probe in _RUNTIME_PROBES:
        if shutil.which(binary):
            _, out, err = run_readonly(probe, timeout=20)
            version = (out or err).strip().splitlines()
            runtimes[label] = version[0] if version else "installed"
    state["runtimes"] = runtimes

    state["npm_globals"] = [
        line.split("/node_modules/", 1)[-1].split("\\node_modules\\", 1)[-1]
        for line in _lines("npm ls -g --depth=0 --parseable 2>/dev/null")[1:]
    ]

    identity = {}
    for field in ("name", "email"):
        _, out, _ = run_readonly(f"git config --global user.{field}", timeout=10)
        if out.strip():
            identity[field] = out.strip()
    state["git_identity"] = identity

    gh = shutil.which("gh")
    state["github"] = {
        "gh": bool(gh),
        "authed": bool(gh) and run_readonly("gh auth status", timeout=10)[0] == 0,
        "ssh_key": (Path.home() / ".ssh" / "id_ed25519.pub").exists(),
    }
    return state


# --------------------------------------------------------------- render ----

def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _cask_line(token: str) -> str:
    pretty = token.replace("-", " ").title()
    return f"- {pretty} (`{token}`)   ✅ installed"


def render_profile(state: dict) -> str:
    """Turn a scan into the living Setup.md markdown."""
    os_label = "Windows" if state.get("os") == "windows" else "macOS"
    out: list[str] = [
        f"# Setup — {os_label} Machine Profile   (generated by SetUp Agent · last updated {_stamp()})",
        "",
        f"> Living file: SetUp Agent generated this by scanning the {os_label} machine and updates it",
        "> automatically after every install or setting change. On a new machine, run",
        "> `setup-agent setup` against this file to rebuild everything.",
        "",
    ]

    def section(title: str, lines: list[str]) -> None:
        out.append(f"## {title}")
        out.extend(lines if lines else ["- (none captured)"])
        out.append("")

    if state.get("os") == "windows":
        pkgs_rendered = []
        for pkg in state.get("winget_packages", []):
            if isinstance(pkg, (tuple, list)) and len(pkg) == 2:
                name, pkg_id = pkg
                pkgs_rendered.append(f"- {name} (`{pkg_id}`)   ✅ installed")
            else:
                pkgs_rendered.append(f"- `{pkg}`   ✅ installed")
        section("Applications & Packages (Winget)", pkgs_rendered)
        
        winget_names_flat = {p[0].lower().replace(" ", "") for p in state.get("winget_packages", []) if isinstance(p, (tuple, list))}
        extra_start_apps = [
            a for a in state.get("start_menu_apps", [])
            if a.lower().replace(" ", "") not in winget_names_flat
        ]
        if extra_start_apps:
            section("Apps not from Winget (Start Menu)", [f"- {a}   ✅ installed" for a in sorted(extra_start_apps)])

        section("Windows preferences", [f"- `{k}` `{v_name}` = {val} _({vt})_" for k, v_name, vt, val in state.get("windows_prefs", [])])

    else:
        comm = sorted(c for c in state.get("casks", []) if c in _COMMUNICATION)
        browsers = sorted(c for c in state.get("casks", []) if c in _BROWSERS)
        other_casks = sorted(c for c in state.get("casks", []) if c not in _COMMUNICATION | _BROWSERS)
        brew_apps_flat = {c.replace("-", "").lower() for c in state.get("casks", [])}
        non_brew_apps = [
            a for a in state.get("apps", [])
            if a.replace(" ", "").replace("-", "").lower().removesuffix(".us") not in brew_apps_flat
        ]
        section("Communication", [_cask_line(c) for c in comm])
        section("Browsers", [_cask_line(c) for c in browsers])
        section("Other GUI apps (Homebrew casks)", [_cask_line(c) for c in other_casks])
        section("Apps not from Homebrew", [f"- {a}   ✅ installed" for a in sorted(non_brew_apps)])
        section("Dev tools (Homebrew formulae)", [f"- `{f}`   ✅ installed" for f in state.get("formulae", [])])
        section("macOS preferences", [f"- `{d}` `{k}` = {v}  _({t})_" for d, k, t, v in state.get("macos_prefs", [])])

    section("Runtimes", [f"- {label}: {version}   ✅ installed" for label, version in state.get("runtimes", {}).items()])
    section("Global npm packages", [f"- `{p}`   ✅ installed" for p in state.get("npm_globals", [])])
    section("Git identity", [f"- user.{field} = {value}" for field, value in state.get("git_identity", {}).items()])
    
    if state.get("os") == "windows":
        section("Shell", [f"- shell: PowerShell", f"- profile: {'exists ✅' if state['shell']['profile_exists'] else '⬜ not created yet'}"])
    else:
        section("Shell", [f"- shell: {state['shell']['shell']}", f"- oh-my-zsh: {'yes' if state['shell']['oh_my_zsh'] else 'no'}"])

    gh = state.get("github", {})
    section(
        "Developer accounts",
        [
            f"- GitHub CLI (`gh`): "
            + ("installed" if gh.get("gh") else "⬜ not installed")
            + (", authenticated ✅" if gh.get("authed") else ", ⬜ not logged in — run: setup github"),
            f"- SSH key: " + ("present ✅" if gh.get("ssh_key") else "⬜ none — setup_github will create one"),
        ],
    )

    out.append("## Changelog")
    out.append(f"- {_stamp()}  initial scan — captured {os_label} system profile")
    out.append("")
    return "\n".join(out)


def _minimal_profile() -> str:
    return (
        f"# Setup — Machine Profile   (generated by SetUp Agent · last updated {_stamp()})\n\n"
        "> Living file — updated automatically as the agent installs and changes things.\n"
        "> Run `setup-agent scan` to fill it in from the current machine.\n\n"
        "## Changelog\n"
    )


def write_initial_profile(path: Path | None = None) -> Path:
    target = path or profile_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with _state_locked():
        _atomic_write(target, render_profile(scan_system()))
    return target


def load_profile_text(path: Path | None = None) -> str | None:
    target = path or profile_path()
    if not target.exists():
        return None
    return target.read_text(encoding="utf-8")


def profile_summary(path: Path | None = None) -> str | None:
    text = load_profile_text(path)
    if not text:
        return None
    counts: dict[str, int] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            if current != "Changelog":
                counts[current] = 0
        elif current and current != "Changelog" and line.lstrip().startswith("- "):
            counts[current] += 1
    if not counts:
        return None
    parts = ", ".join(f"{k} ({v})" for k, v in counts.items() if v)
    return f"machine profile has: {parts}. Call read_profile for the full item list."


def record_change(section: str, item: str, note: str) -> str:
    with _state_locked():
        return _record_change_impl(section, item, note)


def _record_change_impl(section: str, item: str, note: str) -> str:
    path = profile_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, _minimal_profile())

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    header = f"## {section}"
    try:
        sec_idx = next(i for i, l in enumerate(lines) if l.strip().lower() == header.lower())
    except StopIteration:
        try:
            log_idx = next(i for i, l in enumerate(lines) if l.strip() == "## Changelog")
        except StopIteration:
            lines += ["", "## Changelog"]
            log_idx = len(lines) - 1
        lines[log_idx:log_idx] = [header, f"- {item}   ✅ installed", ""]
        sec_idx = log_idx
    else:
        end = next(
            (i for i in range(sec_idx + 1, len(lines)) if lines[i].startswith("## ")),
            len(lines),
        )
        tokens = re.findall(r"`[^`]+`", item)
        if tokens:
            def _matches(line: str, _t=tokens) -> bool:
                return all(t in line for t in _t)
        else:
            needle = item.strip("- ").strip()
            def _matches(line: str, _n=needle) -> bool:
                return _n in line

        found = False
        for i in range(sec_idx + 1, end):
            if _matches(lines[i]):
                lines[i] = lines[i].replace("⬜ not installed", "✅ installed").replace("⬜", "✅")
                found = True
                break
        if not found:
            for i in range(end - 1, sec_idx, -1):
                if lines[i].strip() == "- (none captured)":
                    del lines[i]
                    end -= 1
            insert_at = end
            while insert_at > sec_idx + 1 and not lines[insert_at - 1].strip():
                insert_at -= 1
            lines.insert(insert_at, f"- {item}   ✅ installed")

    entry = f"- {_stamp()}  {note}"
    try:
        log_idx = next(i for i, l in enumerate(lines) if l.strip() == "## Changelog")
        end = next(
            (i for i in range(log_idx + 1, len(lines)) if lines[i].startswith("## ")),
            len(lines),
        )
        insert_at = end
        while insert_at > log_idx + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines.insert(insert_at, entry)
    except StopIteration:
        lines += ["", "## Changelog", entry]

    if lines and lines[0].startswith("# Setup"):
        lines[0] = re.sub(r"last updated [^)]*", f"last updated {_stamp()}", lines[0])

    _atomic_write(path, "\n".join(lines) + "\n")
    return f"(recorded in {path.name}: {note})"


def record_removal(token: str, note: str) -> str:
    with _state_locked():
        return _record_removal_impl(token, note)


def _record_removal_impl(token: str, note: str) -> str:
    path = profile_path()
    if not path.exists():
        return f"(no {path.name} to update)"
    lines = path.read_text(encoding="utf-8").splitlines()

    needle = f"`{token}`"
    kept: list[str] = []
    in_changelog = False
    removed = 0
    for line in lines:
        if line.startswith("## "):
            in_changelog = line.strip() == "## Changelog"
        if (not in_changelog) and line.lstrip().startswith("- ") and needle in line:
            removed += 1
            continue
        kept.append(line)

    entry = f"- {_stamp()}  {note}"
    try:
        log_idx = next(i for i, l in enumerate(kept) if l.strip() == "## Changelog")
        end = next(
            (i for i in range(log_idx + 1, len(kept)) if kept[i].startswith("## ")),
            len(kept),
        )
        insert_at = end
        while insert_at > log_idx + 1 and not kept[insert_at - 1].strip():
            insert_at -= 1
        kept.insert(insert_at, entry)
    except StopIteration:
        kept += ["", "## Changelog", entry]

    if kept and kept[0].startswith("# Setup"):
        kept[0] = re.sub(r"last updated [^)]*", f"last updated {_stamp()}", kept[0])

    _atomic_write(path, "\n".join(kept) + "\n")
    return f"(removed {removed} line(s) for `{token}` from {path.name}: {note})"

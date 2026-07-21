"""Tool for macOS system preferences via user-domain `defaults write` (never sudo)."""

from __future__ import annotations

from ..safety import guarded_run_argv, succeeded
from ..state import record_change

_TYPE_FLAGS = {
    "bool": "-bool",
    "int": "-int",
    "float": "-float",
    "string": "-string",
}

_RESTART_HINTS = {
    "com.apple.dock": "killall Dock",
    "com.apple.finder": "killall Finder",
    "com.apple.screencapture": None,
}


def set_macos_default(domain: str, key: str, value: str, value_type: str = "string") -> str:
    """Write one user-domain macOS preference, e.g. dock autohide or key repeat."""
    flag = _TYPE_FLAGS.get(value_type.lower())
    if flag is None:
        return f"ERROR: value_type must be one of {sorted(_TYPE_FLAGS)}, got '{value_type}'."
    if domain.strip().startswith("/") or "sudo" in domain:
        return "REFUSED: only user-domain preferences are allowed (no system files, no sudo)."

    result = guarded_run_argv(
        ["defaults", "write", domain, key, flag, value],
        purpose=f"macOS preference {domain} {key}",
    )

    if succeeded(result):
        note = record_change(
            "macOS preferences",
            f"`{domain}` `{key}` = {value}  _({value_type})_",
            f"set {domain} {key} = {value}",
        )
        result += f"\n{note}"
        hint = _RESTART_HINTS.get(domain)
        if hint:
            result += (
                f"\nNOTE: takes effect after `{hint}` — suggest it to the user, do not run it yourself."
            )
    return result

"""Desktop notifications — one place, used across the app for both macOS and Windows.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

NOTIFY_LOG = Path.home() / ".setup-agent" / "logs" / "notifications.log"


def enabled() -> bool:
    return os.environ.get("SETUP_AGENT_NOTIFY", "1") != "0"


def _log(title: str, message: str) -> None:
    try:
        NOTIFY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with NOTIFY_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {title}: {message}\n")
    except OSError:
        pass


def notify(title: str, message: str, subtitle: str = "") -> None:
    if not enabled():
        return
    _log(title, message)
    try:
        if sys.platform == "win32":
            title_json = json.dumps(title)
            msg_json = json.dumps(message)
            ps_script = (
                f"[reflection.assembly]::loadwithpartialname('System.Windows.Forms') | Out-Null; "
                f"$notify = New-Object System.Windows.Forms.NotifyIcon; "
                f"$notify.Icon = [System.Drawing.SystemIcons]::Information; "
                f"$notify.Visible = $true; "
                f"$notify.ShowBalloonTip(5000, {title_json}, {msg_json}, [System.Windows.Forms.ToolTipIcon]::Info);"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps_script], capture_output=True, timeout=10)
        else:
            tn = shutil.which("terminal-notifier")
            if tn:
                cmd = [tn, "-title", title, "-message", message]
                if subtitle:
                    cmd += ["-subtitle", subtitle]
                subprocess.run(cmd, capture_output=True, timeout=10)
            else:
                script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
                if subtitle:
                    script += f" subtitle {json.dumps(subtitle)}"
                subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except Exception:
        pass

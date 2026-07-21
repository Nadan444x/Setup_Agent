"""The agent loop: LLM proposes tool calls, we execute them, results feed back.
"""

from __future__ import annotations

import json
import sys
import time

import ollama

from . import DEFAULT_MODEL
from .console import assistant_text, console, error, info, rule, warn
from . import debuglog, llm
from .jobs import JOBS
from .state import profile_path, profile_summary
from .tools import FUNCS, TOOLS, dispatch

MAX_STEPS = 30


def _json_objects(text: str) -> list[str]:
    objs, depth, start = [], 0, None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                objs.append(text[start : i + 1])
                start = None
    return objs


_ZERO_ARG_TOOLS = {"account_info", "jobs_status", "scan_system", "read_profile"}


def _tool_param_names(name: str) -> list[str]:
    for t in TOOLS:
        if t["function"]["name"] == name:
            props = list(t["function"]["parameters"]["properties"])
            req = t["function"]["parameters"].get("required", [])
            return list(req) + [p for p in props if p not in req]
    return []


def _parse_callargs(name: str, argstr: str) -> dict:
    import re as _re
    argstr = argstr.strip()
    if not argstr:
        return {}
    kwargs = _re.findall(r'(\w+)\s*=\s*["\']([^"\']*)["\']', argstr)
    if kwargs:
        return {k: v for k, v in kwargs}
    positional = _re.findall(r'["\']([^"\']*)["\']', argstr)
    params = _tool_param_names(name)
    return {params[i]: positional[i] for i in range(min(len(positional), len(params)))}


def _extract_text_toolcalls(content: str) -> list[tuple[str, dict]]:
    import re as _re

    if not content:
        return []
    calls: list[tuple[str, dict]] = []
    seen: set[str] = set()

    def add(name, args):
        if isinstance(name, str) and name in FUNCS and isinstance(args, dict):
            key = f"{name}:{json.dumps(args, sort_keys=True)}"
            if key not in seen:
                seen.add(key)
                calls.append((name, args))

    for blob in _json_objects(content):
        try:
            obj = json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            args = obj.get("arguments", obj.get("parameters", {}))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (json.JSONDecodeError, ValueError):
                    args = {}
            add(obj.get("name"), args if isinstance(args, dict) else {})
    if calls:
        return calls

    for m in _re.finditer(r"([A-Za-z_]\w*)\s*\(([^()]*)\)", content):
        fname, argstr = m.group(1), m.group(2)
        if fname in FUNCS:
            add(fname, _parse_callargs(fname, argstr))
        else:
            for tool in FUNCS:
                if _re.search(rf"['\"]{_re.escape(tool)}['\"]", argstr):
                    add(tool, {})
    if calls:
        return calls

    stripped = content.strip()
    if len(stripped) < 80 and stripped.count(" ") < 6:
        for tool in _ZERO_ARG_TOOLS:
            if _re.search(rf"\b{_re.escape(tool)}\b", stripped):
                add(tool, {})
                break
    return calls


SYSTEM_PROMPT = """\
You are SetUp Agent, a careful assistant that provisions a machine (macOS or Windows) from the terminal.

Environment: {os_name} with {package_manager}. You act ONLY through the provided tools. Some requests come
back REFUSED (safety) or DECLINED — accept that and adapt; never retry a refused command.

ACT, DON'T ASK. When the user asks you to install / uninstall / update / set up something, DO
IT — call the tool right away. NEVER reply with a question like "Would you like me to install
it?", "Shall I proceed?", or "Do you want to continue?". The user's request IS your approval.

Rules:
1. Before installing anything, call check_installed. Never reinstall what exists.
2. Use each app's package token / ID. Call search_brew or search_winget if you're unsure.
3. install_background is THE main install tool. Gather EVERY app/tool requested and call it ONCE. All install in parallel in the background. Already-installed items are skipped automatically.
4. For "update X" / "upgrade X" requests, use brew_upgrade or winget_upgrade. For "set up GitHub" / "log in to github" / "set up ssh", CALL the setup_github tool.
5. Use run_shell only when no dedicated tool fits.
6. Successful changes are recorded into the Setup.md profile automatically — you do not need to update it yourself.
7. When the goal is done, reply in plain text with a short summary: installed / already present / skipped / failed.

A compact summary of the machine profile (Setup.md) follows. Call the read_profile tool
if you need the full item list. If empty, suggest `setup-agent scan`.

{profile}
"""


def _system_message() -> dict:
    profile = profile_summary() or "(no Setup.md yet — run `setup-agent scan`)"
    os_name = "Windows" if sys.platform == "win32" else "macOS"
    pkg_mgr = "Winget and PowerShell" if sys.platform == "win32" else "Homebrew"
    return {"role": "system", "content": SYSTEM_PROMPT.format(os_name=os_name, package_manager=pkg_mgr, profile=profile)}


def _preflight(model: str) -> bool:
    if not llm.server_up():
        error("Ollama server is not running.")
        info(llm.ServerDown.hint)
        return False
    if not llm.has_model(model):
        error(f"model '{model}' is not pulled.")
        info(f"run:  ollama pull {model}")
        return False
    return True


def _loop(model: str, messages: list) -> None:
    empty_tries = 0
    for _ in range(MAX_STEPS):
        t0 = time.time()
        try:
            with console.status("[magenta]🧠 thinking…[/magenta]", spinner="dots"):
                response = llm.chat(model, messages, TOOLS)
        except llm.ModelMissing as exc:
            error(exc.hint)
            return
        except llm.ServerDown as exc:
            error(exc.hint)
            return
        except ollama.ResponseError as exc:
            error(f"Ollama returned an error: {exc}")
            return
        except Exception as exc:
            error(f"unexpected error talking to the model: {exc}")
            return

        think = time.time() - t0
        pe, ec = getattr(response, "prompt_eval_count", 0), getattr(response, "eval_count", 0)
        console.print(f"[dim]· model turn {think:.1f}s  ({pe} in / {ec} out tokens)[/dim]")
        debuglog.log(f"model turn {think:.1f}s  in={pe} out={ec}")

        message = response.message
        messages.append(message)

        if message.content:
            assistant_text(message.content)
            debuglog.log(f"agent said: {message.content[:200]}")

        calls = [
            (c.function.name, dict(c.function.arguments or {}))
            for c in (message.tool_calls or [])
        ]
        if not calls and message.content:
            parsed = _extract_text_toolcalls(message.content)
            if parsed:
                warn("model wrote the tool call as text — recovering and running it.")
                calls = parsed

        if not calls:
            if not (message.content or "").strip():
                empty_tries += 1
                debuglog.log(f"empty response — retry {empty_tries}")
                if empty_tries <= 2:
                    warn("model returned an empty response — nudging it to act.")
                    messages.append({"role": "user", "content":
                        "You returned nothing. If the task is to install / uninstall / update "
                        "something, CALL the right tool now (install_background for installs). "
                        "Otherwise reply in plain text with the answer."})
                    continue
                error("the model isn't producing a usable response — re-run, or try a shorter/clearer request.")
                return
            return

        empty_tries = 0
        from .console import tool_call as render_call, tool_result as render_result

        for name, arguments in calls:
            render_call(name, arguments)
            debuglog.log(f"tool {name}({arguments})")
            t1 = time.time()
            result = dispatch(name, arguments)
            took = time.time() - t1
            ok = not result.startswith(("ERROR", "REFUSED", "DECLINED"))
            render_result(name, result, ok=ok)
            console.print(f"[dim]· {name} {took:.1f}s[/dim]")
            debuglog.log(f"  -> {name} {took:.1f}s: {result.splitlines()[0][:200] if result else ''}")
            messages.append({"role": "tool", "content": result, "tool_name": name})

    warn(f"stopped after {MAX_STEPS} steps — the goal may be incomplete. Re-run to continue.")


def _report_completed_jobs() -> None:
    from .console import error as _error, success as _success
    for job in JOBS.newly_finished():
        if job.status == "done":
            _success(f"background install finished: {job.token or job.label}")
        else:
            _error(f"background install FAILED: {job.token or job.label} (log: {job.log_path})")


def _note_background_jobs() -> None:
    _report_completed_jobs()
    active = JOBS.active()
    if active:
        info(f"{len(active)} install(s) running in the background · `setup-agent jobs` for status.")


def run_goal(goal: str, model: str = DEFAULT_MODEL) -> None:
    if not _preflight(model):
        return
    rule("SetUp Agent")
    logfile = debuglog.start_session(f"run: {goal}")
    info(f"model: {model} · profile: {profile_path()}")
    info(f"live log: tail -f {logfile}")
    messages = [_system_message(), {"role": "user", "content": goal}]
    try:
        _loop(model, messages)
    except KeyboardInterrupt:
        warn("interrupted — nothing else will run.")
    _note_background_jobs()


def chat_repl(model: str = DEFAULT_MODEL) -> None:
    if not _preflight(model):
        return
    rule("SetUp Agent — chat (type 'exit' to quit)")
    logfile = debuglog.start_session("chat session")
    info(f"model: {model} · profile: {profile_path()}")
    info(f"live log: tail -f {logfile}")
    messages = [_system_message()]
    while True:
        _report_completed_jobs()
        try:
            user_input = console.input("[bold cyan]you ›[/bold cyan] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print()
            break
        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit", "q"}:
            break
        messages.append({"role": "user", "content": user_input})
        try:
            _loop(model, messages)
        except KeyboardInterrupt:
            warn("interrupted — waiting for your next instruction.")
    _note_background_jobs()

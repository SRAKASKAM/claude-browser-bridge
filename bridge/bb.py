#!/usr/bin/env python3
"""Thin CLI over the browser bridge — what the agent actually calls.

Every command targets one Chrome profile. Name it with --profile, or set a
default once with `use`; with a single browser connected it is inferred.

    bb health
    bb use private
    bb whoami
    bb open https://www.vinted.pl  # your own tab in the minimized agent window, pinned
    bb navigate https://www.vinted.pl
    bb snapshot
    bb click '#login-button'
    bb type 'input[name=email]' you@example.com --clear
    bb key Enter
    bb text
    bb shot
    bb eval 'location.href'
    bb wheel 800 450 -300 --ctrl   # zoom in at a point (Ctrl+wheel); no --ctrl = scroll
    bb ask "Enter the SMS code" 'input[name=otp]' --until-gone 'input[name=otp]'
                                   # human step: red outline + banner; waits until done,
                                   # then clears it and minimizes the window again
    bb ask-done                    # the same cleanup, for an ask without --until
    bb close                       # close your tab and unpin
    bb record start [--tab ID] [--out DIR]   # record the user's active tab (a bug repro, a behaviour to study)
    bb record stop [--name "what broke"]     # -> ./.recordings/<time>-<slug>/report.md
    bb record status
    bb raw '{"op":"clickAt","args":{"x":120,"y":340}}'

    ... --profile work         # any command, explicit target

`bb` is `uv run ${CLAUDE_PLUGIN_ROOT}/bridge/bb.py` — the skill defines it.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import recording

# Same state dir as the server — see the note in server.py.
HOME = pathlib.Path(os.getenv("BROWSER_BRIDGE_HOME", pathlib.Path.home() / ".claude-browser-bridge"))
PORT = os.getenv("BROWSER_BRIDGE_PORT", "8799")
BASE = f"http://127.0.0.1:{PORT}"

PINS_DIR = HOME / "pins"

# Which agent is asking. Two agents share one browser and one state directory,
# so anything remembered per-profile — the pinned tab, the default profile —
# would otherwise be remembered for BOTH of them, and whoever wrote last would
# silently redirect the other. That is not hypothetical: a personal agent
# inherited a work agent's default profile and opened a shopping site in the
# tab the work agent was mid-task in.
SESSION = (
    os.getenv("BROWSER_BRIDGE_SESSION")
    or os.getenv("CLAUDE_CODE_SESSION_ID")
    or os.getenv("CLAUDE_CODE_HOST_SESSION_ID")
    or "shared"
)
SESSION_KEY = "".join(c if c.isalnum() or c in "-_" else "_" for c in SESSION)[:64]

DEFAULT_PROFILE_FILE = HOME / f"default_profile.{SESSION_KEY}"

# Ops that must never be silently retargeted at the pinned tab.
# Recording targets the user's own tab, never the agent's pinned one.
UNPINNED_OPS = {"tabs", "activate", "whoami", "openTab", "newWindow", "hide", "recordStart", "recordStop"}

# Optional per-machine hook that shows the human a notification for `bb ask`:
#   notify show <key> <title> <body> <command to run on click>
#   notify dismiss <key> [return]   # return: hand focus back to the app the human came from
NOTIFY_HOOK = HOME / "notify"

# Pins older than this point at tabs long gone.
STALE_AFTER_S = 30 * 24 * 3600

# Ops that need a tab of your own. Without a pin these land on "whatever tab is
# active in the last focused window" — the user's tab, or another agent's.
# Refuse instead: a loud error costs one round trip, hijacking someone's tab
# costs their work.
#
# The read-only ones are here for a second reason: `snapshot`, `text` and `shot`
# on an unowned tab quietly pull whatever the user or the other agent had open
# into this agent's context. Reading the wrong page is a leak, not a typo.
TARGETED_OPS = {
    "navigate", "click", "clickAt", "wheel", "type", "key", "eval",
    "shot", "snapshot", "text", "waitFor",
    "attention", "clearAttention", "closeTab", "reveal",
}


def token() -> str:
    tok = os.getenv("BROWSER_BRIDGE_TOKEN")
    if tok:
        return tok.strip()
    path = HOME / "token"
    if not path.exists():
        sys.exit(f"no pairing token — run bridge/setup.sh (expected {path})")
    return path.read_text().strip()


def default_profile() -> str | None:
    env = os.getenv("BROWSER_BRIDGE_PROFILE")
    if env:
        return env.strip()
    if DEFAULT_PROFILE_FILE.exists():
        return DEFAULT_PROFILE_FILE.read_text().strip() or None
    return None


def pin_file(profile: str | None) -> pathlib.Path:
    """Pins are per-profile AND per-agent.

    Per-profile because tab ids from one Chrome mean nothing in another.
    Per-agent because two agents driving the same profile would otherwise share
    one pin file, and the second `pin` would quietly re-aim the first agent.
    """
    return PINS_DIR / f"{profile or '_inferred'}.{SESSION_KEY}.tab"


def pinned_tab(profile: str | None) -> int | None:
    """The tab this session owns, if one was pinned.

    Without a pin, ops hit whatever tab is active — and the user switching tabs
    mid-run means clicks land in an unrelated page. Pin once, then every op is
    addressed explicitly.
    """
    path = pin_file(profile)
    if not path.exists():
        return None
    return int(path.read_text().strip())


def post(op: str, args: dict, profile: str | None, timeout: float = 60.0) -> dict:
    tab = pinned_tab(profile)
    if tab is not None and op not in UNPINNED_OPS and "tabId" not in args:
        args = {**args, "tabId": tab}
    elif tab is None and op in TARGETED_OPS and "tabId" not in args:
        sys.exit(
            json.dumps(
                {
                    "ok": False,
                    "error": f"'{op}' needs a tab of your own — none pinned for "
                    f"profile '{profile or '(inferred)'}'",
                    "fix": "open a tab of your own in the agent window (it pins itself), so "
                    "this lands nowhere near the user's tabs or another agent's:\n"
                    "  bb open <url>\n"
                    "To reuse a tab you already know: pass tabId in args, or `bb pin` it.",
                }
            )
        )
    payload: dict = {"op": op, "args": args}
    if profile:
        payload["profile"] = profile
    req = urllib.request.Request(
        f"{BASE}/cmd",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Bridge-Token": token()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.load(resp)
            if "No tab with id" in str(out.get("error", "")):
                out["fix"] = "your tab is gone (closed by the user, or the agent window was closed) — `bb open <url>` again"
            return out
    except urllib.error.HTTPError as exc:
        return {"ok": False, "http_status": exc.code, "error": exc.read().decode()[:500]}
    except urllib.error.URLError as exc:
        return {"ok": False, "error": f"bridge server unreachable on {BASE}: {exc.reason}"}


def build(argv: list[str]) -> tuple[str, dict]:
    cmd, rest = argv[0], argv[1:]
    match cmd:
        case "navigate":
            return "navigate", {"url": rest[0], "newTab": "--new-tab" in rest}
        case "click":
            return "click", {"selector": rest[0]}
        case "clickat":
            return "clickAt", {"x": float(rest[0]), "y": float(rest[1])}
        case "wheel":
            dx = float(rest[rest.index("--dx") + 1]) if "--dx" in rest else 0
            return "wheel", {"x": float(rest[0]), "y": float(rest[1]), "deltaY": float(rest[2]),
                             "deltaX": dx, "ctrl": "--ctrl" in rest}
        case "type":
            return "type", {"selector": rest[0], "text": rest[1], "clear": "--clear" in rest}
        case "key":
            return "key", {"key": rest[0]}
        case "eval":
            return "eval", {"expression": rest[0]}
        case "text":
            return "text", {"selector": rest[0] if rest else None}
        case "snapshot":
            return "snapshot", {}
        case "shot":
            return "shot", {}
        case "tabs":
            return "tabs", {}
        case "whoami":
            return "whoami", {}
        case "activate":
            return "activate", {"tabId": int(rest[0])}
        case "reveal":
            return "reveal", {"tabId": int(rest[0])} if rest else {}
        case "hide":
            return "hide", {}
        case "waitfor":
            return "waitFor", {"selector": rest[0]} if rest[0].startswith((".", "#", "[")) else {"text": rest[0]}
        case "detach":
            return "detach", {}
        case _:
            sys.exit(f"unknown command: {cmd}\n\n{__doc__}")


def prune_stale_pins() -> None:
    """Drop pin files of sessions that ended long ago — one per session, never removed."""
    cutoff = time.time() - STALE_AFTER_S
    for path in PINS_DIR.glob("*.tab"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def notify(*args: str) -> bool:
    """True only if the hook ran and succeeded — the human is told or he is not."""
    if not (NOTIFY_HOOK.exists() and os.access(NOTIFY_HOOK, os.X_OK)):
        return False
    try:
        return subprocess.run([str(NOTIFY_HOOK), *args], check=False, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# How long the page must be free of keystrokes before a finished step closes,
# and how long to wait for that before leaving the window up after all.
TYPING_QUIET_S = 3
TYPING_WAIT_MAX_S = 30
POLL_S = 2

# True once the page has had no keystrokes for TYPING_QUIET_S. Installs its own
# listener on a page the user navigated to after the ask, counting from then.
IDLE_JS = (
    "(() => { if (!window.__claudeTyping) { window.__claudeTyping = true;"
    " const m = () => (window.__claudeLastKey = Date.now());"
    " addEventListener('keydown', m, true); addEventListener('input', m, true);"
    " window.__claudeLastKey = Date.now(); }"
    f" return Date.now() - (window.__claudeLastKey || 0) > {TYPING_QUIET_S * 1000}; }})()"
)


def take_flag(argv: list[str], name: str) -> tuple[list[str], str | None]:
    if name not in argv:
        return argv, None
    i = argv.index(name)
    if i + 1 >= len(argv):
        sys.exit(f"{name} needs a value")
    return argv[:i] + argv[i + 2:], argv[i + 1]


def done_condition(argv: list[str]) -> tuple[list[str], str | None]:
    """The JS expression that is truthy once the human's step is done, if any."""
    argv, js = take_flag(argv, "--until")
    argv, gone = take_flag(argv, "--until-gone")
    argv, url = take_flag(argv, "--until-url")
    if sum(x is not None for x in (js, gone, url)) > 1:
        sys.exit("give one of --until, --until-gone, --until-url")
    if "" in (js, gone, url):
        sys.exit("an --until flag needs a non-empty value")
    if gone:
        # A page still loading has no field yet — that is not "gone".
        js = f"document.readyState === 'complete' && !document.querySelector({json.dumps(gone)})"
    elif url:
        js = f"location.href.includes({json.dumps(url)})"
    return argv, js


def ask(argv: list[str], profile: str | None) -> dict:
    """Hand a step to the human without raising any window.

    The page gets a red outline on each selector and a red bar with the message;
    the human gets a notification whose click reveals exactly this tab. With a
    done condition it waits for the step and then cleans up by itself — clears
    the marks, dismisses the notification, minimizes the window — because an
    agent told to do that afterwards eventually forgets.
    """
    argv, until = done_condition(argv)
    argv, timeout_arg = take_flag(argv, "--timeout")
    if not argv:
        sys.exit('usage: bb ask "what the human must do" [selector ...] '
                 "[--until JS | --until-gone SELECTOR | --until-url TEXT] [--timeout SECONDS]")
    if timeout_arg is not None and until is None:
        sys.exit("--timeout only applies with a done condition (--until, --until-gone, --until-url)")
    try:
        timeout = 600.0 if timeout_arg is None else float(timeout_arg)
    except ValueError:
        sys.exit(f"--timeout takes seconds, got {timeout_arg!r}")
    message, selectors = argv[0], argv[1:]
    if until is not None and (tab := pinned_tab(profile)) is not None:
        # Checked before anything is shown. A condition that throws (not CSS:
        # `:has-text`, a JS typo) would never come true; one that already holds
        # (a selector typo, a field not rendered yet or inside an iframe) would
        # report a step as done that nobody did.
        value, error = page_eval(until, tab, profile)
        if error and error != "transient":
            return {"ok": False, "done": False, "error": f"the done condition fails in the page: {error}"}
        if value:
            return {"ok": False, "done": False,
                    "error": "the done condition already holds before the human acted — check it "
                             "(selector typo? field not rendered yet, or inside an iframe?)"}
    out = post("attention", {"message": message, "selectors": selectors}, profile)
    if not out.get("ok"):
        return out
    tab = out["result"]["tabId"]
    who = out.get("profile") or profile or ""
    # The hook may run with a bare PATH and environment (Hammerspoon has no uv):
    # bb.py is stdlib-only, so the interpreter running it now is enough, and the
    # bridge's own settings travel along explicitly.
    env = [f"{k}={os.environ[k]}" for k in ("BROWSER_BRIDGE_HOME", "BROWSER_BRIDGE_PORT") if k in os.environ]
    reveal = " ".join(
        shlex.quote(part)
        for part in ["env", *env, sys.executable, str(pathlib.Path(__file__).resolve()),
                     "reveal", str(tab), "--profile", who]
    )
    out["notified"] = notify("show", f"{who}:{tab}", f"Chrome: {who}", message, reveal)
    if not out["notified"]:
        out["note"] = (f"no notification shown (hook {NOTIFY_HOOK} missing or failed) — "
                       "tell the human yourself; do not raise the window")
    if until is None:
        return out
    deadline = time.time() + timeout
    hits = 0
    while time.time() < deadline:
        value, error = page_eval(until, tab, profile)
        if error == "gone":
            notify("dismiss", f"{who}:{tab}")
            return {**out, "ok": False, "done": False, "error": "your tab is gone — the user closed it"}
        # Twice in a row: a re-render or a reload after a wrong code can drop
        # the field for a moment while the step is still the user's.
        hits = hits + 1 if value else 0
        if hits >= 2:
            out["done"] = True
            out["finish"] = finish(tab, who, profile)
            return out
        time.sleep(POLL_S)
    out["ok"] = False
    out["done"] = False
    out["error"] = "timed out waiting for the human — the marks and the notification are still up"
    return out


def page_eval(expression: str, tab: int, profile: str | None) -> tuple[object, str | None]:
    """Evaluate in the tab: (value, None), or (None, why it failed).

    "gone" — the tab was closed; "transient" — the bridge or a navigating page
    did not answer; anything else is the page's own error message.
    """
    out = post("eval", {"expression": expression, "tabId": tab}, profile)
    if out.get("ok"):
        return out.get("result", {}).get("value"), None
    error = str(out.get("error", ""))
    if "No tab with id" in error:
        return None, "gone"
    if "http_status" in out or "unreachable" in error or "context" in error.lower():
        return None, "transient"
    return None, error


def page_value(expression: str, tab: int, profile: str | None):
    """Evaluate in the tab; any failure counts as not-yet."""
    return page_eval(expression, tab, profile)[0]


def finish(tab: int, who: str, profile: str | None, cleared: bool = False) -> dict:
    """Close a human step: marks off, notification gone, window back to the Dock.

    Not mid-keystroke: the page must have been quiet for a few seconds; if the
    user keeps typing, the window stays up. It also stays up if he switched to
    another tab in it. Focus goes back to the app he came from only when the
    window really was minimized from under him.
    """
    key = f"{who}:{tab}"
    if not cleared:
        post("clearAttention", {"tabId": tab}, profile)
    deadline = time.time() + TYPING_WAIT_MAX_S
    while page_value(IDLE_JS, tab, profile) is False:
        if time.time() > deadline:
            notify("dismiss", key)
            return {"hidden": False, "reason": "the user is still typing there"}
        time.sleep(1)
    hide = post("hide", {"owner": tab}, profile)
    result = hide.get("result", hide)
    give_back = bool(result.get("hidden") and result.get("wasFocused"))
    notify("dismiss", key, *(["return"] if give_back else []))
    return result


def ask_done(profile: str | None) -> dict:
    out = post("clearAttention", {}, profile)
    if out.get("ok"):
        who = out.get("profile") or profile or ""
        out["finish"] = finish(out["result"]["cleared"], who, profile, cleared=True)
    return out


def rec_state_file(profile: str | None) -> pathlib.Path:
    """The running recording, per profile and per agent — like pins."""
    return HOME / "recordings" / f"current.{profile or '_inferred'}.{SESSION_KEY}.json"


def git_exclude(base: pathlib.Path) -> None:
    """Keep .recordings/ out of commits without touching the repo's own .gitignore."""
    for d in [base, *base.parents]:
        git = d / ".git"
        if git.is_dir():
            info = git
        elif git.is_file():  # a worktree: "gitdir: <path>", exclude lives in the common dir
            text = git.read_text().strip()
            if not text.startswith("gitdir:"):
                return
            info = (d / text.split(":", 1)[1].strip()).resolve()
            common = info / "commondir"
            if common.exists():
                info = (info / common.read_text().strip()).resolve()
        else:
            continue
        exclude = info / "info" / "exclude"
        text = exclude.read_text() if exclude.exists() else ""
        if ".recordings/" not in (line.strip() for line in text.splitlines()):
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with open(exclude, "a") as fh:
                fh.write(("\n" if text and not text.endswith("\n") else "") + ".recordings/\n")
        return


def record(argv: list[str], profile: str | None) -> dict:
    sub = argv[0] if argv else "status"
    state_file = rec_state_file(profile)
    if sub == "stop" and not state_file.exists():
        # Started with --profile and stopped without one, or the other way round:
        # this session's only recording of that profile is the one meant. A
        # different profile named outright is refused.
        mine = [f for f in (HOME / "recordings").glob(f"current.*.{SESSION_KEY}.json")
                if profile is None or json.loads(f.read_text()).get("profile") == profile]
        if len(mine) == 1:
            state_file = mine[0]
    state = json.loads(state_file.read_text()) if state_file.exists() else None

    if sub == "status":
        out = {"ok": True, "active": state is not None, **({"state": state} if state else {})}
        if state is None:
            # One this session lost track of (a reply that never came back) still blocks starts.
            browser = post("recordStatus", {}, profile)
            if browser.get("ok") and (browser.get("result") or {}).get("recording"):
                out["browser"] = browser["result"]
                out["fix"] = f"a recording is running in the browser — `bb record stop --rec {browser['result'].get('rec')}`"
        return out

    if sub == "start":
        rest, tab = take_flag(argv[1:], "--tab")
        rest, out_base = take_flag(rest, "--out")
        if state is not None:
            return {"ok": False, "error": f"a recording is already running ({state['rec']}) — "
                    "`bb record stop` it first", "state": state}
        recording.prune(HOME)
        rec = uuid.uuid4().hex[:12]
        args: dict = {"rec": rec}
        if tab is not None:
            if not tab.isdigit():
                return {"ok": False, "error": f"--tab takes a tab id from `bb tabs`, got {tab!r}"}
            args["tabId"] = int(tab)
        out = post("recordStart", args, profile)
        if not out.get("ok"):
            # Started in the browser but the reply never came back: no state here,
            # and the browser refuses every new start until it is stopped.
            running = re.search(r"already running \((\w+)\)", str(out.get("error", "")))
            if running:
                out["fix"] = (f"a recording this session does not know about is running — if it is yours, "
                              f"`bb record stop --rec {running.group(1)}` stops it and builds its bundle here")
            return out
        result = out.get("result") or {}
        state = {"rec": rec, "profile": out.get("profile") or profile, "started": time.time(),
                 "out": str(pathlib.Path(out_base or os.getcwd()).resolve()),
                 "tabId": result.get("tabId"), "url": result.get("url"), "title": result.get("title")}
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state))
        return {**out, "recording": rec,
                "next": "tell the user to do in that tab what should be recorded (reproduce the bug, walk through the behaviour) and to say when to stop; "
                        "then run `bb record stop --name \"<what broke>\"`"}

    if sub == "stop":
        rest, name = take_flag(argv[1:], "--name")
        _, orphan = take_flag(rest, "--rec")
        if state is None and orphan:
            if not re.fullmatch(r"\w{1,64}", orphan):
                return {"ok": False, "error": f"bad recording id {orphan!r}"}
            state = {"rec": orphan, "profile": profile, "started": time.time(), "out": os.getcwd()}
        if state is None:
            return {"ok": False, "error": "no recording is running in this session — `bb record start` first"}
        stop = post("recordStop", {"rec": state["rec"]}, state.get("profile") or profile)
        raw = recording.raw_dir(HOME, state["rec"])
        if not (raw / "events.jsonl").exists():
            state_file.unlink(missing_ok=True)
            return {"ok": False, "error": "nothing was recorded — no events reached the bridge server",
                    "stop": stop}
        info = recording.start_info(raw)
        host = urllib.parse.urlsplit(info.get("url") or state.get("url") or "").hostname or ""
        slug = recording.slug(name or info.get("title") or state.get("title") or host) or "session"
        stamp = time.strftime("%Y-%m-%d-%H%M", time.localtime(state["started"]))
        base = pathlib.Path(state["out"])
        dest = base / ".recordings" / f"{stamp}-{slug}"
        n = 2
        while dest.exists():
            dest = base / ".recordings" / f"{stamp}-{slug}-{n}"
            n += 1
        try:
            recording.run_deep(recording.build_bundle, raw, dest, name)
        except Exception as exc:
            # A failed build must not leave a recording that blocks every later start.
            state_file.unlink(missing_ok=True)
            return {"ok": False, "error": f"could not build the bundle: {exc!r}", "raw": str(raw)}
        git_exclude(base)
        state_file.unlink(missing_ok=True)
        out = {"ok": True, "path": str(dest), "report": str(dest / "report.md")}
        if not stop.get("ok"):
            out["warning"] = ("the extension did not confirm the stop — the bundle holds what reached "
                              f"the server: {stop.get('error')}")
        return out

    sys.exit("usage: bb record start [--tab ID] [--out DIR] | stop [--name TEXT] [--rec ID] | status")


def take_profile_flag(argv: list[str]) -> tuple[list[str], str | None]:
    """Pull --profile NAME / --profile=NAME out of anywhere in the argv."""
    out: list[str] = []
    profile: str | None = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--profile" and i + 1 < len(argv):
            profile = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--profile="):
            profile = arg.split("=", 1)[1]
            i += 1
            continue
        out.append(arg)
        i += 1
    return out, profile or default_profile()


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    argv, profile = take_profile_flag(sys.argv[1:])

    if argv[0] == "health":
        try:
            with urllib.request.urlopen(f"{BASE}/health", timeout=5) as resp:
                data = json.load(resp)
        except urllib.error.URLError as exc:
            sys.exit(f"bridge server not running on {BASE}: {exc.reason}")
        data["default_profile"] = default_profile()
        print(json.dumps(data, indent=2))
        return

    if argv[0] == "use":
        if len(argv) < 2:
            print(json.dumps({"default_profile": default_profile()}))
            return
        DEFAULT_PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
        DEFAULT_PROFILE_FILE.write_text(argv[1].strip())
        print(json.dumps({"ok": True, "default_profile": argv[1].strip()}))
        return

    if argv[0] == "pin":
        PINS_DIR.mkdir(parents=True, exist_ok=True)
        pin_file(profile).write_text(str(int(argv[1])))
        print(json.dumps({"ok": True, "profile": profile, "pinned_tab": int(argv[1])}))
        return

    if argv[0] == "open":
        prune_stale_pins()
        previous = pinned_tab(profile)
        out = post("openTab", {"url": argv[1]} if len(argv) > 1 else {}, profile)
        if out.get("ok") and previous is not None:
            # One tab per session: the one this pin used to own would otherwise
            # keep running, unseen, in the agent window. closeTab refuses
            # anything outside that window, so this cannot hit the user's tab.
            post("closeTab", {"tabId": previous}, profile)
        if out.get("ok"):
            PINS_DIR.mkdir(parents=True, exist_ok=True)
            pin_file(profile).write_text(str(out["result"]["tabId"]))
            out["pinned_tab"] = out["result"]["tabId"]
        print(json.dumps(out, indent=2, ensure_ascii=False))
        sys.exit(0 if out.get("ok") else 1)

    if argv[0] == "close":
        out = post("closeTab", {}, profile)
        if out.get("ok"):
            pin_file(profile).unlink(missing_ok=True)
        print(json.dumps(out, indent=2, ensure_ascii=False))
        sys.exit(0 if out.get("ok") else 1)

    if argv[0] == "record":
        out = record(argv[1:], profile)
        print(json.dumps(out, indent=2, ensure_ascii=False))
        sys.exit(0 if out.get("ok") else 1)

    if argv[0] in ("ask", "ask-done"):
        out = ask(argv[1:], profile) if argv[0] == "ask" else ask_done(profile)
        print(json.dumps(out, indent=2, ensure_ascii=False))
        sys.exit(0 if out.get("ok") else 1)

    if argv[0] == "unpin":
        pin_file(profile).unlink(missing_ok=True)
        print(json.dumps({"ok": True, "profile": profile, "pinned_tab": None}))
        return

    if argv[0] == "raw":
        payload = json.loads(argv[1])
        out = post(payload["op"], payload.get("args") or {}, profile)
    else:
        op, args = build(argv)
        out = post(op, args, profile)

    print(json.dumps(out, indent=2, ensure_ascii=False))
    if not out.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()

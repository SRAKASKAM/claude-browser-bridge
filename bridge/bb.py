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
    bb ask "Enter the SMS code" 'input[name=otp]'   # human step: red outline + banner
    bb ask-done                    # clear it once the human has acted
    bb close                       # close your tab and unpin
    bb raw '{"op":"clickAt","args":{"x":120,"y":340}}'

    ... --profile work         # any command, explicit target

`bb` is `uv run ${CLAUDE_PLUGIN_ROOT}/bridge/bb.py` — the skill defines it.
"""

from __future__ import annotations

import json
import os
import pathlib
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request

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
UNPINNED_OPS = {"tabs", "activate", "whoami", "openTab", "newWindow", "hide"}

# Optional per-machine hook that shows the human a notification for `bb ask`:
#   notify show <key> <title> <body> <command to run on click>
#   notify dismiss <key>
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


def ask(argv: list[str], profile: str | None) -> dict:
    """Hand a step to the human without raising any window.

    The page gets a red outline on each selector and a red bar with the message;
    the human gets a notification whose click reveals exactly this tab.
    """
    if not argv:
        sys.exit('usage: bb ask "what the human must do" [selector ...]')
    message, selectors = argv[0], argv[1:]
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
    return out


def ask_done(profile: str | None) -> dict:
    out = post("clearAttention", {}, profile)
    if out.get("ok"):
        notify("dismiss", f"{out.get('profile') or profile or ''}:{out['result']['cleared']}")
        post("hide", {}, profile)
    return out


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

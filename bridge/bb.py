#!/usr/bin/env python3
"""Thin CLI over the browser bridge — what the agent actually calls.

Every command targets one Chrome profile. Name it with --profile, or set a
default once with `use`; with a single browser connected it is inferred.

    bb health
    bb use private
    bb whoami
    bb tabs
    bb pin 1234567
    bb navigate https://www.vinted.pl
    bb snapshot
    bb click '#login-button'
    bb type 'input[name=email]' you@example.com --clear
    bb key Enter
    bb text
    bb shot
    bb eval 'location.href'
    bb raw '{"op":"clickAt","args":{"x":120,"y":340}}'

    ... --profile work         # any command, explicit target

`bb` is `uv run ${CLAUDE_PLUGIN_ROOT}/bridge/bb.py` — the skill defines it.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
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
UNPINNED_OPS = {"tabs", "activate", "whoami"}

# Ops that need a tab of your own. Without a pin these land on "whatever tab is
# active in the last focused window" — the user's tab, or another agent's.
# Refuse instead: a loud error costs one round trip, hijacking someone's tab
# costs their work.
#
# The read-only ones are here for a second reason: `snapshot`, `text` and `shot`
# on an unowned tab quietly pull whatever the user or the other agent had open
# into this agent's context. Reading the wrong page is a leak, not a typo.
TARGETED_OPS = {
    "navigate", "click", "clickAt", "type", "key", "eval",
    "shot", "snapshot", "text", "waitFor",
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
                    "fix": "open your own window and pin the tab it returns, so this "
                    "lands nowhere near the user's tabs or another agent's:\n"
                    "  bb raw '{\"op\":\"newWindow\",\"args\":{\"url\":\"about:blank\"}}'\n"
                    "  bb pin <tabId from that reply>\n"
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
            return json.load(resp)
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
        case "waitfor":
            return "waitFor", {"selector": rest[0]} if rest[0].startswith((".", "#", "[")) else {"text": rest[0]}
        case "detach":
            return "detach", {}
        case _:
            sys.exit(f"unknown command: {cmd}\n\n{__doc__}")


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

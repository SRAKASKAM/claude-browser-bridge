#!/usr/bin/env python3
"""Fast judgments for browser steps — TypeSafe's Jev, through OpenRouter with ZDR.

Reading a whole snapshot and choosing what to click costs the agent a model turn.
Jev answers that kind of narrow question in about half a second, so the agent
can hand it over and only take the step back when Jev is unsure.

    jev do "the Add to cart button"            # list elements → Jev picks → click
    jev do "the search field" --type "shoes"   # pick → type into it
    jev pick "the Sign in link"                # pick only, print the selector
    jev check "the order confirmation is shown"            # yes-probability
    jev check "results have loaded" --wait 20              # poll until yes

Exit codes: 0 done / yes, 3 Jev unsure — the agent decides itself (candidates are
printed), 4 check answered no or timed out, 1 error.

Privacy: every request carries provider.zdr=true and data_collection=deny, so
OpenRouter routes only to endpoints with zero data retention and refuses rather
than falling back to one that keeps prompts. What the user typed — input and
textarea values, Chrome autofill, contenteditable editors (mail drafts, chat
boxes) — is never read into a request: fields are described by their label,
placeholder or name, and `check` reads only visible text outside editors. The
page's own echo of typed text (an autocomplete "Search for '…'") can still go
through. URLs are sent as origin + path, without query or fragment.

Not seen: elements inside iframes or shadow roots, and icon-only controls with
no aria-label/title/name — on such pages `jev` hands back with exit 3.
Candidate x/y are viewport coordinates at listing time; act on `selector`.

`jev` is `uv run ${CLAUDE_PLUGIN_ROOT}/bridge/jev.py`; the skill defines it.

Key: OPENROUTER_API_KEY, else ~/.claude-browser-bridge/openrouter.key.
Profile and pinned tab are whatever `bb` uses — pass --profile through.
"""

from __future__ import annotations

import json
import math
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
HOME = pathlib.Path(os.getenv("BROWSER_BRIDGE_HOME", pathlib.Path.home() / ".claude-browser-bridge"))
ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
MODEL = os.getenv("JEV_MODEL", "typesafe/jev-1.14")
PRIVACY = {"zdr": True, "data_collection": "deny"}

# Below these Jev's answer is not acted on; the agent gets the candidates instead.
def threshold(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        sys.exit(f"{name} must be a number")


PICK_MIN = threshold("JEV_PICK_MIN", 0.8)
FITS_MIN = threshold("JEV_FITS_MIN", 0.7)
CHECK_YES = threshold("JEV_CHECK_YES", 0.8)

CHOICE_MAX = 250  # API allows 255 options per Choice
ELEMENTS_MAX = 600
TEXT_MAX = 60_000  # characters of page text for `check` (the text op defaults to 20000)

# Lists the visible interactive elements and tags each with data-jev="N", so the
# click goes to exactly the element Jev picked — the snapshot op's selectors can
# be tag chains that match several elements. Field values are never read.
LIST_JS = r"""
(() => {
  document.querySelectorAll('[data-jev]').forEach(e => e.removeAttribute('data-jev'));
  const q = 'a[href],button,input:not([type=hidden]),select,textarea,summary,[role=button],[role=link],'
    + '[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=option],[role=switch],[contenteditable=true]';
  const plain = t => (t || '').replace(/\s+/g, ' ').trim().slice(0, 120);
  const out = [];
  for (const el of document.querySelectorAll(q)) {
    if (out.length >= %d) break;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') continue;
    const tag = el.tagName.toLowerCase(), type = (el.getAttribute('type') || '').toLowerCase();
    const field = (tag === 'input' && !['submit', 'button', 'reset', 'image'].includes(type))
      || tag === 'textarea' || tag === 'select' || el.isContentEditable;
    let label = el.getAttribute('aria-label') || '';
    if (!label && el.labels && el.labels.length) label = [...el.labels].map(l => l.innerText).join(' ');
    if (!label && !field) label = el.innerText || (tag === 'input' ? el.value : '');
    if (!label) label = el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('name') || '';
    label = plain(label);
    if (!label) continue;
    el.setAttribute('data-jev', String(out.length));
    out.push({id: out.length, tag, type, label, x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)});
  }
  return {url: location.origin + location.pathname, title: document.title, elements: out};
})()
""" % ELEMENTS_MAX

# Visible page text for `check`, skipping form fields and contenteditable
# editors — innerText (the text op) would include what the user is typing there.
TEXT_JS = r"""
(() => {
  const skip = el => el.closest('input,textarea,select,script,style,noscript,[contenteditable=""],[contenteditable=true]')
    || el.isContentEditable || (el.checkVisibility && !el.checkVisibility());
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const parts = [];
  let total = 0, node;
  while ((node = walk.nextNode()) && total < %d) {
    const el = node.parentElement, t = node.nodeValue.replace(/\s+/g, ' ').trim();
    if (!t || !el || skip(el)) continue;
    parts.push(t); total += t.length + 1;
  }
  return {url: location.origin + location.pathname, title: document.title, text: parts.join('\n')};
})()
""" % TEXT_MAX

CLEAN_JS = "document.querySelectorAll('[data-jev]').forEach(e => e.removeAttribute('data-jev')); 'ok'"

# Bridge errors that won't clear up by waiting: fail at once instead of polling.
PERMANENT = ("needs a tab of your own", "not running", "pairing token", "Connection refused", "no profile")


class BridgeError(Exception):
    def __init__(self, msg: str, permanent: bool = False):
        super().__init__(msg)
        self.permanent = permanent


def api_key() -> str:
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        path = HOME / "openrouter.key"
        if path.exists():
            key = path.read_text().strip()
    if not key:
        sys.exit("no OpenRouter key: set OPENROUTER_API_KEY or write ~/.claude-browser-bridge/openrouter.key")
    return key


def bb(*args: str, profile: str | None) -> dict:
    # bb.py is stdlib-only but needs 3.10+; skip uv's startup when we already have that.
    py = [sys.executable] if sys.version_info >= (3, 10) else ["uv", "run", "--quiet"]
    cmd = [*py, str(HERE / "bb.py"), *args]
    if profile:
        cmd += ["--profile", profile]
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    out = subprocess.run(cmd, capture_output=True, text=True, env=env)
    try:
        res = json.loads(out.stdout)
    except json.JSONDecodeError:
        # bb answers every op with JSON; anything else means bb itself broke.
        raise BridgeError(f"bb {args[0]} failed: {(out.stdout + out.stderr).strip()[:500]}", permanent=True)
    if not res.get("ok", False):
        raise BridgeError(json.dumps(res))
    return res.get("result", res)


def page_eval(js: str, key: str, profile: str | None) -> dict:
    res = bb("eval", js, profile=profile)
    page = res.get("value", res)
    if not isinstance(page, dict) or key not in page:
        raise BridgeError(f"page script returned {str(page)[:200]}")
    return page


def list_elements(profile: str | None) -> dict:
    return page_eval(LIST_JS, "elements", profile)


def clean(profile: str | None) -> None:
    try:
        bb("eval", CLEAN_JS, profile=profile)
    except BridgeError:
        pass


def decide(state, questions: dict) -> dict:
    body = {"model": MODEL, "provider": PRIVACY, "state": state, "questions": questions}
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())["answers"]
    except urllib.error.HTTPError as e:
        sys.exit(f"Jev request failed: {e.code} {e.read().decode(errors='replace')[:500]}")
    except (urllib.error.URLError, TimeoutError) as e:
        sys.exit(f"Jev unreachable: {e}")
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        sys.exit(f"Jev returned an unexpected body: {e!r}")


def candidates(page: dict) -> list[dict]:
    out = []
    for el in page.get("elements", []):
        kind = f"{el['tag']} {el['type']}".strip() if el.get("type") else el["tag"]
        out.append({**el, "id": f"e{el['id']}", "selector": f'[data-jev="{el["id"]}"]',
                    "label": f"<{kind}> {el['label']}"})
    return out


def pick(target: str, profile: str | None) -> tuple[dict | None, list[dict], dict]:
    page = list_elements(profile)
    cands = candidates(page)
    if not cands:
        return None, [], {"reason": "no labelled interactive elements on the page"}
    chunks = [cands[i : i + CHOICE_MAX] for i in range(0, len(cands), CHOICE_MAX)]
    state = {"page": {"url": page.get("url"), "title": page.get("title")}, "target": target}
    questions = {}
    for n, chunk in enumerate(chunks):
        state[f"elements_{n}"] = {c["id"]: c["label"] for c in chunk}
        questions[f"pick_{n}"] = {
            "type": "choice",
            "instructions": f"Which element in `elements_{n}` is `target` on this page?",
            "criteria": {c["id"]: c["label"] for c in chunk},
        }
        questions[f"fits_{n}"] = {
            "type": "noul",
            "instructions": f"Is `target` one of the elements listed in `elements_{n}`?",
        }
    ans = decide(state, questions)
    try:
        n = max(range(len(chunks)), key=lambda i: ans[f"fits_{i}"]["noul"])
        fits = ans[f"fits_{n}"]["noul"]
        choice = ans[f"pick_{n}"]
        chosen = next(c for c in chunks[n] if c["id"] == choice["choice"])
        prob = choice["probabilities"][choice["choice"]]
        info = {"fits": round(fits, 3), "prob": round(prob, 3), "confidence": round(choice["confidence"], 3)}
        top = sorted(chunks[n], key=lambda c: -choice["probabilities"].get(c["id"], 0))[:5]
    except (KeyError, TypeError, StopIteration) as e:
        sys.exit(f"Jev returned an unexpected answer: {e!r}")
    if fits < FITS_MIN or prob < PICK_MIN:
        return None, top, info
    return chosen, top, info


def show(c: dict) -> dict:
    return {k: c.get(k) for k in ("selector", "label", "x", "y")}


def cmd_pick(target: str, profile: str | None, act: str | None, text: str | None) -> int:
    t0 = time.monotonic()
    try:
        chosen, top, info = pick(target, profile)
        if chosen is None:
            print(json.dumps({"ok": False, "unsure": True, **info, "candidates": [show(c) for c in top]},
                             ensure_ascii=False))
            return 3
        # The listing and the click are one Jev round trip apart; a framework
        # that patches nodes in place could have reused the tagged node by then.
        if act == "click":
            bb("click", chosen["selector"], profile=profile)
        elif act == "type":
            bb("type", chosen["selector"], text or "", "--clear", profile=profile)
    except BridgeError as e:
        sys.exit(str(e))
    finally:
        clean(profile)
    print(json.dumps({"ok": True, "did": act or "pick", **show(chosen), **info,
                      "seconds": round(time.monotonic() - t0, 2)}, ensure_ascii=False))
    return 0


def cmd_check(condition: str, profile: str | None, wait: float) -> int:
    t0 = time.monotonic()
    p, error = None, None
    while True:
        try:
            page = page_eval(TEXT_JS, "text", profile)
        except BridgeError as e:
            # Mid-navigation the page can't be read yet: that is "not yet", not a failure.
            if wait <= 0 or e.permanent or any(m in str(e) for m in PERMANENT):
                sys.exit(str(e))
            page, error = None, str(e)
        if page is not None:
            ans = decide(
                {"url": page.get("url"), "page_text": str(page.get("text", ""))[:TEXT_MAX], "condition": condition},
                {"holds": {"type": "noul",
                           "instructions": "Judging by `page_text`, does `condition` hold on this page right now?"}},
            )
            try:
                p = ans["holds"]["noul"]
            except (KeyError, TypeError) as e:
                sys.exit(f"Jev returned an unexpected answer: {e!r}")
        elapsed = time.monotonic() - t0
        if (p is not None and p >= CHECK_YES) or elapsed >= wait:
            if p is None:
                sys.exit(f"page never readable: {error}")
            res = {"ok": p >= CHECK_YES, "yes": round(p, 3), "seconds": round(elapsed, 2)}
            if error and p < CHECK_YES:
                res["last_error"] = error
            print(json.dumps(res))
            return 0 if p >= CHECK_YES else 4
        time.sleep(min(1.5, wait - elapsed))


def take_flag(argv: list[str], name: str) -> str | None:
    """Remove `--name VALUE` / `--name=VALUE` from argv; ValueError if it has no value."""
    for i, arg in enumerate(argv):
        if arg == name:
            if i + 1 >= len(argv):
                raise ValueError(f"{name} needs a value")
            value = argv[i + 1]
            del argv[i : i + 2]
            return value
        if arg.startswith(name + "="):
            del argv[i]
            return arg.split("=", 1)[1]
    return None


def main(argv: list[str]) -> int:
    argv = list(argv)
    try:
        profile = take_flag(argv, "--profile")
        wait_flag = take_flag(argv, "--wait")
        text = take_flag(argv, "--type")
        wait = float(wait_flag or 0)
        if not (math.isfinite(wait) and wait >= 0):
            raise ValueError("--wait must be a number of seconds")
        if len(argv) != 2 or argv[0] not in {"do", "pick", "check"}:
            raise ValueError("usage")
        if wait_flag is not None and argv[0] != "check":
            raise ValueError("--wait only applies to check")
        if text is not None and argv[0] != "do":
            raise ValueError("--type only applies to do")
    except ValueError as e:
        print(f"{e}\n{__doc__}")
        return 1
    cmd, arg = argv
    if cmd == "check":
        return cmd_check(arg, profile, wait)
    if cmd == "pick":
        return cmd_pick(arg, profile, None, None)
    return cmd_pick(arg, profile, "type" if text is not None else "click", text)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

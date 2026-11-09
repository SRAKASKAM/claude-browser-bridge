"""Browser recordings: redact what the browser streams, then build the bundle an agent reads.

While `bb record` runs, the extension streams every event of the recorded tabs
to the server, which calls `append()`: the event is redacted and written to
`<home>/recordings/<rec>/events.jsonl`, screenshots next to it. Nothing
unredacted ever reaches the disk. On stop, `bb` calls `build_bundle()`, which
turns that raw stream into a folder another agent can read offline:

    report.md       errors first, then every step with links to the rest
    actions.jsonl   the user's steps
    console.jsonl   console messages, uncaught exceptions, browser log entries
    network.jsonl   one line per request: request, response, failure, body file
    bodies/         response bodies (XHR, fetch, documents), 256 KB max each
    shots/NNN.jpg   a screenshot per step
    dom/NNN.html    the page's DOM after each step, rebuilt from rrweb
    dom/err-NNN.html  the DOM at the moment of an error
    rrweb.json      the raw rrweb events, for a human replay

Stdlib only: server.py and bb.py both import it.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import html
import json
import pathlib
import re
import shutil
import time
import urllib.parse

# What the bundle keeps of a body. The extension sends bodies whole (up to
# SEND_CAP in recorder.js) so they are redacted before they are cut: a cut JSON
# body no longer parses, and its secrets would slip past field-by-field redaction.
BODY_CAP = 256 * 1024
BODY_TYPES = {"XHR", "Fetch", "Document"}  # keep in sync with recorder.js
REDACTED = "[redacted]"
# Error-time DOM snapshots per bundle: a page that throws in a loop would
# otherwise write thousands of them.
MAX_ERROR_DOMS = 50
# A mutation batch adding or removing this many nodes is a "big DOM change":
# the usual reason a click freezes the page.
BIG_DOM = 500

# ------------------------------------------------------------------ redaction

# Matched against the words of a key or header name: "X-CSRF-Token" is
# x / csrf / token, "refreshToken" is refresh / token.
SECRET_WORDS = {
    "password", "passwd", "pass", "pwd", "passphrase", "secret", "token", "otp", "totp", "pin",
    "cvv", "cvc", "cvv2", "cvc2", "apikey", "authorization", "auth", "session", "sessionid", "sid",
    "csrf", "xsrf", "credential", "credentials", "cookie", "cookies", "jwt", "signature", "sig",
}
SECRET_PAIRS = [
    ("card", "number"), ("card", "num"), ("card", "no"), ("cc", "number"), ("cc", "num"),
    ("api", "key"), ("private", "key"), ("access", "key"), ("security", "code"),
    ("verification", "code"), ("auth", "code"), ("sms", "code"), ("mfa", "code"), ("one", "time"),
]
# In URLs and form posts "code" alone is an OAuth code or a one-time code; in
# a JSON body it is usually an error code, which the debugging agent needs.
# "at" is the XSRF token Google apps post with every request.
FORM_ONLY_WORDS = {"code", "key", "at"}

JWT = re.compile(r"eyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]*")
# After "Token" the value must look like one, or "Token validation failed" loses its words.
BEARER = re.compile(r"\b(Bearer|Basic|Token(?=\s+[\w.~+/=-]*\d))\s+[\w.~+/=-]{8,}", re.I)
# Any URL parameter; its value goes when its name reads as a secret by the
# same word rule as form fields (token, X-Amz-Signature, access_key, jwt, ...).
QUERY_PARAM = re.compile(r"([?&#;])([^=&#;\s\"'<>]{1,80})=([^&#;\s\"'<>]*)")
HEADER_LINE = re.compile(
    r"^((?:proxy-)?authorization|cookie|set-cookie|x-[\w-]*(?:token|key|secret|auth)[\w-]*):[^\r\n]*",
    re.I | re.M,
)
# Strings that carry a whole request or response body.
BODY_KEYS = {"postData", "body", "payloadData"}
# Dropped outright: Chrome repeats the request body here, base64-encoded,
# where no pattern can see into it.
DROP_KEYS = {"postDataEntries"}
# "key": "value" pairs, for JSON that no longer parses (cut at the size cap)
# and for JSON inside an HTML page's inline scripts.
JSON_PAIR = re.compile(r'"([^"\\]{1,80})"(\s*:\s*)("(?:[^"\\]|\\.)*"|-?\d[\d.eE+-]*)')
HTML_INPUT = re.compile(r"<input\b[^>]*>", re.I)
HTML_META = re.compile(r"<meta\b[^>]*>", re.I)
HTML_ATTR = re.compile(r'(\b(value|content)\s*=\s*)("[^"]*"|\'[^\']*\'|[^\s>]+)', re.I)
MULTIPART_PART = re.compile(r'(Content-Disposition:\s*form-data;[^\r\n]*?\bname="([^"]*)"[^\r\n]*\r?\n(?:[^\r\n]+\r?\n)*\r?\n)(.*?)(?=\r?\n--)', re.I | re.S)


def _words(key: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    return [w for w in re.split(r"[^a-z0-9]+", spaced.lower()) if w]


def secret_key(key: str, form: bool = False) -> bool:
    if key.lower().startswith("access-control-"):
        return False  # CORS headers: "Allow-Credentials" is what a CORS bug turns on
    words = _words(key)
    if any(w in SECRET_WORDS for w in words):
        return True
    if form and any(w in FORM_ONLY_WORDS for w in words):
        return True
    return any(a in words and b in words for a, b in SECRET_PAIRS)


def _query_param(m: re.Match) -> str:
    name = urllib.parse.unquote(m.group(2))
    if m.group(3) and secret_key(name, form=True):
        return f"{m.group(1)}{m.group(2)}={REDACTED}"
    return m.group(0)


def scrub_text(text: str) -> str:
    """Secrets that can sit inside any free text: tokens, auth headers, URL params."""
    text = JWT.sub(REDACTED, text)
    text = BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = QUERY_PARAM.sub(_query_param, text)
    return HEADER_LINE.sub(lambda m: f"{m.group(1)}: {REDACTED}", text)


def _redact_pairs(text: str) -> str:
    return JSON_PAIR.sub(lambda m: f'"{m.group(1)}"{m.group(2)}"{REDACTED}"' if secret_key(m.group(1)) else m.group(0), text)


def _redact_html(text: str) -> str:
    """Server-rendered values: hidden and password inputs, secret-named metas, inline state."""
    def tag(m: re.Match) -> str:
        el = m.group(0)
        # (?<![\w-]): not data-name= or data-type=.
        name = re.search(r'(?<![\w-])(?:name|id|property|http-equiv)\s*=\s*["\']?([^"\'\s>]+)', el, re.I)
        kind = re.search(r'(?<![\w-])type\s*=\s*["\']?(\w+)', el, re.I)
        secret = (kind and kind.group(1).lower() in ("hidden", "password")) or (name and secret_key(name.group(1), form=True))
        return HTML_ATTR.sub(lambda a: f'{a.group(1)}"{REDACTED}"', el) if secret else el
    text = HTML_INPUT.sub(tag, text)
    text = HTML_META.sub(tag, text)
    return scrub_text(_redact_pairs(text))


def _redact_multipart(text: str) -> str:
    return MULTIPART_PART.sub(
        lambda m: m.group(1) + (REDACTED if secret_key(m.group(2), form=True) else scrub_text(m.group(3))), text)


def _redact_body(text: str) -> str:
    """A body string: JSON, form and multipart posts field by field, HTML by its fields, anything else as free text."""
    stripped = text.strip()
    # socket.io and friends prefix a JSON frame with a packet type: 42["auth",{...}]
    framed = re.match(r"(\d{1,4})([\[{].*)$", stripped, re.S)
    if framed:
        return framed.group(1) + _redact_body(framed.group(2))
    if stripped[:1] in "{[":
        try:
            return json.dumps(redact(json.loads(stripped)), ensure_ascii=False)
        except ValueError:
            return scrub_text(_redact_pairs(text))  # not valid JSON after all
    if re.search(r"Content-Disposition:\s*form-data", text, re.I):
        return _redact_multipart(text)
    if stripped[:1] == "<":
        return _redact_html(text)
    if "=" in stripped and not re.search(r"\s", stripped) and stripped[:1] not in "<{[":
        pairs = urllib.parse.parse_qsl(stripped, keep_blank_values=True)
        if pairs:
            return urllib.parse.urlencode(
                [(k, REDACTED if v and secret_key(k, form=True) else scrub_text(v)) for k, v in pairs],
                safe="[]",
            )
    return scrub_text(text)


def redact(obj, _key: str | None = None):
    """A redacted deep copy of any JSON value.

    Key and header names stay, so an agent can still see that an Authorization
    header or a password field was sent; only the values go.
    """
    if isinstance(obj, dict):
        out = {}
        # {name: "csrf-token", content: "..."} — a <meta> tag, a cookie object.
        named_secret = isinstance(obj.get("name"), str) and secret_key(obj["name"], form=True)
        for k, v in obj.items():
            if k in DROP_KEYS:
                continue
            if (secret_key(k) or (named_secret and k in ("value", "content"))) and _scalar(v):
                out[k] = REDACTED
            elif secret_key(k) and isinstance(v, (dict, list)) and v:
                out[k] = REDACTED
            else:
                out[k] = redact(v, k)
        return out
    if isinstance(obj, list):
        return [redact(v, _key) for v in obj]
    if isinstance(obj, str):
        if _key in BODY_KEYS:
            return _redact_body(obj)
        # JSON inside a JSON string: GraphQL "variables", a stringified payload.
        if obj[:1] in "{[" and obj[-1:] in "}]" and len(obj) < BODY_CAP:
            try:
                inner = json.loads(obj)
            except ValueError:
                pass
            else:
                if isinstance(inner, (dict, list)):
                    return json.dumps(redact(inner), ensure_ascii=False)
        return scrub_text(obj)
    return obj


def _scalar(v) -> bool:
    return isinstance(v, (str, int, float)) and not isinstance(v, bool) and v != ""


def _b64_text(data: str) -> str | None:
    """Text behind base64, or None if it is binary. A body cut at the cap may end mid-character."""
    try:
        raw = base64.b64decode(data)
    except ValueError:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.start >= len(raw) - 3 and exc.reason == "unexpected end of data":
            return raw[: exc.start].decode("utf-8", "replace")
        return None


def _redact_message(msg: dict) -> dict:
    params = msg.get("params") or {}
    method = msg.get("method") or ""
    if method.startswith("Network.webSocketFrame") and (params.get("response") or {}).get("opcode") == 2:
        # A binary frame: nothing in it can be checked for secrets.
        params = {**params, "response": {**params["response"], "payloadData": "(binary frame, not kept)"}}
        msg = {**msg, "params": params}
    if method == "rec.body" and params.get("base64Encoded"):
        # Text served base64 (some APIs do): decode so it can be redacted at all.
        text = _b64_text(params.get("body") or "")
        params = {**params, "body": text if text is not None else "", "base64Encoded": False,
                  **({} if text is not None else {"binary": True})}
        msg = {**msg, "params": params}
    return redact(msg)


# ------------------------------------------------------------------ raw store


def raw_dir(home: pathlib.Path, rec_id: str) -> pathlib.Path:
    return pathlib.Path(home) / "recordings" / rec_id


def append(home: pathlib.Path, msg: dict) -> None:
    """Redact one streamed event and append it to its recording."""
    rec = str(msg.get("rec") or "")
    if not re.fullmatch(r"[\w-]{1,64}", rec):
        raise ValueError(f"bad recording id: {rec!r}")
    raw = raw_dir(home, rec)
    raw.mkdir(parents=True, exist_ok=True)
    if msg.get("method") == "rec.shot":
        params = msg.get("params") or {}
        seq = int(params.get("seq", 0))
        shots = raw / "shots"
        shots.mkdir(exist_ok=True)
        (shots / f"{seq}.jpg").write_bytes(base64.b64decode(params.get("data") or ""))
        msg = {**msg, "params": {"seq": seq, "file": f"shots/{seq}.jpg"}}
    line = json.dumps(_redact_message(msg), ensure_ascii=False)
    with open(raw / "events.jsonl", "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def read_events(raw: pathlib.Path) -> list[dict]:
    path = pathlib.Path(raw) / "events.jsonl"
    if not path.exists():
        return []
    events = []
    # Not splitlines(): it also splits on U+2028 and friends, which JSON strings may hold raw.
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue  # a line cut short by a crash mid-write
    return events


def start_info(raw: pathlib.Path) -> dict:
    """The page the recording started on: {url, title}."""
    for ev in read_events(raw):
        if ev.get("method") == "rec.start":
            return ev.get("params") or {}
    return {}


def prune(home: pathlib.Path, max_age_s: float = 7 * 24 * 3600) -> None:
    """Raw recordings are kept a week after the bundle is built, then dropped."""
    root = pathlib.Path(home) / "recordings"
    cutoff = time.time() - max_age_s
    for d in root.glob("*/"):
        try:
            if (d / "events.jsonl").stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


# ------------------------------------------------------------- DOM rebuilding
#
# rrweb records one full snapshot of the page (a serialized node tree with ids)
# and then incremental mutations against those ids. Replaying them here gives
# the DOM at any moment as plain HTML an agent can read and grep.

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class Dom:
    def __init__(self) -> None:
        self.root: dict | None = None
        self.index: dict[int, dict] = {}
        self.parent: dict[int, int] = {}
        self.href = ""

    def _index(self, node: dict, parent: int | None) -> None:
        nid = node.get("id")
        if nid is not None:
            self.index[nid] = node
            if parent is not None:
                self.parent[nid] = parent
        for child in node.get("childNodes") or []:
            self._index(child, nid)

    def _unindex(self, node: dict) -> None:
        self.index.pop(node.get("id"), None)
        self.parent.pop(node.get("id"), None)
        for child in node.get("childNodes") or []:
            self._unindex(child)

    def _detach(self, nid: int) -> dict | None:
        node = self.index.get(nid)
        pid = self.parent.get(nid)
        if node is None:
            return None
        if pid is not None and pid in self.index:
            kids = self.index[pid].setdefault("childNodes", [])
            self.index[pid]["childNodes"] = [c for c in kids if c.get("id") != nid]
        return node

    def apply(self, event: dict) -> dict | None:
        """Apply one rrweb event; for a mutation, return how many nodes it added and removed."""
        etype, data = event.get("type"), event.get("data") or {}
        if etype == 4:
            self.href = data.get("href") or self.href
        elif etype == 2:
            self.root = copy.deepcopy(data.get("node"))
            self.index, self.parent = {}, {}
            if self.root:
                self._index(self.root, None)
        elif etype == 3 and self.root is not None:
            source = data.get("source")
            if source == 0:
                return self._mutate(data)
            elif source == 5:
                node = self.index.get(data.get("id"))
                if node is not None and node.get("type") == 2:
                    attrs = node.setdefault("attributes", {})
                    if "text" in data:
                        attrs["value"] = data["text"]
                    if data.get("isChecked") is not None:
                        if data["isChecked"]:
                            attrs["checked"] = ""
                        else:
                            attrs.pop("checked", None)

    def _mutate(self, data: dict) -> dict:
        before = len(self.index)
        for rm in data.get("removes") or []:
            node = self._detach(rm.get("id"))
            if node is not None:
                self._unindex(node)
        after_removes = len(self.index)
        pending = list(data.get("adds") or [])
        # An add may name a sibling that arrives later in the same batch.
        while pending:
            rest = []
            for add in pending:
                parent = self.index.get(add.get("parentId"))
                nxt = add.get("nextId")
                if parent is None or (nxt is not None and nxt not in self.index):
                    rest.append(add)
                    continue
                self._insert(add, parent, nxt)
            if len(rest) == len(pending):
                for add in rest:  # siblings that never came: append at the end
                    parent = self.index.get(add.get("parentId"))
                    if parent is not None:
                        self._insert(add, parent, None)
                break
            pending = rest
        stats = {"added": len(self.index) - after_removes, "removed": before - after_removes}
        adds = data.get("adds") or []
        if stats["added"] >= BIG_DOM and adds:
            parent = self.index.get(adds[0].get("parentId"))
            stats["into"] = _describe(parent) if parent else "?"
            kinds: dict[str, int] = {}
            for add in adds:
                _kinds(add.get("node") or {}, kinds)
            top = sorted(kinds.items(), key=lambda kv: -kv[1])[:3]
            stats["repeated"] = [f"{k} ×{v}" for k, v in top]
        for t in data.get("texts") or []:
            node = self.index.get(t.get("id"))
            if node is not None:
                node["textContent"] = t.get("value") or ""
        for a in data.get("attributes") or []:
            node = self.index.get(a.get("id"))
            if node is None:
                continue
            attrs = node.setdefault("attributes", {})
            for name, value in (a.get("attributes") or {}).items():
                if value is None:
                    attrs.pop(name, None)
                elif name == "style" and isinstance(value, dict):
                    attrs["style"] = _style_diff(attrs.get("style", ""), value)
                else:
                    attrs[name] = value
        return stats

    def _insert(self, add: dict, parent: dict, nxt: int | None) -> None:
        node = copy.deepcopy(add.get("node") or {})
        if node.get("id") in self.index:  # a move: drop it from its old place
            self._unindex(self._detach(node["id"]) or {})
        kids = parent.setdefault("childNodes", [])
        pos = next((i for i, c in enumerate(kids) if c.get("id") == nxt), len(kids)) if nxt is not None else len(kids)
        kids.insert(pos, node)
        self._index(node, parent.get("id"))

    def html(self, css: "CssStore", target: int | None = None) -> str | None:
        if self.root is None:
            return None
        out: list[str] = []
        _serialize(self.root, out, css, target)
        return "".join(out)


def _describe(node: dict) -> str:
    tag = (node.get("tagName") or "#node").lower()
    attrs = node.get("attributes") or {}
    if attrs.get("id"):
        return f"{tag}#{attrs['id']}"
    cls = str(attrs.get("class") or "").split()
    return tag + "".join(f".{c}" for c in cls[:3])


def _kinds(node: dict, out: dict[str, int]) -> None:
    """Counts of element kinds (tag + classes) in an added subtree: what got rendered en masse."""
    if node.get("type") == 2 and not node.get("isSVG"):  # icon internals drown the real items
        key = _describe(node)
        out[key] = out.get(key, 0) + 1
    for child in node.get("childNodes") or []:
        _kinds(child, out)


def _style_diff(current: str, diff: dict) -> str:
    props: dict[str, str] = {}
    for part in current.split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            props[k.strip()] = v.strip()
    for k, v in diff.items():
        if v is False:
            props.pop(k, None)
        elif isinstance(v, list):
            props[k] = f"{v[0]} !{v[1]}" if len(v) > 1 and v[1] else str(v[0])
        else:
            props[k] = str(v)
    return "; ".join(f"{k}: {v}" for k, v in props.items())


class CssStore:
    """Inlined stylesheets go to dom/css/<hash>.css once, shared by every snapshot."""

    def __init__(self, dom_dir: pathlib.Path) -> None:
        self.dir = dom_dir / "css"

    def link(self, css: str) -> str:
        name = hashlib.sha1(css.encode("utf-8", "replace")).hexdigest()[:12] + ".css"
        path = self.dir / name
        if not path.exists():
            self.dir.mkdir(parents=True, exist_ok=True)
            path.write_text(css, encoding="utf-8")
        return f'<link rel="stylesheet" href="css/{name}">'


def _attr(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _serialize(node: dict, out: list[str], css: CssStore, target: int | None) -> None:
    ntype = node.get("type")
    if ntype == 0:
        for child in node.get("childNodes") or []:
            _serialize(child, out, css, target)
    elif ntype == 1:
        out.append(f"<!DOCTYPE {node.get('name') or 'html'}>\n")
    elif ntype == 3:
        if node.get("isStyle"):
            out.append(node.get("textContent") or "")
        else:
            out.append(html.escape(node.get("textContent") or "", quote=False))
    elif ntype == 5:
        out.append(f"<!--{(node.get('textContent') or '').replace('--', '- -')}-->")
    elif ntype == 2:
        tag = (node.get("tagName") or "div").lower()
        attrs = dict(node.get("attributes") or {})
        # A snapshot opened in a browser must not run, or redirect to, the page's code.
        if tag in ("script", "base") or (tag == "meta" and str(attrs.get("http-equiv", "")).lower() == "refresh"):
            return
        sheet = attrs.pop("_cssText", None)
        if tag == "link" and sheet:
            out.append(css.link(sheet))
            return
        kids = node.get("childNodes") or []
        if tag == "style":
            # rrweb v2 keeps the rules in _cssText and leaves the text child empty.
            text = sheet or "".join(c.get("textContent") or "" for c in kids if c.get("type") == 3)
            out.append(css.link(text) if len(text) > 2000 else f"<style>{text}</style>")
            return
        parts = [tag]
        for k, v in attrs.items():
            if k.startswith("rr_") or k.lower().startswith("on") or not re.fullmatch(r"[^\s\"'>/=]+", k):
                continue
            if isinstance(v, str) and re.match(r"\s*javascript:", v, re.I):
                v = "#"
            parts.append(k if v is True or v == "" else f'{k}="{_attr(v)}"')
        if target is not None and node.get("id") == target:
            parts.append('data-recorded-target="this step"')
        out.append(f"<{' '.join(parts)}>")
        if tag == "head":
            out.append('<meta http-equiv="Content-Security-Policy" content="script-src \'none\'">')
        if tag in VOID:
            return
        shadow = [c for c in kids if c.get("isShadow")]
        if shadow:
            out.append('<template shadowrootmode="open">')
            for child in shadow:
                _serialize(child, out, css, target)
            out.append("</template>")
        for child in kids:
            if not child.get("isShadow"):
                _serialize(child, out, css, target)
        out.append(f"</{tag}>")


# ---------------------------------------------------------------- the bundle


def _clock(t: float | None) -> str:
    if not t:
        return "?"
    return time.strftime("%H:%M:%S", time.localtime(t / 1000)) + f".{int(t % 1000):03d}"


def _frames(stack: dict | None, limit: int = 8) -> list[str]:
    frames = []
    for f in ((stack or {}).get("callFrames") or [])[:limit]:
        fn = f.get("functionName") or "(anonymous)"
        frames.append(f"{fn} ({f.get('url') or '?'}:{(f.get('lineNumber') or 0) + 1}:{(f.get('columnNumber') or 0) + 1})")
    return frames


def _arg_text(arg: dict) -> str:
    if "value" in arg:
        v = arg["value"]
        return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    if "unserializableValue" in arg:
        return str(arg["unserializableValue"])
    preview = arg.get("preview")
    if preview and preview.get("properties") is not None:
        props = ", ".join(f"{p.get('name')}: {p.get('value')}" for p in preview["properties"][:10])
        return f"{arg.get('description') or arg.get('className') or 'Object'} {{{props}}}"
    return str(arg.get("description") or arg.get("type") or "")


def _console_entry(method: str, params: dict) -> dict | None:
    if method == "Runtime.consoleAPICalled":
        level = params.get("type") or "log"
        level = {"warning": "warn", "assert": "error"}.get(level, level)
        text = " ".join(_arg_text(a) for a in params.get("args") or [])
        return {"source": "console", "level": level, "text": text, "stack": _frames(params.get("stackTrace"))}
    if method == "Runtime.exceptionThrown":
        d = params.get("exceptionDetails") or {}
        desc = (d.get("exception") or {}).get("description") or d.get("text") or "exception"
        stack = _frames(d.get("stackTrace"))
        if not stack and d.get("url"):
            stack = [f"({d['url']}:{(d.get('lineNumber') or 0) + 1}:{(d.get('columnNumber') or 0) + 1})"]
        return {"source": "exception", "level": "error", "text": desc.split("\n")[0], "detail": desc,
                "stack": stack}
    if method == "Log.entryAdded":
        e = params.get("entry") or {}
        level = {"warning": "warn"}.get(e.get("level"), e.get("level") or "info")
        stack = _frames(e.get("stackTrace"))
        return {"source": f"browser:{e.get('source') or 'other'}", "level": level, "text": e.get("text") or "",
                "url": e.get("url"), "stack": stack}
    return None


def slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:limit].strip("-")


def _write_jsonl(path: pathlib.Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def _body_ext(mime: str | None, body: str) -> str:
    mime = (mime or "").lower()
    if "json" in mime or body.lstrip()[:1] in ("{", "["):
        return "json"
    if "html" in mime:
        return "html"
    if "xml" in mime:
        return "xml"
    return "txt"


def build_bundle(raw: pathlib.Path, out_dir: pathlib.Path, name: str | None = None) -> pathlib.Path:
    raw, out = pathlib.Path(raw), pathlib.Path(out_dir)
    events = read_events(raw)
    out.mkdir(parents=True, exist_ok=True)
    dom_dir, shots_dir, bodies_dir = out / "dom", out / "shots", out / "bodies"
    css = CssStore(dom_dir)
    doms: dict[int | None, Dom] = {}
    dom_files: dict[str, str] = {}  # content hash -> relative path, so equal states share a file

    def dump(tab, fname: str, target: int | None = None) -> str | None:
        dom = doms.get(tab)
        page = dom.html(css, target) if dom else None
        if page is None:
            return None
        key = hashlib.sha1(page.encode("utf-8", "replace")).hexdigest()
        if key in dom_files:
            return dom_files[key]
        dom_dir.mkdir(parents=True, exist_ok=True)
        (dom_dir / fname).write_text(page, encoding="utf-8")
        dom_files[key] = f"dom/{fname}"
        return dom_files[key]

    start: dict = {}
    stop_t = None
    steps: list[dict] = []
    console: list[dict] = []
    network: dict[str, dict] = {}
    gaps: list[dict] = []
    rrweb: list[dict] = []
    dom_changes: list[dict] = []
    slow_frames: list[dict] = []
    error_doms = 0
    current = 0  # seq of the step the events belong to (0 = before the first)

    def close_step() -> None:
        if steps:
            last = steps[-1]
            last["dom"] = dump(last["tabId"], f"{last['seq']:03d}.html", last.get("rrwebId"))

    for ev in events:
        method, params, t, tab = ev.get("method"), ev.get("params") or {}, ev.get("t"), ev.get("tabId")
        if method == "rec.start":
            start = {**params, "t": t}
        elif method == "rec.stop":
            stop_t = t
        elif method == "rec.rrweb":
            event = params.get("event") or {}
            rrweb.append({**event, "tabId": tab})
            stats = doms.setdefault(tab, Dom()).apply(event)
            if stats and (stats["added"] >= BIG_DOM or stats["removed"] >= BIG_DOM):
                when = event.get("timestamp") or t
                dom_changes.append({"t": when, "time": _clock(when), "step": current, "tabId": tab, **stats})
        elif method == "rec.perf":
            when = params.get("t") or t
            slow_frames.append({"t": when, "time": _clock(when), "step": current, "tabId": tab,
                                **{k: v for k, v in params.items() if k != "t"}})
        elif method == "rec.action":
            close_step()
            current = int(params.get("seq") or len(steps) + 1)
            step = {"seq": current, "t": t, "time": _clock(t), "tabId": tab,
                    **{k: v for k, v in params.items() if k != "seq"}, "shot": None, "dom": None}
            steps.append(step)
            # The screenshot lands a moment after the step, under the same seq.
            shot = raw / f"shots/{current}.jpg"
            if shot.exists():
                shots_dir.mkdir(exist_ok=True)
                shutil.copyfile(shot, shots_dir / f"{current:03d}.jpg")
                step["shot"] = f"shots/{current:03d}.jpg"
        elif method in ("rec.detached", "rec.tab", "rec.dropped"):
            gaps.append({"t": t, "time": _clock(t), "step": current, "tabId": tab, **params, "kind": method})
        elif method in ("Runtime.consoleAPICalled", "Runtime.exceptionThrown", "Log.entryAdded"):
            if method == "Log.entryAdded" and (params.get("entry") or {}).get("level") == "verbose":
                continue  # Chrome's own lint hints ("inputs should have autocomplete")
            entry = _console_entry(method, params)
            entry = {"n": len(console) + 1, "t": t, "time": _clock(t), "step": current, "tabId": tab, **entry}
            if entry["level"] == "error" and error_doms < MAX_ERROR_DOMS:
                entry["dom"] = dump(tab, f"err-{entry['n']:03d}.html")
                error_doms += 1
            console.append(entry)
        elif method.startswith("Network.") or method in ("rec.body", "rec.postData"):
            rid = params.get("requestId")
            if not rid:
                continue
            req = network.setdefault(rid, {"requestId": rid, "t": t, "time": _clock(t), "step": current, "tabId": tab})
            if method == "Network.requestWillBeSent":
                if params.get("redirectResponse"):
                    r = params["redirectResponse"]
                    req.setdefault("redirects", []).append({"url": req.get("url"), "status": r.get("status")})
                r = params.get("request") or {}
                req.update(method=r.get("method"), url=r.get("url"), type=params.get("type"),
                           requestHeaders={**req.get("requestHeaders", {}), **(r.get("headers") or {})},
                           _t0=params.get("timestamp"))
                if r.get("postData") is not None:
                    req["postData"] = r["postData"]
                if params.get("initiator", {}).get("type"):
                    req["initiator"] = params["initiator"]["type"]
            elif method == "rec.postData":
                req["postData"] = (f"(request body of {params.get('size')} characters, too large to redact — not kept)"
                                   if params.get("tooLarge") else params.get("postData"))
            elif method == "Network.requestWillBeSentExtraInfo":
                req["requestHeaders"] = {**req.get("requestHeaders", {}), **(params.get("headers") or {})}
            elif method == "Network.responseReceived":
                r = params.get("response") or {}
                req.update(status=r.get("status"), statusText=r.get("statusText"), mimeType=r.get("mimeType"),
                           type=params.get("type") or req.get("type"),
                           responseHeaders={**(r.get("headers") or {}), **req.get("responseHeaders", {})})
                req.setdefault("url", r.get("url"))
            elif method == "Network.responseReceivedExtraInfo":
                req["responseHeaders"] = {**req.get("responseHeaders", {}), **(params.get("headers") or {})}
                if req.get("status") is None and params.get("statusCode"):
                    req["status"] = params["statusCode"]
            elif method == "Network.loadingFinished":
                if req.get("_t0") and params.get("timestamp"):
                    req["durationMs"] = round((params["timestamp"] - req["_t0"]) * 1000)
                req["size"] = params.get("encodedDataLength")
            elif method == "Network.loadingFailed":
                req["failed"] = params.get("errorText") or "failed"
                req["canceled"] = bool(params.get("canceled"))
                if params.get("blockedReason"):
                    req["blockedReason"] = params["blockedReason"]
            elif method == "Network.webSocketCreated":
                req.update(type="WebSocket", url=params.get("url"), method="GET")
            elif method.startswith("Network.webSocketFrame"):
                frame = params.get("response") or {}
                payload = ("(frame too large to redact — not kept)" if frame.get("tooLarge")
                           else frame.get("payloadData") or params.get("errorMessage") or "")
                direction = {"Network.webSocketFrameSent": "sent", "Network.webSocketFrameReceived": "received"}.get(method, "error")
                req.setdefault("frames", []).append({"time": _clock(t), "step": current, "dir": direction,
                                                     "data": payload[:BODY_CAP], "truncated": len(payload) > BODY_CAP})
            elif method == "rec.body":
                req["_body"] = params
            if (method in ("Network.responseReceived", "Network.loadingFailed")
                    and _is_net_error(req) and error_doms < MAX_ERROR_DOMS and "dom" not in req):
                req["dom"] = dump(tab, f"err-net-{error_doms + 1:03d}.html")
                error_doms += 1
    close_step()

    # Bodies: only for API calls and documents, cut to the cap.
    requests = []
    for n, req in enumerate(network.values(), 1):
        req["n"] = n
        req.pop("_t0", None)
        body = req.pop("_body", None)
        if body is not None and req.get("type") in BODY_TYPES:
            text = body.get("body") or ""
            truncated = bool(body.get("truncated")) or len(text) > BODY_CAP
            if body.get("tooLarge"):
                req["body"] = f"({body.get('size') or '?'} characters, too large to redact — not kept)"
            elif body.get("binary"):
                req["body"] = f"(binary, {body.get('size') or '?'} bytes, not kept)"
            elif text:
                truncated = truncated or len(text.encode("utf-8")) > BODY_CAP
                text = _cut(text)
                if truncated:
                    text += (f"\n\n[... truncated at {BODY_CAP // 1024} KB; the full body was "
                             f"{body.get('size') or 'more'} characters]")
                bodies_dir.mkdir(exist_ok=True)
                fname = f"{n:03d}.{_body_ext(req.get('mimeType'), text)}"
                (bodies_dir / fname).write_text(text, encoding="utf-8")
                req["bodyFile"] = f"bodies/{fname}"
                req["bodyTruncated"] = truncated
        if isinstance(req.get("postData"), str) and len(req["postData"]) > BODY_CAP:
            req["postData"] = _cut(req["postData"]) + f"\n[... truncated at {BODY_CAP // 1024} KB]"
        requests.append(req)

    actions = [{k: v for k, v in s.items()} for s in steps]
    _write_jsonl(out / "actions.jsonl", actions)
    _write_jsonl(out / "console.jsonl", console)
    _write_jsonl(out / "network.jsonl", requests)
    (out / "rrweb.json").write_text(json.dumps(rrweb, ensure_ascii=False), encoding="utf-8")
    _write_jsonl(out / "perf.jsonl", [{"kind": "slow-frame", **f} for f in slow_frames]
                 + [{"kind": "dom-change", **d} for d in dom_changes])
    report = _report(name, start, stop_t, steps, console, requests, gaps, rrweb, dom_changes, slow_frames)
    (out / "report.md").write_text(report, encoding="utf-8")
    return out


def _cut(text: str) -> str:
    """At most BODY_CAP bytes of UTF-8, never splitting a character."""
    return text.encode("utf-8")[:BODY_CAP].decode("utf-8", "ignore")


def run_deep(fn, *args):
    """Run fn in a thread with a deep stack: a deep DOM is a deep JSON tree, and
    parsing, copying and serializing it all recurse."""
    import sys
    import threading

    box: dict = {}

    limit = sys.getrecursionlimit()

    def target() -> None:
        # The limit is process-wide; only this thread has the stack for it.
        sys.setrecursionlimit(max(limit, 20000))
        try:
            box["value"] = fn(*args)
        except BaseException as exc:  # handed to the caller below
            box["error"] = exc
        finally:
            sys.setrecursionlimit(limit)

    old = threading.stack_size(256 * 1024 * 1024)
    try:
        worker = threading.Thread(target=target, name="recording")
        worker.start()
    finally:
        threading.stack_size(old)
    worker.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _is_net_error(req: dict) -> bool:
    status = req.get("status")
    return (isinstance(status, int) and status >= 400) or bool(req.get("failed") and not req.get("canceled"))


def _short_url(url: str | None, limit: int = 140) -> str:
    url = url or "?"
    return url if len(url) <= limit else url[: limit - 1] + "…"


def _net_line(req: dict) -> str:
    status = req.get("status")
    outcome = (f"{status} {req.get('statusText') or ''}".strip() if status is not None
               else f"failed: {req.get('failed')}" if req.get("failed") else "no response")
    if req.get("failed") and status is not None:
        outcome += f", failed: {req['failed']}"
    bits = [f"{req.get('method') or '?'} {_short_url(req.get('url'))} → {outcome}"]
    if req.get("durationMs") is not None:
        bits.append(f"{req['durationMs']} ms")
    if req.get("type"):
        bits.append(req["type"])
    line = " · ".join(bits)
    links = []
    if req.get("bodyFile"):
        links.append(f"[response body]({req['bodyFile']})")
    if req.get("dom"):
        links.append(f"[DOM at that moment]({req['dom']})")
    links.append(f"network.jsonl n={req['n']}")
    return f"{line} — " + ", ".join(links)


def _console_line(c: dict) -> str:
    text = c["text"].replace("\n", " ")
    if len(text) > 300:
        text = text[:300] + "…"
    return f"{c['time']} · {c['source']}.{c['level']} · `{text.replace('`', 'ʹ')}`"


def _step_title(s: dict) -> str:
    bits = [f"Step {s['seq']} · {s.get('type')}"]
    if s.get("selector"):
        bits.append(f"`{s['selector']}`")
    if s.get("text"):
        bits.append(f"“{s['text']}”")
    if s.get("type") == "input" and s.get("value") is not None:
        bits.append(f"value “{s['value']}”")
    if s.get("key"):
        bits.append(f"key {s['key']}")
    return " ".join(bits) + f" — {s['time']}"


def _frame_line(f: dict, step_t: float | None = None) -> str:
    at = f" (+{round(f['t'] - step_t)} ms)" if step_t else ""
    line = f"{f['time']}{at} · main thread blocked: frame took {f.get('duration')} ms"
    if f.get("blocking"):
        line += f", {f['blocking']} ms blocking input"
    scripts = []
    for s in f.get("scripts") or []:
        where = s.get("url") or "an injected script (possibly the recorder itself)"
        fn = s.get("function") or s.get("invoker") or "?"
        extra = f", {s['forcedLayout']} ms forced layout" if s.get("forcedLayout") else ""
        scripts.append(f"{fn} in {_short_url(where, 100)} {s.get('duration')} ms{extra}")
    if scripts:
        line += " — " + "; ".join(scripts)
    return line


def _dom_line(d: dict, step_t: float | None = None) -> str:
    at = f" (+{round(d['t'] - step_t)} ms)" if step_t else ""
    bits = []
    if d.get("added"):
        bits.append(f"+{d['added']:,} nodes" + (f" into `{d.get('into')}`" if d.get("into") else ""))
    if d.get("removed"):
        bits.append(f"−{d['removed']:,} nodes")
    line = f"{d['time']}{at} · big DOM change: " + ", ".join(bits)
    if d.get("repeated"):
        line += " — mostly " + ", ".join(f"`{r}`" for r in d["repeated"])
    return line


def _report(name, start, stop_t, steps, console, requests, gaps, rrweb, dom_changes=(), slow_frames=()) -> str:
    title = name or start.get("title") or start.get("url") or "browser recording"
    errors_console = [c for c in console if c["level"] == "error"]
    errors_net = [r for r in requests if _is_net_error(r)]
    lines = [f"# Browser recording: {title}", ""]
    began, ended = start.get("t"), stop_t
    span = f" ({round((ended - began) / 1000)} s)" if began and ended else ""
    lines += [
        f"- Started on: {start.get('url') or '?'} — “{start.get('title') or ''}”",
        f"- Recorded: {time.strftime('%Y-%m-%d', time.localtime(began / 1000)) if began else '?'} "
        f"{_clock(began)} → {_clock(ended)}{span}",
        f"- {len(steps)} steps · {len(errors_console)} console errors · {len(errors_net)} failed requests · "
        f"{len(requests)} requests in all",
        "- Files: `actions.jsonl` (steps), `console.jsonl`, `network.jsonl` (one request per line, headers "
        "included), `perf.jsonl` (slow frames, big DOM changes), `bodies/`, `shots/` (screenshot per step), `dom/` (DOM after each step and at each "
        "error, rebuilt from rrweb; the clicked element carries `data-recorded-target`), `rrweb.json` "
        "(raw rrweb events — replay them with rrweb-player).",
        "- Secrets are redacted: password/OTP/card fields, Authorization/Cookie/Set-Cookie and token "
        "headers, token fields in bodies, JWTs. `[redacted]` and `****` mark them.",
        "- Steps are numbered; an event \"after step N\" happened after step N and before step N+1.",
        "",
    ]
    if not steps:
        lines += ["**No user steps were recorded** — the page may not have loaded the recorder.", ""]

    lines += [f"## Errors ({len(errors_console) + len(errors_net)})", ""]
    if not errors_console and not errors_net:
        lines += ["None: no console errors, no uncaught exceptions, no failed requests.", ""]
    merged = sorted([("c", c) for c in errors_console] + [("n", r) for r in errors_net], key=lambda x: x[1].get("t") or 0)
    for i, (kind, item) in enumerate(merged, 1):
        where = f"after step {item['step']}" if item["step"] else "before the first step"
        if kind == "c":
            lines.append(f"{i}. **[{where}]** {_console_line(item)}")
            for fr in item.get("stack") or []:
                lines.append(f"   - at {fr}")
            if item.get("dom"):
                lines.append(f"   - [DOM at that moment]({item['dom']}) · console.jsonl n={item['n']}")
        else:
            lines.append(f"{i}. **[{where}]** {item['time']} · {_net_line(item)}")
    lines.append("")

    if slow_frames or dom_changes:
        lines += [f"## Slowness ({len(slow_frames)} slow frames, {len(dom_changes)} big DOM changes)", "",
                  "Frames of 100 ms or more (Long Animation Frames, with the scripts that ran in them) and "
                  f"mutation batches of {BIG_DOM}+ nodes — where a click freezes the page. Details: `perf.jsonl`.", ""]
        merged = sorted([("f", f) for f in slow_frames] + [("d", d) for d in dom_changes], key=lambda x: x[1]["t"] or 0)
        for kind, item in merged:
            where = f"after step {item['step']}" if item["step"] else "before the first step"
            lines.append(f"- **[{where}]** " + (_frame_line(item) if kind == "f" else _dom_line(item)))
        lines.append("")

    if gaps:
        lines += ["## Recording notes", ""]
        for g in gaps:
            if g["kind"] == "rec.tab":
                lines.append(f"- {g['time']} (after step {g['step']}): a new tab {g.get('tabId')} was opened from "
                             f"tab {g.get('openerTabId')} ({_short_url(g.get('url'))}) and recorded too")
            elif g["kind"] == "rec.dropped":
                lines.append(f"- {g['time']} (after step {g['step']}): a {g.get('event')} "
                             f"event could not be stored and is missing — {g.get('reason')}")
            else:
                lines.append(f"- {g['time']} (after step {g['step']}): tab {g.get('tabId')} stopped being "
                             f"recorded — {g.get('reason')}")
        lines.append("")

    lines += ["## Timeline", ""]
    shown = BODY_TYPES | {"WebSocket"}

    def section(seq: int, step_t: float | None = None) -> list[str]:
        out: list[str] = []
        slow = [("f", f) for f in slow_frames if f["step"] == seq] + [("d", d) for d in dom_changes if d["step"] == seq]
        if slow:
            out.append("Slowness:")
            out += [f"- " + (_frame_line(x, step_t) if k == "f" else _dom_line(x, step_t))
                    for k, x in sorted(slow, key=lambda kx: kx[1]["t"] or 0)]
        cons = [c for c in console if c["step"] == seq]
        reqs = [r for r in requests if r["step"] == seq]
        if cons:
            out.append("Console:")
            out += [f"- {_console_line(c)}" + (f" — [DOM]({c['dom']})" if c.get("dom") else "") for c in cons]
        main = [r for r in reqs if r.get("type") in shown or _is_net_error(r)]
        rest = len(reqs) - len(main)
        if main or rest:
            out.append("Network:")
            out += [f"- {r['time']} · {_net_line(r)}" for r in main]
            if rest:
                out.append(f"- … and {rest} more (scripts, styles, images, fonts) in network.jsonl")
        return out

    before = section(0)
    if before:
        lines += ["### Before the first step", "", *before, ""]
    for s in steps:
        lines += [f"### {_step_title(s)}", ""]
        lines.append(f"URL: {_short_url(s.get('url'), 200)}" + (f" · tab {s['tabId']}" if s.get("tabId") else ""))
        links = [f"screenshot [{s['shot']}]({s['shot']})" if s.get("shot") else "no screenshot (the tab did not paint)",
                 f"DOM after this step [{s['dom']}]({s['dom']})" if s.get("dom") else "no DOM (rrweb had no snapshot yet)"]
        lines += [" · ".join(links), ""]
        body = section(s["seq"], s.get("t"))
        lines += (body or ["Nothing logged, no requests."]) + [""]
    return "\n".join(lines)

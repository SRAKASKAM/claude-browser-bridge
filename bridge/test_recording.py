"""Unit tests for `bb record` and bridge/recording.py — no browser needed.

The Chrome side is simulated: each test writes the raw websocket messages the
server would hand to recording.append(), then builds a bundle from them.

    uv run --no-project python -m unittest bridge/test_recording.py
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import importlib
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
# bb.py imports recording as a sibling module, as it does when run as a script.
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from test_bb import FakeResponse, load_bb  # noqa: E402


def load_recording():
    import recording
    return importlib.reload(recording)


# --------------------------------------------------------------------------
# Raw event builders — the lines the server would receive from the extension.

T0 = 1_762_000_000_000.0
TAB = 7


def ev(rec: str, t: float, method: str, params: dict) -> dict:
    return {"op": "rec", "rec": rec, "t": T0 + t, "tabId": TAB, "method": method, "params": params}


def shot_bytes(seq: int) -> bytes:
    return b"\xff\xd8\xff\xe0JFIF-test-shot-%03d" % seq


def shot(rec, t, seq):
    return ev(rec, t, "rec.shot", {"seq": seq, "data": base64.b64encode(shot_bytes(seq)).decode()})


def action(rec, t, seq, type_, selector, text, url, **extra):
    return ev(rec, t, "rec.action", {"seq": seq, "type": type_, "selector": selector,
                                     "text": text, "url": url, **extra})


def rr(rec, t, type_, data):
    return ev(rec, t, "rec.rrweb", {"event": {"type": type_, "data": data, "timestamp": T0 + t}})


def mutation(rec, t, texts=(), attributes=(), removes=(), adds=()):
    return rr(rec, t, 3, {"source": 0, "texts": list(texts), "attributes": list(attributes),
                          "removes": list(removes), "adds": list(adds)})


def console(rec, t, type_, text, fn=None, url=None, line=None):
    frames = [{"functionName": fn, "url": url, "lineNumber": line, "columnNumber": 4}] if fn else []
    return ev(rec, t, "Runtime.consoleAPICalled", {
        "type": type_, "args": [{"type": "string", "value": text}],
        "stackTrace": {"callFrames": frames}, "timestamp": T0 + t})


def request(rec, t, rid, url, method="GET", type_="XHR", headers=None, post_data=None):
    req = {"url": url, "method": method, "headers": headers or {}}
    if post_data is not None:
        req["postData"] = post_data
    return ev(rec, t, "Network.requestWillBeSent", {"requestId": rid, "request": req, "type": type_})


def response(rec, t, rid, url, status, type_="XHR", mime="application/json", headers=None):
    return ev(rec, t, "Network.responseReceived", {
        "requestId": rid, "type": type_,
        "response": {"url": url, "status": status, "statusText": "", "mimeType": mime,
                     "headers": headers or {"content-type": mime}}})


def finished(rec, t, rid):
    return ev(rec, t, "Network.loadingFinished", {"requestId": rid, "encodedDataLength": 100})


def body(rec, t, rid, text):
    return ev(rec, t, "rec.body", {"requestId": rid, "body": text, "base64Encoded": False, "truncated": False})


def failed(rec, t, rid, error):
    return ev(rec, t, "Network.loadingFailed", {"requestId": rid, "errorText": error, "canceled": False})


def exchange(rec, t, rid, url, status, type_="XHR", method="GET", text=None, mime="application/json"):
    out = [request(rec, t, rid, url, method, type_), response(rec, t + 50, rid, url, status, type_, mime),
           finished(rec, t + 60, rid)]
    if text is not None:
        out.append(body(rec, t + 70, rid, text))
    return out


def el(id_, tag, attrs=None, children=()):
    return {"type": 2, "tagName": tag, "attributes": attrs or {}, "childNodes": list(children), "id": id_}


def txt(id_, text):
    return {"type": 3, "textContent": text, "id": id_}


def document(id_, *children):
    return {"type": 0, "childNodes": [{"type": 1, "name": "html", "publicId": "", "systemId": "", "id": id_ + 1},
                                      *children], "id": id_}


# --------------------------------------------------------------------------
# The main scenario: three steps, errors after step 1 and step 2.

LOGIN = "https://app.example.com/login"
DASH = "https://app.example.com/dashboard"
BIG = 256 * 1024
STEP_SELECTORS = ["input#email", "button#save", "a#retry-link"]


def scenario(rec: str) -> list[dict]:
    snapshot = document(1, el(3, "html", children=[
        el(4, "head", children=[el(5, "script", children=[txt(6, "window.SCRIPT_MARKER_XYZ = 1")])]),
        el(7, "body", children=[
            el(12, "input", {"id": "email", "type": "email"}),
            el(8, "button", {"id": "save"}, [txt(9, "Save")]),
            el(10, "span", {"class": "spinner"}, [txt(11, "Loading spinner")]),
        ]),
    ]))
    events = [
        ev(rec, 0, "rec.start", {"url": LOGIN, "title": "Acme Login"}),
        rr(rec, 5, 4, {"href": LOGIN, "width": 1280, "height": 800}),
        rr(rec, 10, 2, {"node": snapshot, "initialOffset": {"left": 0, "top": 0}}),
        *exchange(rec, 50, "r_doc", LOGIN, 200, "Document", text="<html>DOC_BODY_MARKER</html>", mime="text/html"),
        *exchange(rec, 60, "r_js", "https://app.example.com/static/app.js", 200, "Script",
                  text="SCRIPT_BODY_MARKER()", mime="application/javascript"),
        *exchange(rec, 70, "r_img", "https://app.example.com/logo.png", 200, "Image", mime="image/png"),
        console(rec, 80, "log", "BOOTLOG app started"),
        # step 1
        action(rec, 1000, 1, "input", "input#email", "", LOGIN, value="bob@example.com", rrwebId=12),
        shot(rec, 1001, 1),
        console(rec, 1200, "log", "STEP1_LOG validating email"),
        console(rec, 1300, "error", "STEP1_ERROR validation exploded",
                fn="validateEmail", url="https://app.example.com/static/form.js", line=17),
        mutation(rec, 1500, texts=[{"id": 9, "value": "Saving..."}],
                 attributes=[{"id": 8, "attributes": {"disabled": "", "class": "busy"}}]),
        # step 2
        action(rec, 2000, 2, "click", "button#save", "Save", LOGIN, rrwebId=8),
        shot(rec, 2001, 2),
        rr(rec, 2002, 3, {"source": 2, "type": 2, "id": 8}),
        request(rec, 2100, "r_save", "https://app.example.com/api/save", "POST", "XHR",
                {"content-type": "application/json"}, '{"title": "hello"}'),
        response(rec, 2300, "r_save", "https://app.example.com/api/save", 500),
        finished(rec, 2310, "r_save"),
        body(rec, 2320, "r_save", '{"error": "db_down"}'),
        request(rec, 2400, "r_fail", "https://app.example.com/api/notify", "GET", "Fetch"),
        failed(rec, 2450, "r_fail", "net::ERR_CONNECTION_REFUSED"),
        mutation(rec, 2500,
                 attributes=[{"id": 8, "attributes": {"disabled": None}}],
                 removes=[{"parentId": 7, "id": 10}],
                 adds=[{"parentId": 7, "nextId": None,
                        "node": el(20, "div", {"class": "toast"}, [txt(21, "Something went wrong")])}]),
        ev(rec, 2600, "Runtime.exceptionThrown", {"exceptionDetails": {
            "text": "Uncaught", "url": "https://app.example.com/static/app.js", "lineNumber": 41,
            "exception": {"description": "TypeError: Cannot read properties of undefined (reading 'id')"},
            "stackTrace": {"callFrames": [{"functionName": "saveForm", "url": "https://app.example.com/static/app.js",
                                           "lineNumber": 41, "columnNumber": 12}]}}}),
        ev(rec, 2700, "Log.entryAdded", {"entry": {"level": "error", "source": "network",
                                                   "text": "STEP2_LOGENTRY Failed to load resource",
                                                   "url": "https://app.example.com/api/report"}}),
        ev(rec, 2710, "Log.entryAdded", {"entry": {"level": "info", "source": "other",
                                                   "text": "INFOLOG_NOT_ERROR", "url": LOGIN}}),
        console(rec, 2800, "error", "STEP2_ERROR save failed",
                fn="onSaveFailed", url="https://app.example.com/static/save.js", line=9),
        # step 3
        action(rec, 4000, 3, "click", "a#retry-link", "Retry", DASH),
        shot(rec, 4001, 3),
        request(rec, 4100, "r_big", "https://app.example.com/api/big", "GET", "Fetch"),
        response(rec, 4150, "r_big", "https://app.example.com/api/big", 200, "Fetch"),
        finished(rec, 4160, "r_big"),
        body(rec, 4170, "r_big", "A" * (300 * 1024) + "TAIL_MARKER_BIG"),
        ev(rec, 4200, "Network.webSocketFrameReceived", {"requestId": "ws1", "timestamp": 1.0,
                                                         "response": {"opcode": 1, "payloadData": "WS_FRAME_MARKER"}}),
        ev(rec, 5000, "rec.stop", {}),
    ]
    return events


# --------------------------------------------------------------------------
# Report parsing helpers. They fix only what the contract fixes: an errors
# section (a heading with "error"), then a timeline section (a heading with
# "timeline" or "steps"), and relative markdown links.

HEADING = re.compile(r"^#{1,6}\s")
ITEM_START = re.compile(r"^(#{1,6}\s|[-*+]\s|\d+[.)]\s|\|)")
LINK = re.compile(r"!?\[[^\]]*\]\(<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\)")


def split_report(report: str) -> tuple[str, str]:
    lines = report.splitlines()
    err = next((i for i, l in enumerate(lines) if HEADING.match(l) and "error" in l.lower()), None)
    assert err is not None, "report.md has no errors heading:\n" + report[:2000]
    tl = next((i for i, l in enumerate(lines) if i > err and HEADING.match(l)
               and ("timeline" in l.lower() or "steps" in l.lower())), None)
    assert tl is not None, "report.md has no timeline/steps heading after the errors section:\n" + report[:2000]
    return "\n".join(lines[err:tl]), "\n".join(lines[tl:])


def entry(section: str, marker: str) -> str:
    """The list item / table row / sub-section holding `marker`, plus the
    nearest heading above it (errors may be grouped under a per-step heading)."""
    lines = section.splitlines()
    idx = next((i for i, l in enumerate(lines) if marker in l), None)
    assert idx is not None, f"{marker!r} not found in:\n{section[:3000]}"
    start = idx
    while start > 0 and not ITEM_START.match(lines[start]):
        start -= 1
    end = idx + 1
    while end < len(lines) and not ITEM_START.match(lines[end]):
        end += 1
    heading = next((lines[i] for i in range(start - 1, -1, -1) if HEADING.match(lines[i])), "")
    return heading + "\n" + "\n".join(lines[start:end])


def relative_links(text: str) -> list[str]:
    return [l for l in LINK.findall(text)
            if not re.match(r"^[a-z][a-z0-9+.-]*:", l, re.I) and not l.startswith("#")]


def resolve_link(bundle: pathlib.Path, link: str) -> pathlib.Path:
    return bundle / urllib.parse.unquote(link.split("#", 1)[0])


def deep_find(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        obj = list(obj.values())
    if isinstance(obj, list):
        for v in obj:
            found = deep_find(v, key)
            if found is not None:
                return found
    return None


def strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from strings(v)


def record_text(record: dict, bundle: pathlib.Path) -> str:
    """A network record as text, plus any bundle file it references (a body file)."""
    out = json.dumps(record)
    for s in strings(record):
        if s and len(s) < 300 and not s.startswith(("http:", "https:")) and (bundle / s).is_file():
            out += "\n" + (bundle / s).read_text(errors="replace")
    return out


def all_files(*dirs: pathlib.Path):
    for d in dirs:
        for p in sorted(d.rglob("*")):
            if p.is_file():
                yield p


def one_file(bundle: pathlib.Path, name: str) -> pathlib.Path:
    found = list(bundle.rglob(name))
    assert len(found) == 1, f"expected one {name} in the bundle, found {found}"
    return found[0]


def jsonl(path: pathlib.Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


# ==========================================================================


class RawRecordingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.recording = load_recording()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_raw_dir_is_under_home_recordings(self) -> None:
        self.assertEqual(self.recording.raw_dir(self.home, "abc"), self.home / "recordings" / "abc")

    def test_append_writes_one_json_line_per_message_in_order(self) -> None:
        msgs = [ev("r1", 0, "rec.start", {"url": LOGIN, "title": "T"}),
                console("r1", 10, "log", "hello one"),
                console("r1", 20, "log", "hello two")]
        for m in msgs:
            self.recording.append(self.home, m)
        lines = jsonl(self.recording.raw_dir(self.home, "r1") / "events.jsonl")
        self.assertEqual([l["method"] for l in lines], ["rec.start", "Runtime.consoleAPICalled", "Runtime.consoleAPICalled"])
        self.assertEqual([l["t"] for l in lines], [m["t"] for m in msgs])
        self.assertEqual(lines[2]["params"]["args"][0]["value"], "hello two")

    def test_shot_bytes_go_to_a_jpeg_and_the_line_only_references_it(self) -> None:
        self.recording.append(self.home, shot("r1", 5, 3))
        raw = self.recording.raw_dir(self.home, "r1")
        self.assertEqual((raw / "shots" / "3.jpg").read_bytes(), shot_bytes(3))
        line = jsonl(raw / "events.jsonl")[0]
        self.assertEqual(line["method"], "rec.shot")
        self.assertEqual(line["params"], {"seq": 3, "file": "shots/3.jpg"})


class RedactTest(unittest.TestCase):
    def setUp(self) -> None:
        self.recording = load_recording()

    def test_secret_headers_keep_their_name_but_lose_their_value(self) -> None:
        headers = {"Authorization": "Bearer hdrAUTH1", "Cookie": "sid=hdrCOOKIE2", "Set-Cookie": "a=hdrSET3",
                   "Proxy-Authorization": "Basic hdrPROXY4", "X-Api-Key": "hdrKEY5", "x-auth-token": "hdrTOK6",
                   "X-CSRF-Token": "hdrCSRF7", "Content-Type": "application/json"}
        out = self.recording.redact({"request": {"headers": headers}})["request"]["headers"]
        self.assertEqual(set(out), set(headers), "header names must stay visible")
        for name, value in headers.items():
            if name == "Content-Type":
                self.assertEqual(out[name], "application/json")
                continue
            secret = value.split("=")[-1].split()[-1]
            self.assertNotIn(secret, json.dumps(out[name]), name)

    def test_secret_json_fields_are_redacted_at_any_depth(self) -> None:
        data = {"user": {"name": "bob", "password": "pwDEEP1"},
                "tokens": [{"access_token": "atDEEP2", "refresh_token": "rtDEEP3"}],
                "app": {"client_secret": "csDEEP4", "otp": "otpDEEP5",
                        "card": {"cvv": "cvvDEEP6", "cardNumber": "4111111111111111"}}}
        out = json.dumps(self.recording.redact(data))
        for secret in ("pwDEEP1", "atDEEP2", "rtDEEP3", "csDEEP4", "otpDEEP5", "cvvDEEP6", "4111111111111111"):
            self.assertNotIn(secret, out)
        for kept in ('"password"', '"access_token"', '"client_secret"', '"bob"'):
            self.assertIn(kept, out)

    def test_non_secret_data_is_untouched_and_the_input_is_not_mutated(self) -> None:
        plain = {"error": "invalid_grant", "status": 400, "items": [1, 2.5, None, True],
                 "headers": {"content-type": "text/html"}, "message": "Save failed"}
        self.assertEqual(self.recording.redact(plain), plain)
        secret = {"headers": {"Authorization": "Bearer keepMeInInput"}}
        before = json.dumps(secret)
        self.recording.redact(secret)
        self.assertEqual(json.dumps(secret), before, "redact must return a copy")

    def test_jwt_and_bearer_tokens_are_removed_from_free_text(self) -> None:
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        out = self.recording.redact({"text": f"got {jwt} then sent Authorization: Bearer freeBEARER9 ok"})
        self.assertNotIn("SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c", out["text"])
        self.assertNotIn("eyJzdWIiOiIxMjMifQ", out["text"])
        self.assertNotIn("freeBEARER9", out["text"])
        self.assertIn("got ", out["text"])

    def test_token_query_params_are_removed_from_urls(self) -> None:
        url = ("https://api.example.com/cb?code=qCODE1&state=okstate&access_token=qAT2"
               "&api_key=qKEY3&token=qTOK4&password=qPW5")
        out = self.recording.redact({"url": url})["url"]
        for secret in ("qCODE1", "qAT2", "qKEY3", "qTOK4", "qPW5"):
            self.assertNotIn(secret, out)
        self.assertIn("state=okstate", out)
        self.assertTrue(out.startswith("https://api.example.com/cb?"))


SECRETS = [
    "tokAUTHHEADER111", "COOKIEVAL222", "SETCOOKIE333", "PROXYAUTH444", "APIKEY555", "AUTHTOK666", "CSRF777",
    "EXTRACOOKIE888", "EXTRAAUTH999", "EXTRASETCOOKIE000",
    "PWJSON123", "CLSEC456", "CVVSECRET789", "4111111111111111", "OTP000111",
    "ACCESSTOK_RESP", "REFRESHTOK_RESP",
    "PWFORM321", "CLSECFORM654", "4012888888881881",
    "eyJzdWIiOiIxMjMifQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    "CONSOLEBEARER42", "EXCBEARER43",
    "eyJyb2xlIjoiYWRtaW4ifQ.RRWEBSIGxyz123abc", "RRWEBBEARER77", "RRWEBINPUTBEARER78",
    "OAUTHCODE55", "URLTOK66", "URLKEY77", "URLTOKEN88", "URLPW99",
]


def secret_scenario(rec: str) -> list[dict]:
    jwt1 = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    jwt2 = "eyJhbGciOiJSUzI1NiJ9.eyJyb2xlIjoiYWRtaW4ifQ.RRWEBSIGxyz123abc"
    cb = ("https://app.example.com/oauth/cb?code=OAUTHCODE55&state=okstate&access_token=URLTOK66"
          "&api_key=URLKEY77&token=URLTOKEN88&password=URLPW99")
    snapshot = document(1, el(3, "html", children=[el(7, "body", children=[
        el(8, "p", children=[txt(9, f"Your key: Bearer RRWEBBEARER77 and {jwt2}")]),
        el(10, "input", {"id": "q"}),
    ])]))
    login_body = json.dumps({"username": "bob", "password": "PWJSON123", "otp": "OTP000111",
                             "nested": {"client_secret": "CLSEC456",
                                        "card": {"cvv": "CVVSECRET789", "cardNumber": "4111111111111111"}}})
    return [
        ev(rec, 0, "rec.start", {"url": cb, "title": "Secrets"}),
        rr(rec, 10, 2, {"node": snapshot, "initialOffset": {"left": 0, "top": 0}}),
        action(rec, 100, 1, "click", "button#login", "Log in", cb),
        shot(rec, 101, 1),
        rr(rec, 150, 3, {"source": 5, "id": 10, "text": "Bearer RRWEBINPUTBEARER78", "isChecked": False}),
        request(rec, 200, "r_login", "https://app.example.com/api/login", "POST", "XHR", {
            "Authorization": "Bearer tokAUTHHEADER111", "Cookie": "sid=COOKIEVAL222",
            "Proxy-Authorization": "Basic PROXYAUTH444", "x-api-key": "APIKEY555",
            "X-Auth-Token": "AUTHTOK666", "x-csrf-token": "CSRF777", "Content-Type": "application/json"},
            login_body),
        ev(rec, 201, "Network.requestWillBeSentExtraInfo", {"requestId": "r_login", "headers": {
            "cookie": "sid=EXTRACOOKIE888", "authorization": "Bearer EXTRAAUTH999"}}),
        response(rec, 300, "r_login", "https://app.example.com/api/login", 200,
                 headers={"Set-Cookie": "sid=SETCOOKIE333; HttpOnly", "content-type": "application/json"}),
        ev(rec, 301, "Network.responseReceivedExtraInfo", {"requestId": "r_login", "headers": {
            "set-cookie": "a=EXTRASETCOOKIE000; Path=/"}}),
        finished(rec, 310, "r_login"),
        body(rec, 320, "r_login", json.dumps({"access_token": "ACCESSTOK_RESP", "refresh_token": "REFRESHTOK_RESP",
                                              "token_type": "bearer", "expires_in": 3600})),
        request(rec, 400, "r_token", "https://app.example.com/oauth/token", "POST", "Fetch",
                {"content-type": "application/x-www-form-urlencoded"},
                "grant_type=password&username=bob&password=PWFORM321&client_secret=CLSECFORM654"
                "&card_number=4012888888881881"),
        response(rec, 450, "r_token", "https://app.example.com/oauth/token", 400, "Fetch"),
        finished(rec, 460, "r_token"),
        body(rec, 470, "r_token", json.dumps({"error": "invalid_grant", "error_description": "Bad creds"})),
        *exchange(rec, 500, "r_cb", cb, 302, "Document", text="<html>redirecting</html>", mime="text/html"),
        console(rec, 600, "error", f"auth failed for {jwt1}; header was Bearer CONSOLEBEARER42",
                fn="auth", url="https://app.example.com/a.js", line=1),
        ev(rec, 700, "Runtime.exceptionThrown", {"exceptionDetails": {
            "text": "Uncaught", "url": "https://app.example.com/a.js", "lineNumber": 3,
            "exception": {"description": "Error: rejected Bearer EXCBEARER43"}, "stackTrace": {"callFrames": []}}}),
        ev(rec, 800, "rec.stop", {}),
    ]


class SecretsOnDiskTest(unittest.TestCase):
    """Condition 5: nothing secret on disk, raw or bundled; names and plain data survive."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.recording = load_recording()
        for m in secret_scenario("sec"):
            self.recording.append(self.home, m)
        self.raw = self.recording.raw_dir(self.home, "sec")
        self.bundle = self.recording.build_bundle(self.raw, self.home / "out" / "bundle")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_no_secret_reaches_the_raw_dir_or_the_bundle(self) -> None:
        files = list(all_files(self.raw, self.bundle))
        self.assertTrue(any(p.name == "events.jsonl" for p in files))
        self.assertTrue(any(p.name == "report.md" for p in files))
        leaks = [(secret, str(p)) for p in files for secret in SECRETS if secret.encode() in p.read_bytes()]
        self.assertEqual(leaks, [])

    def test_the_bundle_still_shows_what_was_sent(self) -> None:
        net = "\n".join(record_text(r, self.bundle) for r in jsonl(one_file(self.bundle, "network.jsonl")))
        low = net.lower()
        for name in ("authorization", "cookie", "set-cookie", "x-api-key", "x-csrf-token", "password", "client_secret"):
            self.assertIn(name, low)
        for plain in ("invalid_grant", "Bad creds", "application/json", "okstate", "bob", "grant_type"):
            self.assertIn(plain, net)


class BundleTest(unittest.TestCase):
    """Conditions 2, 3, 4, 6 — build_bundle on the three-step scenario."""

    @classmethod
    def setUpClass(cls) -> None:
        # Built once; a failure is re-raised in every test so each one reports it.
        cls.tmp = tempfile.TemporaryDirectory()
        cls.home = pathlib.Path(cls.tmp.name)
        cls.setup_error = None
        try:
            cls.recording = load_recording()
            for m in scenario("main"):
                cls.recording.append(cls.home, m)
            cls.raw = cls.recording.raw_dir(cls.home, "main")
            cls.out_dir = cls.home / "deep" / "not" / "yet" / "bundle"
            cls.bundle = cls.recording.build_bundle(cls.raw, cls.out_dir, "save fails")
            cls.report = (cls.bundle / "report.md").read_text()
            cls.errors, cls.timeline = split_report(cls.report)
        except BaseException as exc:  # noqa: BLE001
            cls.setup_error = exc

    def setUp(self) -> None:
        if self.setup_error is not None:
            raise self.setup_error

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def step_blocks(self) -> list[str]:
        pos = [self.timeline.find(s) for s in STEP_SELECTORS]
        self.assertTrue(all(p >= 0 for p in pos), f"every step selector must be in the timeline: {pos}")
        self.assertEqual(pos, sorted(pos), "steps must be in time order")
        return [self.timeline[pos[i]:(pos[i + 1] if i + 1 < len(pos) else None)] for i in range(len(pos))]

    def linked(self, text: str, pattern: str) -> list[pathlib.Path]:
        return [resolve_link(self.bundle, l) for l in relative_links(text) if re.search(pattern, l)]

    def network(self) -> dict[str, dict]:
        records = jsonl(one_file(self.bundle, "network.jsonl"))
        by_id: dict[str, dict] = {}
        for r in records:
            rid = deep_find(r, "requestId")
            self.assertNotIn(rid, by_id, f"one record per requestId, {rid} repeated")
            by_id[rid] = r
        return by_id

    # --- layout

    def test_build_bundle_creates_and_returns_the_out_dir(self) -> None:
        self.assertEqual(pathlib.Path(self.bundle), self.out_dir)
        for name in ("report.md", "network.jsonl", "console.jsonl", "rrweb.json"):
            self.assertEqual(len(list(self.out_dir.rglob(name))), 1, name)

    # --- condition 2: errors first

    def test_every_error_is_in_the_errors_section_before_the_timeline(self) -> None:
        for marker in ("STEP1_ERROR", "STEP2_ERROR", "Cannot read properties of undefined",
                       "STEP2_LOGENTRY", "/api/save", "ERR_CONNECTION_REFUSED"):
            self.assertIn(marker, self.errors)
        self.assertIn("500", entry(self.errors, "/api/save"))

    def test_non_errors_stay_out_of_the_errors_section(self) -> None:
        for marker in ("BOOTLOG", "STEP1_LOG", "INFOLOG_NOT_ERROR", "/api/big", "logo.png"):
            self.assertNotIn(marker, self.errors)

    def test_each_error_names_the_step_it_happened_after(self) -> None:
        def after(marker, n, selector):
            text = entry(self.errors, marker)
            self.assertTrue(re.search(rf"step\s*#?\s*0*{n}\b", text, re.I) or selector in text,
                            f"{marker} should point at step {n}:\n{text}")
        after("STEP1_ERROR", 1, "input#email")
        for marker in ("STEP2_ERROR", "Cannot read properties of undefined", "STEP2_LOGENTRY",
                       "/api/save", "ERR_CONNECTION_REFUSED"):
            after(marker, 2, "button#save")

    def test_errors_show_the_stack_function_and_url_line(self) -> None:
        exc = entry(self.errors, "Cannot read properties of undefined")
        self.assertIn("saveForm", exc)
        self.assertRegex(exc, r"app\.js:(41|42)\b")
        err = entry(self.errors, "STEP2_ERROR")
        self.assertIn("onSaveFailed", err)
        self.assertRegex(err, r"save\.js:(9|10)\b")

    # --- condition 2: timeline

    def test_timeline_lists_every_step_in_order_with_its_details(self) -> None:
        b1, b2, b3 = self.step_blocks()
        self.assertRegex(b1, r"\binput\b")
        self.assertRegex(b2, r"\bclick\b")
        self.assertIn("Save", b2)
        self.assertIn("Retry", b3)
        self.assertIn(LOGIN, b1)
        self.assertIn("/dashboard", b3)

    def test_timeline_puts_console_and_network_under_the_step_they_followed(self) -> None:
        b1, b2, b3 = self.step_blocks()
        self.assertIn("STEP1_LOG", b1)
        self.assertIn("STEP1_ERROR", b1)
        self.assertNotIn("/api/save", b1)
        for marker in ("/api/save", "/api/notify", "STEP2_ERROR"):
            self.assertIn(marker, b2)
        self.assertNotIn("STEP1_LOG", b2)
        self.assertNotIn("/api/big", b2)
        self.assertIn("/api/big", b3)

    def test_each_step_links_its_own_screenshot(self) -> None:
        for seq, block in enumerate(self.step_blocks(), start=1):
            shots = self.linked(block, r"\.jpe?g$")
            self.assertTrue(shots, f"step {seq} has no screenshot link")
            self.assertEqual(shots[0].read_bytes(), shot_bytes(seq), f"step {seq} links the wrong screenshot")

    def test_every_relative_link_in_the_report_points_to_a_bundle_file(self) -> None:
        links = relative_links(self.report)
        self.assertGreaterEqual(len(links), 6)
        root = self.bundle.resolve()
        for link in links:
            path = resolve_link(self.bundle, link).resolve()
            self.assertTrue(path.is_file(), f"broken link {link}")
            self.assertTrue(path.is_relative_to(root), f"link leaves the bundle: {link}")

    # --- condition 3: DOM snapshots

    def step_doms(self) -> list[str]:
        doms = []
        for seq, block in enumerate(self.step_blocks(), start=1):
            found = self.linked(block, r"(^|/)dom/[^/]+\.html$")
            self.assertTrue(found, f"step {seq} has no dom/<NNN>.html link")
            doms.append(found[0].read_text())
        return doms

    def test_dom_of_a_step_shows_the_page_just_before_the_next_step(self) -> None:
        d1, d2, d3 = self.step_doms()
        self.assertIn("Saving...", d1)                      # text mutation applied
        self.assertRegex(d1, r"<button[^>]*\bdisabled\b")   # attribute mutation applied
        self.assertRegex(d1, r"<button[^>]*busy")
        self.assertIn("Loading spinner", d1)
        self.assertNotIn("Something went wrong", d1)
        self.assertIn("Something went wrong", d2)           # node added after step 2
        self.assertNotIn("Loading spinner", d2)             # node removed
        self.assertNotRegex(d2, r"<button[^>]*\bdisabled\b")  # attribute set to null is gone
        self.assertIn("Something went wrong", d3)

    def test_dom_snapshots_have_no_scripts(self) -> None:
        doms = list(self.bundle.rglob("*.html"))
        self.assertTrue(doms)
        for p in doms:
            text = p.read_text()
            self.assertNotIn("SCRIPT_MARKER_XYZ", text, p.name)
            self.assertNotRegex(text.lower(), r"<script\b", p.name)

    def test_a_console_error_links_the_dom_of_its_own_moment(self) -> None:
        def dom_for(marker):
            found = [resolve_link(self.bundle, l) for l in relative_links(entry(self.errors, marker))
                     if l.endswith(".html")]
            self.assertTrue(found, f"{marker} entry links no DOM snapshot")
            return found[0].read_text()
        early = dom_for("STEP1_ERROR")
        self.assertIn("Save", early)
        self.assertNotIn("Saving...", early)
        self.assertNotIn("Something went wrong", early)
        self.assertIn("Something went wrong", dom_for("STEP2_ERROR"))

    # --- condition 4: network and console files

    def test_network_jsonl_correlates_each_request_into_one_record(self) -> None:
        net = self.network()
        for rid in ("r_doc", "r_js", "r_img", "r_save", "r_fail", "r_big"):
            self.assertIn(rid, net)
        save = net["r_save"]
        self.assertEqual(deep_find(save, "status"), 500)
        self.assertEqual(deep_find(save, "method"), "POST")
        self.assertIn("/api/save", deep_find(save, "url"))
        self.assertIn("content-type", json.dumps(save).lower())
        self.assertIn("db_down", record_text(save, self.bundle))
        self.assertIn("ERR_CONNECTION_REFUSED", json.dumps(net["r_fail"]))

    def test_bodies_are_kept_only_for_xhr_fetch_document_and_websocket(self) -> None:
        net = self.network()
        self.assertIn("DOC_BODY_MARKER", record_text(net["r_doc"], self.bundle))
        self.assertEqual(deep_find(net["r_js"], "status"), 200)
        self.assertIn("app.js", deep_find(net["r_js"], "url"))
        everything = b"".join(p.read_bytes() for p in all_files(self.bundle))
        self.assertNotIn(b"SCRIPT_BODY_MARKER", everything)
        self.assertIn(b"WS_FRAME_MARKER", everything)

    def test_a_body_over_256kb_is_cut_with_a_visible_note(self) -> None:
        text = record_text(self.network()["r_big"], self.bundle)
        run = max((len(m) for m in re.findall(r"A{1000,}", text)), default=0)
        self.assertLessEqual(run, BIG)
        self.assertGreaterEqual(run, BIG - 1024)
        self.assertRegex(text.lower(), r"truncat")
        everything = b"".join(p.read_bytes() for p in all_files(self.bundle))
        self.assertNotIn(b"TAIL_MARKER_BIG", everything)

    def test_console_jsonl_holds_the_console_messages(self) -> None:
        lines = jsonl(one_file(self.bundle, "console.jsonl"))
        text = json.dumps(lines)
        for marker in ("BOOTLOG", "STEP1_LOG", "STEP1_ERROR", "STEP2_ERROR"):
            self.assertIn(marker, text)

    # --- condition 6

    def test_rrweb_json_keeps_the_raw_rrweb_events(self) -> None:
        data = json.loads(one_file(self.bundle, "rrweb.json").read_text())
        events = data["events"] if isinstance(data, dict) else data
        rrweb_in = [m["params"]["event"] for m in scenario("main") if m["method"] == "rec.rrweb"]
        self.assertEqual(len(events), len(rrweb_in))
        self.assertEqual([e["type"] for e in events], [e["type"] for e in rrweb_in])


# ==========================================================================


def small_scenario(rec: str, title="Acme Checkout", url="https://shop.example.com/cart", stop=True) -> list[dict]:
    snapshot = document(1, el(3, "html", children=[el(7, "body", children=[el(8, "button", {"id": "pay"}, [txt(9, "Pay")])])]))
    out = [ev(rec, 0, "rec.start", {"url": url, "title": title}),
           rr(rec, 10, 2, {"node": snapshot, "initialOffset": {"left": 0, "top": 0}}),
           action(rec, 100, 1, "click", "button#pay", "Pay", url),
           shot(rec, 101, 1),
           console(rec, 200, "error", "PAY_ERROR boom", fn="pay", url="https://shop.example.com/p.js", line=3)]
    if stop:
        out.append(ev(rec, 300, "rec.stop", {}))
    return out


BUNDLE_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2}-\d{4})-([a-z0-9][a-z0-9-]*[a-z0-9])$")


class BbRecordTest(unittest.TestCase):
    """Conditions 1 and 7 — the `bb record` commands."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.base = self.root / "project"
        self.base.mkdir()
        os.environ.pop("BROWSER_BRIDGE_PROFILE", None)
        self.bb = load_bb(self.home)
        self.sent: list[dict] = []
        self.fail_ops: set[str] = set()
        self.replies: dict[str, dict] = {}

        def urlopen(req, timeout=None):
            payload = json.loads(req.data)
            self.sent.append(payload)
            if payload["op"] in self.fail_ops:
                raise urllib.error.URLError("connection refused")
            return FakeResponse(self.replies.get(payload["op"], {"ok": True, "profile": "p", "result": {}}))
        patch = mock.patch.object(self.bb.urllib.request, "urlopen", urlopen)
        patch.start()
        self.addCleanup(patch.stop)
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_bb(self, *argv: str) -> tuple[int, dict]:
        buf = io.StringIO()
        code: int = 0
        exit_text = ""
        with mock.patch.object(sys, "argv", ["bb.py", *argv]), contextlib.redirect_stdout(buf):
            try:
                self.bb.main()
            except SystemExit as exc:
                if isinstance(exc.code, str):
                    code, exit_text = 1, exc.code
                else:
                    code = exc.code or 0
        text = buf.getvalue().strip() or exit_text.strip()
        try:
            out = json.loads(text) if text else {}
        except json.JSONDecodeError:
            out = {"_unparsed": text}
        return code, out

    def sent_ops(self, op: str) -> list[dict]:
        return [m for m in self.sent if m["op"] == op]

    def start(self, *extra: str, profile="p") -> str:
        code, out = self.run_bb("record", "start", "--out", str(self.base), "--profile", profile, *extra)
        self.assertEqual(code, 0, out)
        return self.sent_ops("recordStart")[-1]["args"]["rec"]

    def simulate(self, rec: str, **kw) -> None:
        recording = load_recording()
        for m in small_scenario(rec, **kw):
            recording.append(self.home, m)

    def stop(self, *extra: str, profile="p") -> tuple[int, dict]:
        return self.run_bb("record", "stop", "--profile", profile, *extra)

    def bundle_name(self, out: dict) -> re.Match:
        path = pathlib.Path(out["path"])
        m = BUNDLE_NAME.match(path.name)
        self.assertIsNotNone(m, f"bundle dir name {path.name!r} is not <YYYY-MM-DD-HHMM>-<slug>")
        return m

    # --- start

    def test_start_does_not_send_the_agents_pinned_tab(self) -> None:
        self.bb.PINS_DIR.mkdir(parents=True, exist_ok=True)
        self.bb.pin_file("p").write_text("42")
        rec = self.start()
        args = self.sent_ops("recordStart")[0]["args"]
        self.assertNotIn("tabId", args)
        self.assertIsInstance(rec, str)
        self.assertTrue(rec)

    def test_start_passes_the_tab_flag(self) -> None:
        self.start("--tab", "9")
        self.assertEqual(self.sent_ops("recordStart")[0]["args"]["tabId"], 9)

    def test_a_second_start_is_refused_while_one_is_active(self) -> None:
        self.start()
        code, out = self.run_bb("record", "start", "--out", str(self.base), "--profile", "p")
        self.assertEqual(code, 1)
        self.assertIs(out.get("ok"), False, out)
        self.assertEqual(len(self.sent_ops("recordStart")), 1)

    def test_a_start_the_extension_rejects_leaves_nothing_active(self) -> None:
        self.replies["recordStart"] = {"ok": False, "error": "no active tab"}
        code, _ = self.run_bb("record", "start", "--out", str(self.base), "--profile", "p")
        self.assertEqual(code, 1)
        code, out = self.run_bb("record", "status", "--profile", "p")
        self.assertIs(out.get("active"), False, out)

    def test_status_says_whether_a_recording_is_active(self) -> None:
        _, out = self.run_bb("record", "status", "--profile", "p")
        self.assertIs(out.get("active"), False, out)
        self.start()
        _, out = self.run_bb("record", "status", "--profile", "p")
        self.assertIs(out.get("active"), True, out)

    # --- stop

    def test_stop_without_a_recording_is_refused(self) -> None:
        code, out = self.stop()
        self.assertEqual(code, 1, out)
        self.assertIs(out.get("ok"), False, out)
        self.assertEqual(self.sent_ops("recordStop"), [])

    def test_stop_writes_the_bundle_under_out_base_named_by_the_flag(self) -> None:
        rec = self.start()
        self.simulate(rec)
        code, out = self.stop("--name", "Login Fails 500 on API!")
        self.assertEqual(code, 0, out)
        self.assertIs(out.get("ok"), True, out)
        self.assertEqual(self.sent_ops("recordStop")[0]["args"]["rec"], rec)
        path = pathlib.Path(out["path"])
        self.assertEqual(path.parent, self.base / ".recordings")
        m = self.bundle_name(out)
        self.assertEqual(m.group(2), "login-fails-500-on-api")
        stamp = datetime.datetime.strptime(m.group(1), "%Y-%m-%d-%H%M")
        self.assertLess(abs((datetime.datetime.now() - stamp).total_seconds()), 180)
        self.assertEqual(pathlib.Path(out["report"]), path / "report.md")
        self.assertIn("PAY_ERROR", (path / "report.md").read_text())

    def test_stop_clears_the_state(self) -> None:
        self.simulate(self.start())
        self.assertEqual(self.stop()[0], 0)
        _, out = self.run_bb("record", "status", "--profile", "p")
        self.assertIs(out.get("active"), False, out)
        self.assertEqual(self.stop()[0], 1)

    def test_without_a_name_the_slug_comes_from_the_page(self) -> None:
        self.simulate(self.start(), title="Checkout Page", url="https://shop.example.com/cart")
        code, out = self.stop()
        self.assertEqual(code, 0, out)
        slug = self.bundle_name(out).group(2)
        self.assertTrue("checkout" in slug or "shop-example-com" in slug, slug)

    def test_slug_is_capped_at_40_chars_and_trimmed(self) -> None:
        self.simulate(self.start())
        _, out = self.stop("--name", "  ** " + "Very Long Bug Name " * 6 + "!!")
        slug = self.bundle_name(out).group(2)
        self.assertLessEqual(len(slug), 40)
        self.assertTrue(slug.startswith("very-long-bug-name-very"), slug)

    def test_out_base_defaults_to_the_cwd_at_start(self) -> None:
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        os.chdir(self.base)
        code, out = self.run_bb("record", "start", "--profile", "p")
        self.assertEqual(code, 0, out)
        self.simulate(self.sent_ops("recordStart")[0]["args"]["rec"])
        os.chdir(elsewhere)
        code, out = self.stop("--name", "cwd")
        self.assertEqual(code, 0, out)
        self.assertEqual(pathlib.Path(out["path"]).resolve().parent, (self.base / ".recordings").resolve())
        self.assertFalse((elsewhere / ".recordings").exists())

    def test_a_recording_belongs_to_its_profile(self) -> None:
        self.start(profile="p")
        code, _ = self.stop(profile="q")
        self.assertEqual(code, 1)
        _, out = self.run_bb("record", "status", "--profile", "p")
        self.assertIs(out.get("active"), True, out)

    def test_git_exclude_gets_recordings_once_and_gitignore_is_untouched(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.base)], check=True)
        exclude = self.base / ".git" / "info" / "exclude"
        self.simulate(self.start())
        self.assertEqual(self.stop("--name", "first")[0], 0)
        sub = self.base / "web"
        sub.mkdir()
        code, out = self.run_bb("record", "start", "--out", str(sub), "--profile", "p")
        self.assertEqual(code, 0, out)
        self.simulate(self.sent_ops("recordStart")[-1]["args"]["rec"])
        self.assertEqual(self.stop("--name", "second")[0], 0)
        lines = [l.strip() for l in exclude.read_text().splitlines()]
        self.assertEqual(lines.count(".recordings/"), 1, exclude.read_text())
        self.assertFalse((self.base / ".gitignore").exists())
        self.assertFalse((sub / ".gitignore").exists())

    def test_stop_builds_the_bundle_even_if_the_extension_is_unreachable(self) -> None:
        self.simulate(self.start(), stop=False)
        self.fail_ops.add("recordStop")
        code, out = self.stop("--name", "offline")
        self.assertEqual(code, 0, out)
        self.assertIs(out.get("ok"), True, out)
        self.assertTrue(out.get("warning"), out)
        self.assertTrue(pathlib.Path(out["report"]).is_file())
        self.assertIn("PAY_ERROR", pathlib.Path(out["report"]).read_text())

    def test_stop_without_a_profile_finds_this_sessions_recording(self) -> None:
        rec = self.start(profile="work")
        self.simulate(rec)
        code, out = self.run_bb("record", "stop", "--name", "later")
        self.assertEqual(code, 0, out)
        self.assertTrue(pathlib.Path(out["report"]).is_file())

class ReviewRedactionTest(unittest.TestCase):
    """Request shapes the first review found leaking to disk."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.recording = load_recording()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def leaks(self, messages: list[dict], secrets: list[str]) -> list[tuple[str, str]]:
        for m in messages:
            self.recording.append(self.home, m)
        raw = self.recording.raw_dir(self.home, "rv")
        bundle = self.recording.build_bundle(raw, self.home / "out")
        files = list(all_files(raw, bundle))
        return [(s, str(f)) for f in files for s in secrets
                if s.encode() in f.read_bytes() or base64.b64encode(s.encode()) in f.read_bytes()]

    def test_post_data_entries_never_reach_the_disk(self) -> None:
        post = json.dumps({"email": "bob@x.com", "password": "ENTRYPASS1"})
        req = request("rv", 10, "r1", "https://api.example.com/login", "POST", post_data=post)
        req["params"]["request"]["postDataEntries"] = [{"bytes": base64.b64encode(post.encode()).decode()}]
        self.assertEqual(self.leaks([req], ["ENTRYPASS1"]), [])

    def test_multipart_form_fields_are_redacted(self) -> None:
        post = ('------b\r\nContent-Disposition: form-data; name="email"\r\n\r\nbob@x.com\r\n'
                '------b\r\nContent-Disposition: form-data; name="password"\r\n\r\nMULTIPASS1\r\n------b--\r\n')
        msgs = [request("rv", 10, "r1", "https://api.example.com/login", "POST", post_data=post)]
        self.assertEqual(self.leaks(msgs, ["MULTIPASS1"]), [])
        net = (self.home / "out" / "network.jsonl").read_text()
        self.assertIn("bob@x.com", net)

    def test_json_cut_at_the_cap_still_loses_its_secret_fields(self) -> None:
        text = '{"accessToken": "OPAQUECUT1", "error": "nope", "items": [1, 2, 3'
        msgs = exchange("rv", 10, "r1", "https://api.example.com/me", 200, text=text)
        self.assertEqual(self.leaks(msgs, ["OPAQUECUT1"]), [])

    def test_html_documents_lose_hidden_inputs_csrf_metas_and_inline_state(self) -> None:
        page = ('<html><head><meta name="csrf-token" content="METACSRF1"></head><body>'
                '<form><input type="hidden" name="authenticity" value="HIDDENVAL1"><input name="q" value="shoes"></form>'
                '<script>window.__STATE__ = {"apiKey": "INLINEKEY1", "user": "bob"}</script></body></html>')
        msgs = exchange("rv", 10, "r1", "https://app.example.com/", 200, type_="Document", text=page, mime="text/html")
        self.assertEqual(self.leaks(msgs, ["METACSRF1", "HIDDENVAL1", "INLINEKEY1"]), [])

    def test_events_holding_unicode_line_separators_survive(self) -> None:
        msgs = [console("rv", 10, "error", "broken\u2028line ERRSEP1")]
        self.leaks(msgs, [])
        self.assertIn("ERRSEP1", (self.home / "out" / "console.jsonl").read_text())

    # Shapes the second review found: redact before cutting, safe snapshots.

    def test_a_big_body_is_redacted_whole_before_it_is_cut(self) -> None:
        text = json.dumps({"auth": {"value": "NESTEDBIG1"}, "items": ["x" * 100] * 3000,
                           "refreshToken": "TAILBIG1"})
        self.assertGreater(len(text), 256 * 1024)
        msgs = exchange("rv", 10, "r1", "https://api.example.com/list", 200, text=text)
        self.assertEqual(self.leaks(msgs, ["NESTEDBIG1", "TAILBIG1"]), [])

    def test_a_body_too_large_to_redact_is_not_kept(self) -> None:
        msgs = exchange("rv", 10, "r1", "https://api.example.com/dump", 200)
        msgs.append(ev("rv", 80, "rec.body", {"requestId": "r1", "body": "", "base64Encoded": False,
                                             "tooLarge": True, "size": 9_000_000}))
        self.leaks(msgs, [])
        rows = jsonl(self.home / "out" / "network.jsonl")
        self.assertIn("too large", rows[0].get("body", ""))
        self.assertNotIn("bodyFile", rows[0])

    def test_html_field_detection_is_not_fooled_by_data_attributes(self) -> None:
        page = '<html><body><input data-type="x" type="hidden" name="state" value="DATATYPE1"></body></html>'
        msgs = exchange("rv", 10, "r1", "https://app.example.com/", 200, type_="Document", text=page, mime="text/html")
        self.assertEqual(self.leaks(msgs, ["DATATYPE1"]), [])

    def test_dom_snapshots_cannot_run_or_redirect(self) -> None:
        doc = document(1, el(2, "html", {}, [
            el(3, "head", {}, [el(4, "base", {"href": "https://evil.example/"}),
                               el(5, "meta", {"http-equiv": "refresh", "content": "0;url=https://evil.example/"})]),
            el(6, "body", {}, [el(7, "img", {"src": "x.png", "onerror": "alert(1)"}),
                               el(8, "a", {"href": "javascript:alert(2)"}, [txt(9, "link")])])]))
        msgs = [ev("rv", 0, "rec.start", {"url": "https://app.example.com/", "title": "App"}),
                rr("rv", 1, 2, {"node": doc}),
                action("rv", 10, 1, "click", "#x", "x", "https://app.example.com/")]
        self.leaks(msgs, [])
        page = (self.home / "out" / "dom" / "001.html").read_text()
        for bad in ("onerror", "javascript:", "<base", "refresh"):
            self.assertNotIn(bad, page)
        self.assertIn("script-src 'none'", page)

    # Shapes the third review found.
    def test_any_secret_named_url_param_is_redacted(self) -> None:
        url = ("https://bucket.s3.amazonaws.com/f?X-Amz-Security-Token=AMZTOK1&X-Amz-Signature=AMZSIG1"
               "&X-Amz-Credential=AMZCRED1&auth_token=AUTHTOK1&session_token=SESSTOK1&access_key=ACCKEY1"
               "&jwt=JWTVAL1&X-Goog-Signature=GOOGSIG1&page=2")
        secrets = ["AMZTOK1", "AMZSIG1", "AMZCRED1", "AUTHTOK1", "SESSTOK1", "ACCKEY1", "JWTVAL1", "GOOGSIG1"]
        self.assertEqual(self.leaks(exchange("rv", 10, "r1", url, 200, text="{}"), secrets), [])
        self.assertIn("page=2", (self.home / "out" / "network.jsonl").read_text())

    def test_json_inside_strings_and_socket_frames_is_redacted(self) -> None:
        post = json.dumps({"query": "mutation Login", "variables": json.dumps({"password": "GQLPASS1"})})
        frame = ev("rv", 30, "Network.webSocketFrameSent",
                   {"requestId": "ws1", "response": {"opcode": 1, "payloadData": '42["auth",{"token":"SIOTOK1"}]'}})
        msgs = [request("rv", 10, "r1", "https://api.example.com/graphql", "POST", post_data=post),
                ev("rv", 20, "Network.webSocketCreated", {"requestId": "ws1", "url": "wss://api.example.com/socket"}),
                frame]
        self.assertEqual(self.leaks(msgs, ["GQLPASS1", "SIOTOK1"]), [])

    def test_cors_headers_survive(self) -> None:
        msgs = [request("rv", 10, "r1", "https://api.example.com/me"),
                response("rv", 20, "r1", "https://api.example.com/me", 200,
                         headers={"Access-Control-Allow-Credentials": "true",
                                  "Access-Control-Allow-Origin": "https://app.example.com"})]
        self.leaks(msgs, [])
        net = (self.home / "out" / "network.jsonl").read_text()
        self.assertIn('"Access-Control-Allow-Credentials": "true"', net)

    def test_a_deep_dom_builds_through_run_deep(self) -> None:
        node = txt(10_000, "bottom")
        for depth in range(600):
            node = el(20_000 + depth, "div", {}, [node])
        msgs = [ev("rv", 0, "rec.start", {"url": "https://app.example.com/", "title": "Deep"}),
                rr("rv", 1, 2, {"node": document(1, el(2, "html", {}, [el(3, "body", {}, [node])]))}),
                action("rv", 10, 1, "click", "#x", "x", "https://app.example.com/")]
        old = sys.getrecursionlimit()
        self.recording.run_deep(lambda: [self.recording.append(self.home, m) for m in msgs])
        raw = self.recording.raw_dir(self.home, "rv")
        self.recording.run_deep(self.recording.build_bundle, raw, self.home / "out")
        self.assertIn("bottom", (self.home / "out" / "dom" / "001.html").read_text())
        self.assertEqual(sys.getrecursionlimit(), old)

class SlownessTest(unittest.TestCase):
    """A click that freezes the page: big DOM batches and long frames reach the report."""

    def test_big_dom_changes_and_slow_frames_are_reported_under_their_step(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = pathlib.Path(tmp.name)
        recording = load_recording()
        url = "https://app.example.com/"
        rows = [el(100 + i, "div", {"class": "row item"}, [txt(5000 + i, f"chat {i}")]) for i in range(800)]
        msgs = [ev("sl", 0, "rec.start", {"url": url, "title": "App"}),
                rr("sl", 1, 2, {"node": document(1, el(2, "html", {}, [el(3, "body", {}, [el(4, "ul", {"class": "list"})])]))}),
                action("sl", 100, 1, "click", "#reports", "Reports", url),
                mutation("sl", 900, adds=[{"parentId": 4, "nextId": None, "node": n} for n in rows]),
                ev("sl", 950, "rec.perf", {"t": T0 + 120, "duration": 1800, "blocking": 1700,
                                          "scripts": [{"duration": 1650, "invoker": "Response.json.then",
                                                       "function": "renderList", "url": "https://app.example.com/app.js"}]}),
                ev("sl", 2000, "rec.stop", {})]
        for m in msgs:
            recording.append(home, m)
        bundle = recording.build_bundle(recording.raw_dir(home, "sl"), home / "out")
        report = (bundle / "report.md").read_text()
        timeline = report.split("## Timeline")[1]
        step1 = timeline.split("### Step 1")[1]
        self.assertIn("1,600 nodes", step1)  # 800 rows with a text node each
        self.assertIn("div.row.item ×800", step1)
        self.assertIn("1800 ms", step1)
        self.assertIn("renderList", step1)
        self.assertLess(report.index("## Slowness"), report.index("## Timeline"))
        kinds = [json.loads(l)["kind"] for l in (bundle / "perf.jsonl").read_text().splitlines()]
        self.assertEqual(sorted(kinds), ["dom-change", "slow-frame"])


if __name__ == "__main__":
    unittest.main()

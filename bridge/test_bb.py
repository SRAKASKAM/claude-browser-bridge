"""Unit tests for bb.py's routing and `bb ask` logic — no browser needed.

    uv run --no-project python -m unittest bridge/test_bb.py
"""

from __future__ import annotations

import importlib
import json
import os
import pathlib
import shlex
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent


def load_bb(home: pathlib.Path):
    os.environ.update(BROWSER_BRIDGE_HOME=str(home), BROWSER_BRIDGE_TOKEN="t", BROWSER_BRIDGE_SESSION="s1")
    sys.path.insert(0, str(HERE))
    try:
        import bb
        return importlib.reload(bb)
    finally:
        sys.path.remove(str(HERE))


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class BbTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.bb = load_bb(self.home)
        self.sent: list[dict] = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def fake_urlopen(self, reply: dict):
        def urlopen(req, timeout=None):
            self.sent.append(json.loads(req.data))
            return FakeResponse(reply)
        return mock.patch.object(self.bb.urllib.request, "urlopen", urlopen)

    def pin(self, tab: int) -> None:
        self.bb.PINS_DIR.mkdir(parents=True, exist_ok=True)
        self.bb.pin_file("p").write_text(str(tab))

    def test_page_ops_refuse_without_a_pin(self) -> None:
        for op in ("attention", "clearAttention", "closeTab", "reveal"):
            with self.subTest(op=op), self.assertRaises(SystemExit) as cm:
                self.bb.post(op, {}, "p")
            self.assertIn("needs a tab of your own", str(cm.exception.code))

    def test_pinned_tab_goes_to_tab_ops_but_not_window_ops(self) -> None:
        self.pin(42)
        with self.fake_urlopen({"ok": True, "result": {}}):
            for op in ("reveal", "closeWindow", "attention", "closeTab"):
                self.bb.post(op, {}, "p")
            for op in ("openTab", "newWindow", "hide"):
                self.bb.post(op, {}, "p")
        self.assertEqual([m["args"].get("tabId") for m in self.sent], [42, 42, 42, 42, None, None, None])

    def test_notify_is_false_when_the_hook_is_missing_or_fails(self) -> None:
        self.assertFalse(self.bb.notify("show", "k"))
        hook = self.bb.NOTIFY_HOOK
        hook.write_text("#!/bin/sh\nexit 3\n")
        hook.chmod(0o755)
        self.assertFalse(self.bb.notify("show", "k"))
        hook.write_text("#!/bin/sh\nexit 0\n")
        self.assertTrue(self.bb.notify("show", "k"))

    def test_ask_hands_the_hook_a_reveal_command_for_this_tab(self) -> None:
        self.pin(7)
        args_file = self.home / "args.json"
        hook = self.bb.NOTIFY_HOOK
        hook.write_text(f"#!{sys.executable}\nimport json,sys\nopen({str(args_file)!r},'w').write(json.dumps(sys.argv[1:]))\n")
        hook.chmod(0o755)
        reply = {"ok": True, "profile": "me 'x'@y", "result": {"tabId": 7, "outlined": [], "missing": []}}
        with self.fake_urlopen(reply):
            out = self.bb.ask(["line one\nline two", "#otp"], "p")
        self.assertTrue(out["notified"])
        kind, key, _title, body, command = json.loads(args_file.read_text())
        self.assertEqual((kind, key, body), ("show", "me 'x'@y:7", "line one\nline two"))
        self.assertEqual(shlex.split(command)[-4:], ["reveal", "7", "--profile", "me 'x'@y"])
        self.assertEqual(self.sent[0]["args"]["selectors"], ["#otp"])

    def test_ask_without_a_hook_tells_the_agent_to_say_it_in_chat(self) -> None:
        self.pin(7)
        with self.fake_urlopen({"ok": True, "profile": "p", "result": {"tabId": 7}}):
            out = self.bb.ask(["do it"], "p")
        self.assertFalse(out["notified"])
        self.assertIn("tell the human yourself", out["note"])

    def test_done_condition_flags_become_page_expressions(self) -> None:
        rest, js = self.bb.done_condition(["msg", "#a", "--until-gone", "input[name='otp']"])
        self.assertEqual(rest, ["msg", "#a"])
        self.assertEqual(js, "document.readyState === 'complete' && !document.querySelector(\"input[name='otp']\")")
        self.assertEqual(self.bb.done_condition(["m", "--until-url", "/home"])[1], 'location.href.includes("/home")')
        self.assertEqual(self.bb.done_condition(["m", "--until", "window.ok"])[1], "window.ok")
        self.assertIsNone(self.bb.done_condition(["m"])[1])

    def fake_post(self, done_after: int, typing_polls: int = 0, hide_result=None, clear_ok=True):
        """A post() whose done condition turns true on the n-th check (the
        pre-check counts as the first) and whose page reports typing for the
        first `typing_polls` idle checks."""
        state = {"n": 0, "typing": typing_polls}

        def post(op, args, profile, timeout=60.0):
            self.sent.append({"op": op, "args": args})
            if op == "attention":
                return {"ok": True, "profile": "p", "result": {"tabId": 7}}
            if op == "eval" and "__claudeLastKey" in args["expression"]:
                state["typing"] -= 1
                return {"ok": True, "result": {"value": state["typing"] < 0}}
            if op == "eval":
                state["n"] += 1
                return {"ok": True, "result": {"value": state["n"] >= done_after}}
            if op == "hide":
                return {"ok": True, "result": hide_result or {"hidden": True, "wasFocused": True}}
            if op == "clearAttention":
                if not clear_ok:
                    return {"ok": False, "error": "No tab with id: 7"}
                return {"ok": True, "profile": "p", "result": {"cleared": 7}}
            return {"ok": True, "result": {}}
        return mock.patch.object(self.bb, "post", post)

    def patched(self, **kw):
        self.notes: list[tuple] = []
        stack = [mock.patch.object(self.bb, "POLL_S", 0),
                 mock.patch.object(self.bb, "notify", lambda *a: self.notes.append(a) or True),
                 mock.patch.object(self.bb.time, "sleep", lambda s: None),
                 self.fake_post(**kw)]
        for p in stack:
            p.start()
            self.addCleanup(p.stop)

    def test_ask_until_waits_then_clears_and_minimizes_by_itself(self) -> None:
        self.pin(7)
        self.patched(done_after=3)
        out = self.bb.ask(["do it", "#x", "--until-gone", "#x"], "p")
        self.assertTrue(out["ok"] and out["done"])
        ops = [m["op"] for m in self.sent]
        self.assertLess(ops.index("clearAttention"), ops.index("hide"))
        self.assertEqual(next(m for m in self.sent if m["op"] == "hide")["args"], {"owner": 7})
        self.assertEqual(self.notes[-1], ("dismiss", "p:7", "return"))

    def condition_sequence(self, values):
        """page_eval for the done condition yields `values` in turn (then False);
        the typing check always reports idle."""
        it = iter(values)

        def page_eval(expression, tab, profile):
            if "__claudeLastKey" in expression:
                return True, None
            return next(it, False), None
        return mock.patch.object(self.bb, "page_eval", page_eval)

    def test_a_single_true_check_is_not_done(self) -> None:
        self.pin(7)
        self.patched(done_after=10**9)
        with self.condition_sequence([False, True, False, True, False]):
            out = self.bb.ask(["do it", "--until", "x", "--timeout", "0.05"], "p")
        self.assertFalse(out["done"])

    def test_two_true_checks_in_a_row_are_done(self) -> None:
        self.pin(7)
        self.patched(done_after=10**9)
        with self.condition_sequence([False, True, False, True, True]):
            out = self.bb.ask(["do it", "--until", "x", "--timeout", "5"], "p")
        self.assertTrue(out["done"])

    def test_condition_that_throws_is_refused_up_front(self) -> None:
        self.pin(7)
        self.patched(done_after=1)
        with mock.patch.object(self.bb, "page_eval", return_value=(None, "SyntaxError: ':has-text' is not a valid selector")):
            out = self.bb.ask(["do it", "--until", "document.querySelector('b:has-text(\"x\")')"], "p")
        self.assertFalse(out["ok"])
        self.assertIn("fails in the page", out["error"])
        self.assertNotIn("attention", [m["op"] for m in self.sent])

    def test_closed_tab_ends_the_wait_at_once(self) -> None:
        self.pin(7)
        self.patched(done_after=10**9)
        answers = iter([(False, None), (None, "gone")])
        with mock.patch.object(self.bb, "page_eval", lambda *a: next(answers)):
            out = self.bb.ask(["do it", "--until", "x"], "p")
        self.assertFalse(out["ok"])
        self.assertIn("tab is gone", out["error"])

    def test_typing_that_stops_still_minimizes(self) -> None:
        self.pin(7)
        self.patched(done_after=3, typing_polls=2)
        out = self.bb.ask(["do it", "--until", "x"], "p")
        self.assertEqual(out["finish"], {"hidden": True, "wasFocused": True})

    def test_ask_done_finishes_like_an_until_ask(self) -> None:
        self.pin(7)
        self.patched(done_after=1)
        out = self.bb.ask_done("p")
        self.assertTrue(out["ok"])
        ops = [m["op"] for m in self.sent]
        self.assertEqual(ops.count("clearAttention"), 1)
        self.assertEqual(next(m for m in self.sent if m["op"] == "hide")["args"], {"owner": 7})
        self.assertEqual(self.notes[-1], ("dismiss", "p:7", "return"))

    def test_timeout_without_a_condition_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            self.bb.ask(["do it", "--timeout", "5"], "p")

    def test_condition_already_true_before_the_human_is_refused(self) -> None:
        self.pin(7)
        self.patched(done_after=1)
        out = self.bb.ask(["do it", "#typo", "--until-gone", "#typo"], "p")
        self.assertFalse(out["ok"])
        self.assertIn("already holds", out["error"])
        self.assertNotIn("attention", [m["op"] for m in self.sent])

    def test_still_typing_keeps_the_window_up(self) -> None:
        self.pin(7)
        self.patched(done_after=3, typing_polls=10**9)
        with mock.patch.object(self.bb, "TYPING_WAIT_MAX_S", -1):
            out = self.bb.ask(["do it", "--until", "x"], "p")
        self.assertEqual(out["finish"]["reason"], "the user is still typing there")
        self.assertNotIn("hide", [m["op"] for m in self.sent])
        self.assertEqual(self.notes[-1], ("dismiss", "p:7"))

    def test_focus_is_not_handed_back_when_the_window_stayed(self) -> None:
        self.pin(7)
        self.patched(done_after=3, hide_result={"hidden": False, "reason": "the user switched to another tab there"})
        self.bb.ask(["do it", "--until", "x"], "p")
        self.assertEqual(self.notes[-1], ("dismiss", "p:7"))

    def test_ask_until_times_out_and_leaves_the_marks_up(self) -> None:
        self.pin(7)
        self.patched(done_after=10**9)
        out = self.bb.ask(["do it", "--until", "false", "--timeout", "0.05"], "p")
        self.assertFalse(out["ok"] or out["done"])
        self.assertNotIn("clearAttention", [m["op"] for m in self.sent])

    def test_bad_timeout_exits_before_marking_the_page(self) -> None:
        self.pin(7)
        self.patched(done_after=1)
        with self.assertRaises(SystemExit):
            self.bb.ask(["do it", "--until", "x", "--timeout", "10m"], "p")
        self.assertEqual(self.sent, [])

    def test_ask_done_reports_a_failed_clear(self) -> None:
        self.pin(7)
        self.patched(done_after=1, clear_ok=False)
        self.assertFalse(self.bb.ask_done("p")["ok"])
        self.assertNotIn("hide", [m["op"] for m in self.sent])

if __name__ == "__main__":
    unittest.main()

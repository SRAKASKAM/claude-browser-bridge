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


if __name__ == "__main__":
    unittest.main()

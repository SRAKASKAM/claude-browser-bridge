"""Unit tests for jev.py — no browser, no network.

    uv run --no-project python -m unittest bridge/test_jev.py
"""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import jev  # noqa: E402


def snap(n: int, extra: list[dict] | None = None) -> dict:
    els = [{"id": i, "tag": "button", "type": "", "label": f"Button {i}", "x": 1, "y": i} for i in range(n)]
    return {"url": "https://example.com", "title": "Example", "elements": els + (extra or [])}


def answers(n_chunks: int, chunk: int, choice: str, prob: float, fits: float) -> dict:
    out = {}
    for i in range(n_chunks):
        out[f"fits_{i}"] = {"noul": fits if i == chunk else 0.01}
        out[f"pick_{i}"] = {"choice": choice if i == chunk else "e0",
                            "probabilities": {choice if i == chunk else "e0": prob}, "confidence": prob}
    return out


def run(fn, *args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = fn(*args)
    return code, out.getvalue()


def acts(bb_mock) -> list[tuple]:
    """bb calls that act on the page (everything except the tag cleanup)."""
    return [c[0] for c in bb_mock.call_args_list if c[0][0] in ("click", "type")]


class Candidates(unittest.TestCase):
    def test_label_and_unique_selector(self):
        c = jev.candidates(snap(2, [{"id": 2, "tag": "input", "type": "email", "label": "Email", "x": 0, "y": 0}]))
        self.assertEqual([x["selector"] for x in c], ['[data-jev="0"]', '[data-jev="1"]', '[data-jev="2"]'])
        self.assertEqual(c[0]["label"], "<button> Button 0")
        self.assertEqual(c[2]["label"], "<input email> Email")

    def test_listing_never_reads_field_values(self):
        # `value` is only read for button-like inputs (submit/button), never for fields.
        self.assertIn("if (!label && !field) label = el.innerText || (tag === 'input' ? el.value : '')", jev.LIST_JS)
        for js in (jev.LIST_JS, jev.TEXT_JS):
            self.assertEqual(js.count(".value"), 1 if js is jev.LIST_JS else 0)
            for leak in ("textContent", "getAttribute('value')", "defaultValue", "location.href", "location.search"):
                self.assertNotIn(leak, js)


class Pick(unittest.TestCase):
    def run_pick(self, s, ans):
        with mock.patch.object(jev, "list_elements", return_value=s), \
             mock.patch.object(jev, "decide", return_value=ans) as d:
            return jev.pick("target", None), d

    def test_confident_answer_is_acted_on(self):
        (chosen, _, info), _ = self.run_pick(snap(3), answers(1, 0, "e2", 0.95, 0.9))
        self.assertEqual(chosen["selector"], '[data-jev="2"]')
        self.assertEqual(info["prob"], 0.95)

    def test_low_probability_hands_back_to_agent(self):
        (chosen, top, _), _ = self.run_pick(snap(3), answers(1, 0, "e2", 0.5, 0.9))
        self.assertIsNone(chosen)
        self.assertTrue(top)

    def test_nothing_fits_hands_back_even_if_choice_is_sure(self):
        (chosen, _, _), _ = self.run_pick(snap(3), answers(1, 0, "e2", 1.0, 0.1))
        self.assertIsNone(chosen)

    def test_long_pages_are_split_into_choices_of_at_most_250(self):
        (chosen, _, _), d = self.run_pick(snap(600), answers(3, 2, "e510", 0.99, 0.95))
        questions = d.call_args[0][1]
        self.assertEqual(sum(q.startswith("pick_") for q in questions), 3)
        self.assertTrue(all(len(q["criteria"]) <= 250 for k, q in questions.items() if k.startswith("pick_")))
        self.assertEqual(chosen["selector"], '[data-jev="510"]')

    def test_answer_missing_confidence_exits_cleanly(self):
        ans = answers(1, 0, "e1", 0.9, 0.9)
        del ans["pick_0"]["confidence"]
        with self.assertRaises(SystemExit):
            self.run_pick(snap(2), ans)


class Do(unittest.TestCase):
    def setUp(self):
        self.chosen = {"selector": "#b2", "label": "<button> Button 2", "x": 1, "y": 2}

    def test_confident_click(self):
        with mock.patch.object(jev, "pick", return_value=(self.chosen, [self.chosen], {"prob": 1})), \
             mock.patch.object(jev, "bb") as bb:
            code, out = run(jev.cmd_pick, "t", None, "click", None)
        self.assertEqual(code, 0)
        self.assertEqual(acts(bb), [("click", "#b2")])
        self.assertTrue(json.loads(out)["ok"])

    def test_confident_type_clears_first(self):
        with mock.patch.object(jev, "pick", return_value=(self.chosen, [], {})), \
             mock.patch.object(jev, "bb") as bb:
            code, _ = run(jev.cmd_pick, "t", "work", "type", "shoes")
        self.assertEqual(code, 0)
        self.assertEqual(acts(bb), [("type", "#b2", "shoes", "--clear")])

    def test_unsure_exits_3_with_candidates_and_does_not_act(self):
        with mock.patch.object(jev, "pick", return_value=(None, [self.chosen], {"prob": 0.4})), \
             mock.patch.object(jev, "bb") as bb:
            code, out = run(jev.cmd_pick, "t", None, "click", None)
        self.assertEqual(code, 3)
        self.assertEqual(acts(bb), [])
        res = json.loads(out)
        self.assertTrue(res["unsure"])
        self.assertEqual(res["candidates"][0]["selector"], "#b2")


class Check(unittest.TestCase):
    def check(self, nouls, wait, pages=None, clock=None):
        page = {"url": "u", "text": "hello"}
        patches = [
            mock.patch.object(jev, "page_eval", side_effect=pages or (lambda *a: page)),
            mock.patch.object(jev, "decide", side_effect=[{"holds": {"noul": n}} for n in nouls]),
            mock.patch.object(jev.time, "sleep"),
        ]
        if clock:
            patches.append(mock.patch.object(jev.time, "monotonic", side_effect=clock))
        with contextlib.ExitStack() as st:
            mocks = [st.enter_context(p) for p in patches]
            code, out = run(jev.cmd_check, "c", None, wait)
        return code, json.loads(out), mocks[1].call_count

    def test_yes_first_time(self):
        self.assertEqual(self.check([0.9], 0)[0], 0)

    def test_no_without_wait_asks_once(self):
        code, res, calls = self.check([0.2], 0)
        self.assertEqual((code, res["ok"], calls), (4, False, 1))

    def test_polls_until_yes(self):
        code, _, calls = self.check([0.2, 0.9], 30)
        self.assertEqual((code, calls), (0, 2))

    def test_times_out_with_exit_4(self):
        code, res, calls = self.check([0.2, 0.3], 5, clock=[0, 1, 6])
        self.assertEqual((code, res["ok"], calls), (4, False, 2))

    def test_permanent_bridge_error_fails_at_once(self):
        with mock.patch.object(jev, "page_eval", side_effect=jev.BridgeError("'eval' needs a tab of your own")) as pe, \
             mock.patch.object(jev, "decide") as d, mock.patch.object(jev.time, "sleep"):
            with self.assertRaises(SystemExit):
                jev.cmd_check("c", None, 30)
        self.assertEqual((pe.call_count, d.call_count), (1, 0))

    def test_reads_text_outside_editors_not_innertext(self):
        with mock.patch.object(jev, "bb", return_value={"value": {"url": "u", "text": "t"}}) as bb, \
             mock.patch.object(jev, "decide", return_value={"holds": {"noul": 0.9}}):
            run(jev.cmd_check, "c", None, 0)
        args = bb.call_args[0]
        self.assertEqual(args[0], "eval")
        self.assertIn("isContentEditable", args[1])
        self.assertIn("input,textarea,select", args[1])

    def test_unreadable_page_mid_navigation_is_not_yet(self):
        page = {"url": "u", "text": "done"}
        code, _, calls = self.check([0.9], 30, pages=[jev.BridgeError("frame removed"), page])
        self.assertEqual((code, calls), (0, 1))


class Main(unittest.TestCase):
    def test_profile_flag_both_forms(self):
        for argv in (["pick", "x", "--profile", "work"], ["pick", "x", "--profile=work"]):
            with mock.patch.object(jev, "cmd_pick", return_value=0) as c:
                jev.main(argv)
            c.assert_called_once_with("x", "work", None, None)

    def test_plain_do_clicks(self):
        with mock.patch.object(jev, "cmd_pick", return_value=0) as c:
            jev.main(["do", "x"])
        c.assert_called_once_with("x", None, "click", None)

    def test_type_flag_selects_type(self):
        with mock.patch.object(jev, "cmd_pick", return_value=0) as c:
            jev.main(["do", "x", "--type", "shoes"])
        c.assert_called_once_with("x", None, "type", "shoes")

    def test_flags_for_the_wrong_command_are_rejected(self):
        for argv in (["pick", "x", "--type", "a"], ["do", "x", "--wait", "5"], ["check", "x", "--wait", "abc"],
                     ["check", "x", "--wait", "nan"], ["check", "x", "--wait", "-1"]):
            self.assertEqual(run(jev.main, argv)[0], 1)

    def test_flag_without_value_prints_usage(self):
        for argv in (["check", "x", "--wait"], ["do", "x", "--type"], ["do", "x", "--profile"]):
            code, out = run(jev.main, argv)
            self.assertEqual(code, 1)
            self.assertIn("needs a value", out)


class Bridge(unittest.TestCase):
    def proc(self, stdout, stderr=""):
        return mock.Mock(stdout=stdout, stderr=stderr)

    def test_unwraps_result(self):
        with mock.patch.object(jev.subprocess, "run", return_value=self.proc('{"ok": true, "result": {"a": 1}}')):
            self.assertEqual(jev.bb("text", profile=None), {"a": 1})

    def test_not_ok_raises(self):
        with mock.patch.object(jev.subprocess, "run", return_value=self.proc('{"ok": false, "error": "x"}')):
            with self.assertRaises(jev.BridgeError):
                jev.bb("text", profile=None)

    def test_non_json_raises_with_stderr(self):
        with mock.patch.object(jev.subprocess, "run", return_value=self.proc("", "bridge server not running")):
            with self.assertRaisesRegex(jev.BridgeError, "not running"):
                jev.bb("text", profile=None)

    def test_click_cleans_up_tags(self):
        chosen = {"selector": '[data-jev="1"]', "label": "l"}
        with mock.patch.object(jev, "pick", return_value=(chosen, [], {})), \
             mock.patch.object(jev, "bb") as bb:
            run(jev.cmd_pick, "t", None, "click", None)
        self.assertEqual(bb.call_args_list[-1][0], ("eval", jev.CLEAN_JS))


class Privacy(unittest.TestCase):
    def test_every_request_demands_zero_retention(self):
        sent = {}

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps({"answers": {}}).encode()

        def fake_urlopen(req, timeout):
            sent.update(json.loads(req.data))
            return Resp()

        with mock.patch.object(jev, "api_key", return_value="k"), \
             mock.patch.object(jev.urllib.request, "urlopen", fake_urlopen):
            jev.decide("s", {})
        self.assertEqual(sent["provider"], {"zdr": True, "data_collection": "deny"})

    def test_key_never_appears_in_errors(self):
        secret = "sk-or-v1-SECRETMARKER"

        def boom(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b"unauthorized"))

        err, out = io.StringIO(), io.StringIO()
        with mock.patch.object(jev, "api_key", return_value=secret), \
             mock.patch.object(jev.urllib.request, "urlopen", boom), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as e:
                jev.decide("s", {})
        self.assertNotIn(secret, str(e.exception.code) + out.getvalue() + err.getvalue())

    def test_unreachable_and_bad_body_exit_cleanly(self):
        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"no": "answers"}'

        def down(req, timeout):
            raise urllib.error.URLError("down")

        for fake in (down, lambda req, timeout: Resp()):
            with mock.patch.object(jev, "api_key", return_value="k"), \
                 mock.patch.object(jev.urllib.request, "urlopen", fake):
                with self.assertRaises(SystemExit):
                    jev.decide("s", {})


if __name__ == "__main__":
    unittest.main()

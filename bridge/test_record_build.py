"""Tests for building a recording bundle with no agent involved.

A recording started from the extension's popup has no `bb record stop` to build
its bundle, so the bridge server builds it itself:

- recording.finish(raw, parent, name=None) names and builds the bundle folder;
- POST /record/build on the server calls it for a recording id.

The server tests start the real server.py (as `uv run --script`, like setup does)
on a free port with a throwaway home, and talk to it over HTTP and the extension
websocket, so nothing here needs a browser.

    uv run --no-project python -m unittest bridge.test_record_build
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import re
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from test_recording import T0, console, ev, load_recording, small_scenario  # noqa: E402

TOKEN = "test-token-123"
BUNDLE_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2}-\d{4})-([a-z0-9][a-z0-9-]*[a-z0-9])$")


def stamp_of(t_ms: float) -> str:
    """The folder stamp for a rec.start at t_ms: local time, minutes."""
    return time.strftime("%Y-%m-%d-%H%M", time.localtime(t_ms / 1000))


def files_of(folder: pathlib.Path) -> set[str]:
    return {str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file()}


def write_raw(home: pathlib.Path, rec: str, events: list[dict]) -> pathlib.Path:
    recording = load_recording()
    for m in events:
        recording.append(home, m)
    return recording.raw_dir(home, rec)


# ===================================================================== finish()


class FinishTest(unittest.TestCase):
    """Condition 1 — recording.finish(raw, parent, name=None)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.parent = self.root / "out" / "not-yet-there"
        self.recording = load_recording()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def raw(self, rec="r1", **kw) -> pathlib.Path:
        return write_raw(self.home, rec, small_scenario(rec, **kw))

    def finish(self, raw, name=None) -> pathlib.Path:
        self.assertTrue(hasattr(self.recording, "finish"), "recording.finish does not exist")
        out = self.recording.finish(raw, self.parent, name) if name is not None \
            else self.recording.finish(raw, self.parent)
        self.assertIsNotNone(out, "finish must return the bundle folder")
        return pathlib.Path(out)

    def name_parts(self, path: pathlib.Path) -> tuple[str, str]:
        m = BUNDLE_NAME.match(path.name)
        self.assertIsNotNone(m, f"bundle dir name {path.name!r} is not <YYYY-MM-DD-HHMM>-<slug>")
        return m.group(1), m.group(2)

    def test_builds_into_parent_stamp_slug_and_returns_that_path(self) -> None:
        path = self.finish(self.raw(), "Pay button broken")
        self.assertEqual(path, self.parent / f"{stamp_of(T0)}-pay-button-broken")
        self.assertTrue(path.is_dir())

    def test_the_stamp_is_the_local_time_of_rec_start_not_now_nor_a_later_event(self) -> None:
        # rec.start at T0 (a fixed day in 2025); the other events three hours later.
        later = 3 * 3600 * 1000
        events = [ev("r1", 0, "rec.start", {"url": "https://shop.example.com/cart", "title": "Cart"}),
                  console("r1", later, "error", "PAY_ERROR late"),
                  ev("r1", later + 10, "rec.stop", {})]
        path = self.finish(write_raw(self.home, "r1", events), "late")
        stamp, _ = self.name_parts(path)
        self.assertEqual(stamp, stamp_of(T0))
        self.assertNotEqual(stamp, stamp_of(T0 + later))
        self.assertNotEqual(stamp, time.strftime("%Y-%m-%d-%H%M"))

    def test_the_name_is_slugged_like_recording_slug(self) -> None:
        name = "  Фриз генератора отчётов, łódź!! "
        _, slug = self.name_parts(self.finish(self.raw(), name))
        self.assertEqual(slug, self.recording.slug(name))
        self.assertEqual(slug, "friz-generatora-otchyotov-lodz")

    def test_a_long_name_is_capped_like_recording_slug(self) -> None:
        name = "Very Long Bug Name " * 6
        _, slug = self.name_parts(self.finish(self.raw(), name))
        self.assertEqual(slug, self.recording.slug(name))
        self.assertLessEqual(len(slug), 40)

    def test_without_a_name_the_slug_is_the_start_page_title(self) -> None:
        _, slug = self.name_parts(self.finish(self.raw(title="Checkout Page", url="https://shop.example.com/cart")))
        self.assertEqual(slug, "checkout-page")

    def test_without_a_name_or_title_the_slug_is_the_start_page_host(self) -> None:
        _, slug = self.name_parts(self.finish(self.raw(title="", url="https://shop.example.com/cart?x=1")))
        self.assertEqual(slug, "shop-example-com")

    def test_with_nothing_to_name_it_by_the_slug_is_session(self) -> None:
        _, slug = self.name_parts(self.finish(self.raw(title="", url="")))
        self.assertEqual(slug, "session")

    def test_a_name_that_slugs_to_nothing_falls_back_like_no_name(self) -> None:
        # recording.slug("!!!") is "", so the folder must not end in "-".
        _, slug = self.name_parts(self.finish(self.raw(title="", url=""), "!!!"))
        self.assertEqual(slug, "session")

    def test_an_existing_folder_gets_2_then_3(self) -> None:
        raw = self.raw()
        first = self.finish(raw, "same")
        second = self.finish(raw, "same")
        third = self.finish(raw, "same")
        self.assertEqual(second, first.parent / f"{first.name}-2")
        self.assertEqual(third, first.parent / f"{first.name}-3")
        for p in (first, second, third):
            self.assertTrue((p / "report.md").is_file(), p)

    def test_the_folder_holds_what_build_bundle_writes(self) -> None:
        raw = self.raw()
        path = self.finish(raw, "pay")
        reference = self.recording.build_bundle(raw, self.root / "reference", "pay")
        self.assertIn("report.md", files_of(path))
        self.assertEqual(files_of(path), files_of(pathlib.Path(reference)))
        self.assertIn("PAY_ERROR", (path / "report.md").read_text())


# ============================================================ POST /record/build


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _uv_dir(*args: str) -> str:
    return subprocess.run(["uv", *args], capture_output=True, text=True, check=True).stdout.strip()


class Server:
    """server.py running for real, with its own home, port and token."""

    def __init__(self, root: pathlib.Path, recordings_dir: pathlib.Path | None) -> None:
        self.root = root
        self.home = root / "bridge-home"
        self.user_home = root / "user-home"  # what ~ means for the server
        self.user_home.mkdir(parents=True)
        self.port = _free_port()
        env = {k: v for k, v in os.environ.items() if not k.startswith("BROWSER_BRIDGE_")}
        env.update(
            # uv keeps its cache and pythons under the real home; ~ moves only for the server.
            UV_CACHE_DIR=_uv_dir("cache", "dir"),
            UV_PYTHON_INSTALL_DIR=_uv_dir("python", "dir"),
            HOME=str(self.user_home),
            BROWSER_BRIDGE_HOME=str(self.home),
            BROWSER_BRIDGE_TOKEN=TOKEN,
            BROWSER_BRIDGE_PORT=str(self.port),
            BROWSER_BRIDGE_SHOTS=str(root / "shots"),
        )
        if recordings_dir is not None:
            env["BROWSER_BRIDGE_RECORDINGS_DIR"] = str(recordings_dir)
        self.log_path = root / "server.log"
        self.log = open(self.log_path, "w")
        self.proc = subprocess.Popen(["uv", "run", "--script", str(HERE / "server.py")],
                                     env=env, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.time() + 120
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server.py exited at start:\n{self.log_path.read_text()}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2):
                    return
            except OSError:
                time.sleep(0.2)
        self.stop()
        raise RuntimeError(f"server.py did not come up:\n{self.log_path.read_text()}")

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()

    def build(self, body: dict, token: str | None = TOKEN) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Bridge-Token"] = token
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/record/build",
                                     data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw or b"{}")
            except ValueError:
                return exc.code, {"_raw": raw.decode(errors="replace")}


class ExtensionSocket:
    """A bare RFC 6455 client on /ext, standing in for the Chrome extension."""

    def __init__(self, port: int, profile: str = "p") -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=60)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET /ext HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                           "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        self.reader = self.sock.makefile("rb")
        status = self.reader.readline()
        if b" 101 " not in status:
            raise RuntimeError(f"websocket upgrade refused: {status!r}")
        while self.reader.readline() not in (b"\r\n", b""):
            pass
        self.send({"token": TOKEN, "profile": profile, "userAgent": "test"})

    def send(self, obj: dict) -> None:
        data = json.dumps(obj).encode()
        n = len(data)
        head = bytes([0x81])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 1 << 16:
            head += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)
        whole = (mask * (n // 4 + 1))[:n]
        masked = (int.from_bytes(data, "big") ^ int.from_bytes(whole, "big")).to_bytes(n, "big")
        self.sock.sendall(head + mask + masked)

    def recv(self) -> dict:
        b1, b2 = self.reader.read(2)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self.reader.read(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self.reader.read(8))[0]
        return json.loads(self.reader.read(n))

    def close(self) -> None:
        self.sock.close()


def heavy_rrweb(rec: str, t: float) -> dict:
    """An event that takes the server's disk writer a noticeable while to redact."""
    nodes = [{"type": 2, "tagName": "div", "attributes": {"class": "c", "data-x": "v" * 20}, "id": 2 * i,
              "childNodes": [{"type": 3, "textContent": "filler text token=abc " * 3, "id": 2 * i + 1}]}
             for i in range(4000)]
    return ev(rec, t, "rec.rrweb", {"event": {"type": 6, "timestamp": T0 + t,
                                              "data": {"plugin": "test/filler", "payload": {"nodes": nodes}}}})


class RecordBuildEndpointTest(unittest.TestCase):
    """Condition 3 — POST /record/build, with BROWSER_BRIDGE_RECORDINGS_DIR set."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = pathlib.Path(cls.tmp.name).resolve()
        cls.out = cls.root / "chosen" / "recordings"  # does not exist yet
        cls.server = Server(cls.root, cls.out)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        cls.tmp.cleanup()

    def recorded(self, rec: str, **kw) -> pathlib.Path:
        return write_raw(self.server.home, rec, small_scenario(rec, **kw))

    def built(self) -> set[str]:
        return {p.name for p in self.out.iterdir()} if self.out.exists() else set()

    # --- refusals

    def test_401_without_a_token(self) -> None:
        self.recorded("auth1")
        before = self.built()
        code, _ = self.server.build({"rec": "auth1"}, token=None)
        self.assertEqual(code, 401)
        self.assertEqual(self.built(), before)

    def test_401_with_a_wrong_token(self) -> None:
        self.recorded("auth2")
        before = self.built()
        code, _ = self.server.build({"rec": "auth2"}, token="not-the-token")
        self.assertEqual(code, 401)
        self.assertEqual(self.built(), before)

    def test_400_for_a_rec_id_that_is_not_word_chars_1_to_64(self) -> None:
        before = self.built()
        for bad in ("../../etc", "a/b", "has-dash", "has space", "", "a" * 65):
            with self.subTest(rec=bad):
                code, _ = self.server.build({"rec": bad})
                self.assertEqual(code, 400)
        code, _ = self.server.build({"name": "no rec at all"})
        self.assertEqual(code, 400)
        self.assertEqual(self.built(), before)

    def test_nothing_recorded_under_the_id_is_ok_false_with_an_error(self) -> None:
        before = self.built()
        code, out = self.server.build({"rec": "never_recorded_1"})
        self.assertEqual(code, 200, out)
        self.assertIs(out.get("ok"), False, out)
        self.assertIsInstance(out.get("error"), str)
        self.assertTrue(out["error"].strip(), out)
        self.assertEqual(self.built(), before)

    # --- a build

    def test_builds_the_bundle_into_the_recordings_dir_and_answers_path_report_handoff(self) -> None:
        self.recorded("okrec1", title="Acme Checkout")
        code, out = self.server.build({"rec": "okrec1", "name": "Pay button dead"})
        self.assertEqual(code, 200, out)
        self.assertIs(out.get("ok"), True, out)
        path = pathlib.Path(out["path"])
        self.assertTrue(path.is_absolute(), out)
        self.assertEqual(path, self.out / f"{stamp_of(T0)}-pay-button-dead")
        self.assertEqual(pathlib.Path(out["report"]), path / "report.md")
        self.assertIn("PAY_ERROR", (path / "report.md").read_text())

    def test_without_a_name_the_folder_is_named_by_the_start_page(self) -> None:
        self.recorded("okrec2", title="Invoice Editor")
        code, out = self.server.build({"rec": "okrec2"})
        self.assertIs(out.get("ok"), True, out)
        self.assertEqual(pathlib.Path(out["path"]).name, f"{stamp_of(T0)}-invoice-editor")

    def test_handoff_is_one_short_text_holding_the_absolute_report_path(self) -> None:
        self.recorded("okrec3", title="Handoff Page")
        _, out = self.server.build({"rec": "okrec3"})
        self.assertIs(out.get("ok"), True, out)
        handoff = out.get("handoff")
        self.assertIsInstance(handoff, str, out)
        report = str(self.out / f"{stamp_of(T0)}-handoff-page" / "report.md")
        self.assertIn(report, handoff)
        self.assertLessEqual(len(handoff), len(report) + 400, f"handoff is not short: {handoff!r}")

    def test_building_the_same_recording_twice_gets_a_2_folder(self) -> None:
        self.recorded("okrec4", title="Twice")
        _, first = self.server.build({"rec": "okrec4"})
        _, second = self.server.build({"rec": "okrec4"})
        self.assertIs(second.get("ok"), True, second)
        self.assertEqual(pathlib.Path(second["path"]), pathlib.Path(first["path"]).parent
                         / f"{pathlib.Path(first['path']).name}-2")

    # --- events still in the server's write queue

    def test_events_still_queued_for_the_disk_are_in_the_bundle(self) -> None:
        rec = "queued1"
        ext = ExtensionSocket(self.server.port)
        try:
            ext.send(ev(rec, 0, "rec.start", {"url": "https://shop.example.com/cart", "title": "Queued"}))
            for i in range(15):  # a few seconds of redaction work for the writer thread
                ext.send(heavy_rrweb(rec, 10 + i))
            ext.send(console(rec, 500, "error", "LAST_EVENT_MARKER after the backlog"))
            ext.send(ev(rec, 600, "rec.stop", {}))
            # The socket loop handles messages in order: a pong means every rec event
            # above has been handed to the write queue (not necessarily written).
            ext.send({"op": "ping"})
            self.assertEqual(ext.recv().get("op"), "pong")
            code, out = self.server.build({"rec": rec, "name": "queued"})
        finally:
            ext.close()
        self.assertEqual(code, 200, out)
        self.assertIs(out.get("ok"), True, out)
        path = pathlib.Path(out["path"])
        text = "".join(p.read_text(errors="replace") for p in path.rglob("*")
                       if p.is_file() and p.suffix in (".md", ".jsonl"))
        self.assertIn("LAST_EVENT_MARKER", text, "an event still queued at build time is missing")


    def test_a_build_asked_for_right_after_stop_waits_for_the_tail(self) -> None:
        # The popup asks over HTTP right after the extension sent rec.stop on its
        # socket: no round trip in between guarantees the server has read it.
        rec = "racing1"
        ext = ExtensionSocket(self.server.port)
        try:
            ext.send(ev(rec, 0, "rec.start", {"url": "https://shop.example.com/cart", "title": "Racing"}))
            for i in range(15):
                ext.send(heavy_rrweb(rec, 10 + i))
            ext.send(console(rec, 500, "error", "LAST_EVENT_MARKER right before stop"))
            ext.send(ev(rec, 600, "rec.stop", {}))
            code, out = self.server.build({"rec": rec, "name": "racing"})
        finally:
            ext.close()
        self.assertEqual(code, 200, out)
        self.assertIs(out.get("ok"), True, out)
        text = "".join(p.read_text(errors="replace") for p in pathlib.Path(out["path"]).rglob("*")
                       if p.is_file() and p.suffix in (".md", ".jsonl"))
        self.assertIn("LAST_EVENT_MARKER", text, "the build ran before the recording's tail arrived")

    def test_a_body_that_is_not_an_object_is_a_400(self) -> None:
        code, _ = self.server.build([])
        self.assertEqual(code, 400)


class RecordBuildDefaultDirTest(unittest.TestCase):
    """Condition 3 — without BROWSER_BRIDGE_RECORDINGS_DIR, ~/Downloads/browser-recordings."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = pathlib.Path(cls.tmp.name).resolve()
        cls.server = Server(cls.root, None)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        cls.tmp.cleanup()

    def test_the_bundle_goes_to_downloads_browser_recordings(self) -> None:
        write_raw(self.server.home, "deflt1", small_scenario("deflt1", title="Default Dir"))
        code, out = self.server.build({"rec": "deflt1"})
        self.assertEqual(code, 200, out)
        self.assertIs(out.get("ok"), True, out)
        expected = self.server.user_home / "Downloads" / "browser-recordings" / f"{stamp_of(T0)}-default-dir"
        self.assertEqual(pathlib.Path(out["path"]), expected)
        self.assertTrue((expected / "report.md").is_file())


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.110", "uvicorn>=0.27", "websockets>=12"]
# ///
"""Local bridge between an agent (HTTP) and Chrome extensions (WebSocket).

Agent  --POST /cmd-->  server  --ws /ext-->  extension  --> real browser tab
                          <----- reply ------

One server, many browsers: each extension install announces a **profile label**
on connect ("private", "10cfi", ...), and every command names the profile it is
meant for. That is what lets one agent drive several logged-in Chrome profiles
without them fighting over a single socket.

Binds to 127.0.0.1 only. Every call must carry the pairing token, both on
/cmd (header) and on the extension's WS handshake. Run with:

    uv run ${CLAUDE_PLUGIN_ROOT}/bridge/server.py
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import queue
import re
import sys
import threading
import json
import os
import pathlib
import time
import uuid

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect

import recording

# State lives outside the plugin: the plugin folder is replaced wholesale on
# every update, and nothing that must survive an update — least of all a secret
# — belongs inside a checkout.
HOME = pathlib.Path(os.getenv("BROWSER_BRIDGE_HOME", pathlib.Path.home() / ".claude-browser-bridge"))
TOKEN_FILE = HOME / "token"
SHOTS_DIR = pathlib.Path(os.getenv("BROWSER_BRIDGE_SHOTS", HOME / "shots"))
PORT = int(os.getenv("BROWSER_BRIDGE_PORT", "8799"))
DEFAULT_TIMEOUT = 45.0


def _token() -> str:
    tok = os.getenv("BROWSER_BRIDGE_TOKEN")
    if tok:
        return tok.strip()
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    raise SystemExit(f"no pairing token — run bridge/setup.sh first (expected {TOKEN_FILE})")


TOKEN = _token()

app = FastAPI(title="claude-browser-bridge")


class Client:
    """One connected extension — that is, one Chrome profile."""

    def __init__(self, ws: WebSocket, profile: str, ua: str | None) -> None:
        self.ws = ws
        self.profile = profile
        self.ua = ua
        self.connected_at = time.time()
        self.pending: dict[str, asyncio.Future] = {}

    async def call(self, op: str, args: dict, timeout: float) -> dict:
        req_id = uuid.uuid4().hex[:12]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[req_id] = fut
        try:
            await self.ws.send_text(json.dumps({"id": req_id, "op": op, "args": args}))
        except Exception as exc:  # socket died between check and send
            self.pending.pop(req_id, None)
            raise HTTPException(503, f"extension socket send failed: {exc}") from exc
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise HTTPException(
                504, f"profile '{self.profile}' did not answer '{op}' within {timeout}s"
            ) from None
        finally:
            self.pending.pop(req_id, None)

    def resolve(self, msg: dict) -> None:
        fut = self.pending.get(msg.get("id", ""))
        if fut and not fut.done():
            fut.set_result(msg)

    def drop(self) -> None:
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("extension disconnected"))
        self.pending.clear()

    def info(self) -> dict:
        return {
            "profile": self.profile,
            "connected_for_s": round(time.time() - self.connected_at, 1),
            "user_agent": self.ua,
        }


class Registry:
    """Profile label -> Client. Reconnects replace the entry for that label."""

    def __init__(self) -> None:
        self.clients: dict[str, Client] = {}

    def add(self, client: Client) -> None:
        old = self.clients.get(client.profile)
        if old is not None:  # stale socket from a reloaded service worker
            old.drop()
        self.clients[client.profile] = client

    def remove(self, client: Client) -> None:
        if self.clients.get(client.profile) is client:
            del self.clients[client.profile]
        client.drop()

    def pick(self, profile: str | None) -> Client:
        """Resolve the target profile, failing loudly rather than guessing.

        With exactly one browser connected and no profile named, that browser is
        unambiguous, so use it. With several, refuse — silently picking one means
        typing a password into the wrong logged-in identity.
        """
        if not self.clients:
            raise HTTPException(
                503,
                "no extension connected — open Chrome and check the bridge extension "
                "is enabled in that profile and has the pairing token in its popup",
            )
        if profile:
            client = self.clients.get(profile)
            if client is None:
                raise HTTPException(
                    404,
                    f"profile '{profile}' is not connected; connected: {sorted(self.clients)}",
                )
            return client
        if len(self.clients) == 1:
            return next(iter(self.clients.values()))
        raise HTTPException(
            409,
            f"several profiles connected ({sorted(self.clients)}) — pass --profile to say which",
        )


registry = Registry()


# Recording events are redacted and written by one thread, in arrival order:
# redacting a multi-megabyte DOM snapshot on the event loop would stall every
# other op meanwhile. A big stack, because a deep DOM is a deep JSON tree.
_rec_queue: queue.Queue = queue.Queue()


def _write_recordings() -> None:
    sys.setrecursionlimit(20000)
    while True:
        msg = _rec_queue.get()
        try:
            recording.append(HOME, msg)
        except Exception as exc:
            print(f"[bridge] could not write recording event: {exc}", flush=True)
            # Leave a mark, so the report says something is missing.
            with contextlib.suppress(Exception):
                recording.append(HOME, {**{k: msg.get(k) for k in ("op", "rec", "t", "tabId")},
                                        "method": "rec.dropped",
                                        "params": {"event": msg.get("method"), "reason": str(exc)[:200]}})
        finally:
            _rec_queue.task_done()


threading.stack_size(256 * 1024 * 1024)
threading.Thread(target=_write_recordings, name="recordings", daemon=True).start()
threading.stack_size(0)


def _persist_screenshot(result: dict) -> dict:
    """Move a base64 screenshot out of the JSON payload and onto disk.

    Keeps agent-facing responses small: the reply carries a path to Read, not
    a megabyte of base64.
    """
    data_url = result.get("dataUrl")
    if not isinstance(data_url, str) or "," not in data_url:
        return result
    header, b64 = data_url.split(",", 1)
    ext = "png" if "png" in header else "jpg"
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = SHOTS_DIR / f"shot-{int(time.time())}-{uuid.uuid4().hex[:6]}.{ext}"
    path.write_bytes(base64.b64decode(b64))
    result.pop("dataUrl")
    result["path"] = str(path)
    return result


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "port": PORT,
        "profiles": [c.info() for c in registry.clients.values()],
        "extension_connected": bool(registry.clients),
    }


@app.post("/cmd")
async def cmd(request: Request, x_bridge_token: str = Header(default="")) -> dict:
    if x_bridge_token != TOKEN:
        raise HTTPException(401, "bad or missing X-Bridge-Token")
    body = await request.json()
    op = body.get("op")
    if not op:
        raise HTTPException(400, "missing 'op'")
    timeout = float(body.get("timeout") or DEFAULT_TIMEOUT)
    client = registry.pick(body.get("profile"))
    reply = await client.call(op, body.get("args") or {}, timeout)
    if op == "recordStop":
        # Everything the extension sent before this reply is queued; let it reach
        # the disk before `bb` builds the bundle from it.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.to_thread(_rec_queue.join), 60)
    if reply.get("ok") and isinstance(reply.get("result"), dict):
        reply["result"] = _persist_screenshot(reply["result"])
    reply["profile"] = client.profile
    return reply


# Where the popup's recordings go: no agent started them, so there is no agent
# working directory to put them in.
RECORDINGS_DIR = pathlib.Path(
    os.getenv("BROWSER_BRIDGE_RECORDINGS_DIR", pathlib.Path.home() / "Downloads" / "browser-recordings")
)


def _stop_written(raw: pathlib.Path) -> bool:
    """Whether the recording's rec.stop, its last event, has reached the disk."""
    try:
        with open(raw / "events.jsonl", "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 65536))
            return b'"rec.stop"' in fh.read()
    except FileNotFoundError:
        return False


@app.post("/record/build")
async def record_build(request: Request, x_bridge_token: str = Header(default="")) -> dict:
    """Build the folder for a recording the extension's popup started and stopped."""
    if x_bridge_token != TOKEN:
        raise HTTPException(401, "bad or missing X-Bridge-Token")
    body = await request.json()
    rec = str((body.get("rec") if isinstance(body, dict) else "") or "")
    if not re.fullmatch(r"\w{1,64}", rec):
        raise HTTPException(400, f"bad recording id {rec!r}")
    raw = recording.raw_dir(HOME, rec)
    # This request came over its own connection, so the recording's tail may
    # still be in flight on the extension's socket or in the write queue: wait
    # until its last event, rec.stop, is on the disk.
    deadline = time.monotonic() + (30 if (raw / "events.jsonl").exists() else 2)
    while not _stop_written(raw) and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.to_thread(_rec_queue.join), 60)
    recording.prune(HOME)
    if not (raw / "events.jsonl").exists():
        return {"ok": False, "error": "nothing was recorded — no events reached the bridge server"}
    try:
        dest = await asyncio.to_thread(recording.finish, raw, RECORDINGS_DIR, body.get("name") or None)
    except Exception as exc:
        return {"ok": False, "error": f"could not build the bundle: {exc!r}"}
    report = dest / "report.md"
    return {
        "ok": True,
        "path": str(dest),
        "report": str(report),
        "handoff": f"A browser session I recorded: {report} — read it first; "
        "the steps, console, network, screenshots and DOM are in the same folder.",
    }


@app.websocket("/ext")
async def ext_socket(ws: WebSocket) -> None:
    await ws.accept()
    try:
        hello = json.loads(await asyncio.wait_for(ws.receive_text(), 10))
    except Exception:
        await ws.close(code=4000)
        return
    if hello.get("token") != TOKEN:
        await ws.send_text(json.dumps({"fatal": "bad token"}))
        await ws.close(code=4001)
        return

    profile = (hello.get("profile") or "").strip()
    if not profile:
        # Unlabelled browsers must still get distinct keys, or two of them collide
        # on one name and silently evict each other.
        n = 1
        while f"unnamed-{n}" in registry.clients:
            n += 1
        profile = f"unnamed-{n}"
    client = Client(ws, profile, hello.get("userAgent"))
    registry.add(client)
    print(f"[bridge] '{profile}' connected ({client.ua})", flush=True)

    try:
        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("op") == "ping":
                await ws.send_text(json.dumps({"op": "pong"}))
                continue
            # The popup can relabel a live connection; re-key it in place.
            if msg.get("op") == "relabel":
                new = (msg.get("profile") or "").strip()
                if new and new != client.profile:
                    registry.remove(client)
                    client.profile = new
                    registry.add(client)
                    print(f"[bridge] relabelled -> '{new}'", flush=True)
                continue
            # `bb record` events: redacted and written to disk as they arrive.
            if msg.get("op") == "rec":
                _rec_queue.put(msg)
                continue
            client.resolve(msg)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        print(f"[bridge] socket error ({client.profile}): {exc}", flush=True)
    finally:
        registry.remove(client)
        print(f"[bridge] '{client.profile}' disconnected", flush=True)


if __name__ == "__main__":
    print(f"[bridge] listening on http://127.0.0.1:{PORT} (ws /ext)", flush=True)
    # A recording's full DOM snapshot, styles inlined, can run to many megabytes.
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning", ws_max_size=128 * 1024 * 1024)

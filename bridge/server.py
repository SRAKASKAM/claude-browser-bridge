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
import json
import os
import pathlib
import time
import uuid

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect

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
    if reply.get("ok") and isinstance(reply.get("result"), dict):
        reply["result"] = _persist_screenshot(reply["result"])
    reply["profile"] = client.profile
    return reply


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
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")

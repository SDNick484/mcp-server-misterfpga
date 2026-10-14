"""A fake Zaparoo Core: the real JSON-RPC-over-WebSocket protocol on a real socket.

The server's own ZaparooClient connects to it exactly as it would to a MiSTer,
so tests exercise framing, id matching, notifications, errors and reconnects,
not a mock of our own code. It follows Zaparoo's documented contract
(docs/api/*.md in zaparoo-core): response and notification shapes, the
``data.category`` on run errors, ``ping``/``pong``, and the legacy-access
rules (methods a remote unauthenticated client may not call are refused).

It also records every method it receives, so tests can assert what the server
did *not* send.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

from aiohttp import WSMsgType, web

from .device import FakeDevice, now, tiny_png

log = logging.getLogger(__name__)

# Methods a legacy (unauthenticated, remote) client is refused on MiSTer (ASSUMPTION Z-LEGACY-CAPS).
LEGACY_REFUSED = {
    "device.status",
    "device.power.reboot",
    "device.power.shutdown",
    "update.check",
    "update.apply",
    "settings.update",
    "settings.backup",
    "clients",
}


class RpcError(Exception):
    def __init__(self, message: str, code: int = 1, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class FakeZaparoo:
    def __init__(self, device: FakeDevice, host: str = "127.0.0.1", port: int = 0) -> None:
        self.device = device
        self.host = host
        self.port = port
        self.received: list[tuple[str, Any]] = []  # (method, params), in order
        self.pings = 0
        self.silent = False  # True: accept frames but never answer (a hung Core)
        # method -> result to answer with instead of the simulated one: replays a real MiSTer's recorded
        # answers (doctor --dump) through the server's parsing code (tests/test_recorded.py).
        self.recorded: dict[str, Any] = {}
        self._sockets: set[web.WebSocketResponse] = set()
        self._tasks: set[asyncio.Task[None]] = set()  # in-flight answers (kept so they aren't garbage collected)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @property
    def methods(self) -> list[str]:
        return [m for m, _ in self.received]

    async def start(self) -> int:
        app = web.Application()
        app.router.add_get("/api/v0.1", self._ws)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        server = self._site._server
        assert server is not None
        self.port = server.sockets[0].getsockname()[1]  # type: ignore[attr-defined]
        return self.port

    async def stop(self) -> None:
        for ws in list(self._sockets):
            await ws.close()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def drop_connections(self) -> None:
        """Close every client connection, as a Core restart would."""
        for ws in list(self._sockets):
            await ws.close()

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        frame = json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
        for ws in list(self._sockets):
            if not ws.closed:
                await ws.send_str(frame)

    async def scan_card(self, uid: str) -> None:
        token = self.device.scan(uid)
        await self.notify("tokens.added", token)

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._sockets.add(ws)
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                if msg.data == "ping":
                    self.pings += 1
                    await ws.send_str("pong")
                    continue
                task = asyncio.create_task(self._answer(ws, msg.data))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        finally:
            self._sockets.discard(ws)
        return ws

    async def _answer(self, ws: web.WebSocketResponse, text: str) -> None:
        try:
            req = json.loads(text)
        except ValueError:
            await ws.send_str(json.dumps({"jsonrpc": "2.0", "error": {"code": -32700, "message": "parse error"}}))
            return
        method, params, req_id = req.get("method"), req.get("params"), req.get("id")
        self.received.append((method, params))
        if self.silent:
            return
        try:
            result = await self._call(str(method), params if isinstance(params, dict) else {})
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "result": result}
        except RpcError as exc:
            err: dict[str, Any] = {"code": exc.code, "message": str(exc)}
            if exc.data:
                err["data"] = exc.data
            reply = {"jsonrpc": "2.0", "id": req_id, "error": err}
        if not ws.closed:
            await ws.send_str(json.dumps(reply))

    # --- methods ---------------------------------------------------------------------------------
    async def _call(self, method: str, p: dict[str, Any]) -> Any:
        d = self.device
        if method in self.recorded:
            return self.recorded[method]
        if method in LEGACY_REFUSED:
            raise RpcError("not permitted for this client")
        if method == "version":
            return {"version": "2.20.0", "platform": "mister"}
        if method == "media":
            return {
                "database": {"exists": True, "indexing": False, "optimizing": False, "totalMedia": len(d.media)},
                "active": [d.active] if d.active else [],
            }
        if method == "media.active":
            return d.active
        if method == "media.search":
            words = str(p.get("query") or "").lower().split()
            systems = {s.lower() for s in p.get("systems") or []}
            rows = [
                m
                for m in d.media
                if all(w in m["name"].lower() or w in m["path"].lower() for w in words)
                and (not systems or m["system"]["id"].lower() in systems)
            ]
            limit = int(p.get("maxResults") or 100)
            return {
                "results": rows[:limit],
                "total": min(len(rows), limit),
                "pagination": {"hasNextPage": len(rows) > limit, "pageSize": limit},
            }
        if method == "media.meta":
            m = d.by_id(int(p.get("mediaId") or -1))
            if m is None:
                raise RpcError("media not found")
            return {
                "media": {
                    "path": m["path"],
                    "zapScript": m["zapScript"],
                    "parentDir": m["path"].rsplit("/", 1)[0],
                    "isMissing": bool(m.get("isMissing")),
                    "tags": m.get("tags", []),
                    "properties": {},
                    "title": {"slug": m["name"].lower(), "name": m["name"], "system": m["system"]},
                }
            }
        if method == "media.history":
            return {"entries": d.history[: int(p.get("limit") or 25)]}
        if method == "media.history.top":
            totals: dict[int, dict[str, Any]] = {}
            for e in d.history:
                t = totals.setdefault(
                    e["mediaId"],
                    {
                        **{k: e[k] for k in ("mediaId", "systemId", "systemName", "mediaName", "mediaPath")},
                        "totalPlayTime": 0,
                        "sessionCount": 0,
                        "lastPlayedAt": e["startedAt"],
                    },
                )
                t["totalPlayTime"] += e["playTime"]
                t["sessionCount"] += 1
            ranked = sorted(totals.values(), key=lambda t: -t["totalPlayTime"])
            return {"entries": ranked[: int(p.get("limit") or 25)]}
        if method == "systems":
            return {"systems": d.systems()}
        if method == "launchers":
            return {"launchers": d.launchers()}
        if method == "run":
            return await self._run(str(p.get("text") or ""))
        if method == "stop":
            stopped = d.to_menu()
            if stopped:
                await self.notify(
                    "media.stopped",
                    {
                        **{k: stopped[k] for k in ("systemId", "systemName", "mediaName", "mediaPath", "launcherId")},
                        "elapsed": 42,
                    },
                )
            return None
        if method == "screenshot":
            data = tiny_png()
            path = "/media/fat/screenshots/MiSTer_sim.png"
            (d.sd / path.lstrip("/")).write_bytes(data)
            return {"path": path, "data": base64.b64encode(data).decode(), "size": len(data)}
        if method == "readers":
            return {"readers": d.fixture.get("readers", [])}
        if method == "readers.write":
            if not any("write" in r.get("capabilities", []) for r in d.fixture.get("readers", [])):
                raise RpcError("no writable reader")
            d.cards_written.append(str(p.get("text")))
            return None
        if method == "mappings":
            return {"mappings": d.mappings}
        if method == "mappings.new":
            d.mappings.append({"id": str(len(d.mappings) + 1), "added": now(), **p})
            return None
        if method == "tokens":
            return {"active": d.tokens_active, "last": d.last_token}
        raise RpcError(f"method not found: {method}", code=-32601)

    async def _run(self, text: str) -> None:
        d = self.device
        if text.startswith("**launch.system:"):
            system = text.split(":", 1)[1]
            if d.core_for(system) is None:
                raise RpcError(
                    "this launcher's plugin for this system is not installed",
                    data={"category": "launch_repair", "reason": "launcher_plugin_missing"},
                )
            d.start_core(system)
            return None
        if text.startswith("**mister.script:"):
            d.scripts_run.append(text.split(":", 1)[1])
            return None
        media = d.by_script(text)
        if media is None:
            raise RpcError("media not found", data={"category": "media_not_found"})
        if media.get("coreMissing"):
            raise RpcError(
                "this launcher's plugin for this system is not installed",
                data={"category": "launch_repair", "reason": "launcher_plugin_missing"},
            )
        active = d.start_game(media)
        await self.notify(  # ASSUMPTION Z-NOTIFY: the documented payload
            "media.started",
            {k: active[k] for k in ("systemId", "systemName", "mediaPath", "mediaName")},
        )
        return None

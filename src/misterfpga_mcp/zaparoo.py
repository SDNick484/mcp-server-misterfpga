"""Zaparoo Core's API: one long-lived WebSocket per MiSTer, speaking JSON-RPC 2.0.

Why our own client and not a library: the API is small and documented as
stable (``/api/v0.1``, additive changes only), and every method this server may
send fits in one frozenset. A thin client keeps that allow-list in one place
and lets the tests run the real client against a fake Core on a real socket.

How it works:
- ``start()`` launches a background task that connects and reconnects with
  backoff (1 s doubling to 30 s). The first attempt sets ``tried``, so the
  first tool call after startup can wait for it briefly (hosts.py).
- Requests carry a uuid4 ``id``; responses are matched back to their futures.
  Several requests can be in flight on one socket.
- Notifications (no ``id``) update a small cache (the last scanned card) and
  are passed to listeners: the play tracker will be one.
- Liveness: Zaparoo answers the text ``ping`` with ``pong`` (ASSUMPTION
  Z-ENDPOINT). We send one every 20 s and drop the connection if nothing at
  all has arrived for 60 s, so a MiSTer that was switched off is noticed even
  though no TCP error ever arrives.
- No authentication: on MiSTer, Zaparoo admits unauthenticated LAN clients as
  "legacy" (ASSUMPTION Z-LEGACY), which is enough for every method below
  (ASSUMPTION Z-LEGACY-CAPS). We deliberately don't use an API key: configuring
  one makes Core refuse every key-less client, the Zaparoo phone app included.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiohttp

from .errors import MisterError, Unavailable, ZaparooError
from .tasks import stop_task

log = logging.getLogger(__name__)

API_PATH = "/api/v0.1"  # ASSUMPTION Z-ENDPOINT

# Everything this server may ever send. Nothing under settings.*, device.*,
# update.*, clients.*, profiles.*, input.*, and no media.generate: those change
# Zaparoo itself, the device, or other people's limits. tests/test_safety.py
# pins this set, so widening it is a deliberate, reviewed change.
ALLOWED_METHODS = frozenset(
    {
        "version",
        "media",
        "media.search",
        "media.meta",
        "media.active",
        "media.history",
        "media.history.top",
        "systems",
        "launchers",
        "run",
        "stop",
        "screenshot",
        "readers",
        "readers.write",
        "mappings",
        "mappings.new",
        "tokens",
    }
)

DEFAULT_TIMEOUT = 10.0
RUN_TIMEOUT = 35.0  # Core waits up to 30 s for a launch itself (ASSUMPTION Z-RUN-WAITS)
WRITE_TIMEOUT = 65.0  # readers.write waits for a card to be placed (ASSUMPTION Z-NFC)
PING_EVERY = 20.0
SILENCE_LIMIT = 60.0
BACKOFF_MAX = 30.0

SETUP_HINT = "Install it from Update All (Tools & Scripts → Zaparoo); the README's 'Set up your MiSTer' has the steps."

Listener = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class Token:
    uid: str
    at: datetime


def describe_failure(exc: BaseException, label: str, host: str, port: int) -> str:
    """One sentence saying why we couldn't reach Zaparoo, and what to check."""
    os_error = exc.os_error if isinstance(exc, aiohttp.ClientConnectorError) else exc
    if isinstance(os_error, OSError) and os_error.errno == errno.ECONNREFUSED:
        return (
            f"Zaparoo Core isn't running on {label} (connection refused on port {port}). "
            f"The MiSTer is on, but nothing is listening. {SETUP_HINT}"
        )
    if isinstance(exc, aiohttp.WSServerHandshakeError):
        return f"{label} answered on port {port}, but not as Zaparoo's API ({exc.status}): is this Zaparoo Core?"
    if isinstance(exc, TimeoutError | aiohttp.ServerTimeoutError) or (
        isinstance(os_error, OSError) and os_error.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH, errno.ETIMEDOUT)
    ):
        return f"No answer from {label} ({host}): is the MiSTer switched on and on the network?"
    return f"Can't reach Zaparoo on {label}: {exc.__class__.__name__}: {exc}"


class ZaparooClient:
    def __init__(self, label: str, host: str, port: int) -> None:
        self.label = label
        self.host = host
        self.port = port
        self.url = f"ws://{host}:{port}{API_PATH}"
        self.connected = False
        self.version: str | None = None
        self.error: str | None = None
        self.last_token: Token | None = None
        self.tried = asyncio.Event()  # set once the first connection attempt has finished, either way
        self._listeners: list[Listener] = []
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._task: asyncio.Task[None] | None = None
        self._last_heard = 0.0

    # --- lifecycle ---------------------------------------------------------------------------
    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"zaparoo {self.label}")

    async def stop(self) -> None:
        task, self._task = self._task, None
        await stop_task(task, 3.0, f"Zaparoo connection to {self.label}")
        self._fail_pending(Unavailable(f"Zaparoo connection to {self.label} closed"))

    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    async def _run(self) -> None:
        backoff = 1.0
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    async with session.ws_connect(self.url, timeout=aiohttp.ClientWSTimeout(ws_close=5)) as ws:
                        backoff = 1.0
                        await self._serve(ws)
                    if self.error is None:
                        self.error = f"Zaparoo on {self.label} closed the connection; reconnecting"
                except asyncio.CancelledError:
                    raise
                except (aiohttp.ClientError, OSError, TimeoutError) as exc:
                    self.error = describe_failure(exc, self.label, self.host, self.port)
                    log.info("%s: %s", self.label, self.error)
                finally:
                    self.connected = False
                    self._ws = None
                    self._fail_pending(Unavailable(self.error or f"Zaparoo on {self.label} disconnected"))
                self.tried.set()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)

    async def _serve(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws = ws
        self.connected = True
        self.error = None
        self._last_heard = time.monotonic()
        log.info("%s: connected to Zaparoo Core", self.label)
        pinger = asyncio.create_task(self._ping(ws), name=f"zaparoo ping {self.label}")
        primer = asyncio.create_task(self._prime(), name=f"zaparoo prime {self.label}")
        try:
            async for msg in ws:
                self._last_heard = time.monotonic()
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._handle(msg.data)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break
        finally:
            await stop_task(pinger, 1.0, f"Zaparoo ping for {self.label}")
            await stop_task(primer, 1.0, f"Zaparoo version read for {self.label}")

    async def _prime(self) -> None:
        """Read the version once per connection; set ``tried`` when it's in (or failed)."""
        try:
            info = await self.call("version")
            self.version = str(info.get("version")) if isinstance(info, dict) else None
        except MisterError as exc:
            log.warning("%s: couldn't read Zaparoo's version: %s", self.label, exc)
        finally:
            self.tried.set()

    async def _ping(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            await asyncio.sleep(PING_EVERY)
            if time.monotonic() - self._last_heard > SILENCE_LIMIT:
                self.error = f"Zaparoo on {self.label} stopped answering; reconnecting"
                log.warning("%s", self.error)
                await ws.close()
                return
            with contextlib.suppress(ConnectionError, RuntimeError):
                await ws.send_str("ping")  # ASSUMPTION Z-ENDPOINT: answered with "pong"

    # --- messages ----------------------------------------------------------------------------
    def _handle(self, text: str) -> None:
        if text == "pong":
            return
        try:
            msg = json.loads(text)
        except ValueError:
            log.debug("%s: ignoring a frame that isn't JSON: %.80s", self.label, text)
            return
        if not isinstance(msg, dict):
            return
        msg_id = msg.get("id")
        if isinstance(msg_id, str) and msg_id in self._pending:
            fut = self._pending.pop(msg_id)
            if fut.done():
                return
            if "error" in msg and msg["error"] is not None:
                fut.set_exception(self._error(msg["error"]))
            else:
                fut.set_result(msg.get("result"))
        elif msg_id is None and isinstance(msg.get("method"), str):
            params = msg.get("params")
            self._notify(msg["method"], params if isinstance(params, dict) else {})
        elif msg_id is None and "error" in msg:
            log.warning("%s: Zaparoo protocol error: %s", self.label, msg["error"])

    def _error(self, err: Any) -> ZaparooError:
        if not isinstance(err, dict):
            return ZaparooError(f"Zaparoo on {self.label} returned an error: {err!r}")
        raw = err.get("data")
        data: dict[str, Any] = raw if isinstance(raw, dict) else {}
        category = data.get("category") if isinstance(data.get("category"), str) else None
        reason = data.get("reason") if isinstance(data.get("reason"), str) else None
        return ZaparooError(str(err.get("message") or "error"), category=category, reason=reason)

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        if method == "tokens.added" and isinstance(params.get("uid"), str) and params["uid"]:
            self.last_token = Token(params["uid"], datetime.now(UTC))
        for listener in list(self._listeners):
            try:
                listener(method, params)
            except Exception:  # a listener's bug must not kill the connection
                log.exception("%s: notification listener failed on %s", self.label, method)

    def _fail_pending(self, exc: MisterError) -> None:
        pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(exc)

    async def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = DEFAULT_TIMEOUT) -> Any:
        """Send one request and return its ``result``. Raises ZaparooError for an error reply."""
        if method not in ALLOWED_METHODS:
            raise ValueError(f"{method!r} is not a Zaparoo method this server sends")  # a bug, not user input
        ws = self._ws
        if ws is None or not self.connected:
            raise Unavailable(self.error or f"Zaparoo on {self.label} isn't connected yet")
        req_id = str(uuid.uuid4())
        frame: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            frame["params"] = params
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            log.debug("%s -> %s %s", self.label, method, params)
            await ws.send_str(json.dumps(frame))
            async with asyncio.timeout(timeout):
                return await fut
        except TimeoutError:
            raise MisterError(
                f"Zaparoo on {self.label} didn't answer {method} within {timeout:.0f}s. "
                "Anything it already started keeps going; check get_status before retrying."
            ) from None
        except (ConnectionError, RuntimeError) as exc:
            raise Unavailable(f"Lost the connection to Zaparoo on {self.label}: {exc}") from None
        finally:
            self._pending.pop(req_id, None)

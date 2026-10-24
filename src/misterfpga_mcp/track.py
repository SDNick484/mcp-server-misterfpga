"""Play tracking: each MiSTer's games as Home Assistant entities, over MQTT (docs/DESIGN.md §10).

`mcp-server-misterfpga track` is a long-lived service next to `serve`. It keeps a
Zaparoo connection to every configured MiSTer, turns ``media.started`` /
``media.stopped`` into play sessions, and publishes them to Home Assistant's
broker with MQTT discovery. Topics and payloads are mcp-server-batocera's
playtracker's, so one automation or dashboard card works for the arcade and
both MiSTers:

    misterfpga/<host>/now_playing    retained  {"state": "playing", game, system, rom, source, started, host, device}
                                               or {"state": "idle"}
    misterfpga/<host>/last_session   retained  {game, system, rom, source, started, ended, duration_s, host, device}
    misterfpga/<host>/session        event     {"event_type": "session_start" | "session_end", ...}
    misterfpga/<host>/activity       retained  menu | playing | off
    misterfpga/<host>/status         retained  online | offline  (this MiSTer's Zaparoo connection)
    misterfpga/_tracker/status       retained  online | offline  (this process; the MQTT last will)

Why it runs here and not on the MiSTer (Batocera's runs on the cabinet):
Zaparoo already pushes events over the network, so nothing is installed on the
SD card, and a reflash can't lose it. And `serve` over stdio only lives as long
as a client session, so it can't be the tracker.

Why it holds a connection (Batocera's script connects per event): a real MQTT
last will. If this process dies, the broker marks it offline, and every entity
goes unavailable rather than showing a stale "playing".

What it doesn't do yet: attract mode (MiSTer Super Attract Mode) detection.
SAM's demo games are real launches, so until that lands they're logged like any
other session over MIN_SESSION_SECONDS.

Missed sessions: when the tracker or a MiSTer's connection was down, it catches
up from Zaparoo's own play history on reconnect, back to the newest session it
published (kept in ``track-state.json``). Zaparoo is the outbox, the way
Batocera's ``/tmp`` queue is.

Times: a session's ``started`` comes from the MiSTer (``media.active``), its
length from Zaparoo's ``elapsed``, and ``ended`` is started + length. So a
session's times are all on the MiSTer's clock and agree with its history, even
if this machine's clock differs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import socket
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiomqtt

from .config import HostSettings, Settings, config_dir
from .errors import MisterError
from .tasks import stop_task
from .zaparoo import ZaparooClient

log = logging.getLogger(__name__)

Message = tuple[str, str, bool]  # topic, payload, retain

ACTIVITIES = ("menu", "playing", "off")
HISTORY_LIMIT = 50  # sessions read back on reconnect; more than a long outage produces
SAME_SESSION = timedelta(seconds=60)  # live start vs history start, for one session (ASSUMPTION Z-SESSION-TIMES)
STATE_FILE = "track-state.json"


# --- settings -----------------------------------------------------------------------------------
@dataclass(frozen=True)
class TrackSettings:
    mqtt_host: str
    mqtt_port: int = 1883
    mqtt_user: str | None = None
    mqtt_password: str | None = None
    topic_prefix: str = "misterfpga"
    discovery_prefix: str = "homeassistant"
    min_session_s: int = 60
    state_file: Path = field(default_factory=lambda: config_dir() / STATE_FILE)

    @property
    def tracker_status(self) -> str:
        return f"{self.topic_prefix}/_tracker/status"


def load_track_settings(env: Mapping[str, str] | None = None) -> tuple[TrackSettings | None, list[str]]:
    """The tracker's settings from ``MISTERFPGA_MQTT_*``; None (and why) when they're unusable."""
    env = os.environ if env is None else env
    problems: list[str] = []
    host = (env.get("MISTERFPGA_MQTT_HOST") or "").strip()
    if not host:
        return None, ["MISTERFPGA_MQTT_HOST isn't set: say where Home Assistant's MQTT broker is."]

    def number(name: str, default: int, low: int, high: int) -> int:
        raw = (env.get(name) or "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            value = -1
        if not low <= value <= high:
            problems.append(f"{name}={raw!r} isn't a number from {low} to {high}; using {default}.")
            return default
        return value

    prefix = (env.get("MISTERFPGA_MQTT_TOPIC_PREFIX") or "misterfpga").strip().strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*", prefix):
        problems.append(f"MISTERFPGA_MQTT_TOPIC_PREFIX={prefix!r} isn't a usable MQTT topic; using misterfpga.")
        prefix = "misterfpga"
    settings = TrackSettings(
        mqtt_host=host,
        mqtt_port=number("MISTERFPGA_MQTT_PORT", 1883, 1, 65535),
        mqtt_user=env.get("MISTERFPGA_MQTT_USER") or None,
        mqtt_password=env.get("MISTERFPGA_MQTT_PASSWORD") or None,
        topic_prefix=prefix,
        discovery_prefix=(env.get("MISTERFPGA_MQTT_DISCOVERY_PREFIX") or "homeassistant").strip().strip("/"),
        min_session_s=number("MISTERFPGA_MIN_SESSION_SECONDS", 60, 0, 86400),
    )
    return settings, problems


# --- topics and Home Assistant discovery ------------------------------------------------------
def slug(name: str) -> str:
    """'Family Room' -> 'family_room': the topic and unique-id part for a MiSTer."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "mister"


def base_topic(track: TrackSettings, host: HostSettings) -> str:
    return f"{track.topic_prefix}/{slug(host.label)}"


def device_name(host: HostSettings) -> str:
    """'Family Room SuperStation One': entities then read 'Family Room SuperStation One Now playing'."""
    return f"{host.label} {host.model}"


def discovery_messages(track: TrackSettings, host: HostSettings, sw_version: str | None) -> list[Message]:
    """Home Assistant MQTT discovery configs: Batocera's playtracker entities, one HA device per MiSTer."""
    base = base_topic(track, host)
    node = f"misterfpga_{slug(host.label)}"
    device: dict[str, Any] = {
        "identifiers": [node],
        "name": device_name(host),
        "manufacturer": host.manufacturer or "MiSTer FPGA",
        "model": host.model,
    }
    if sw_version:
        device["sw_version"] = f"Zaparoo Core {sw_version}"
    tracker = {"topic": track.tracker_status}
    # Both must be online: this process (else nothing's watching) and this MiSTer's Zaparoo connection.
    available = {"availability": [tracker, {"topic": f"{base}/status"}], "availability_mode": "all"}
    np, last = f"{base}/now_playing", f"{base}/last_session"
    entities: list[tuple[str, str, dict[str, Any]]] = [
        (
            "sensor",
            "now_playing",
            {
                "name": "Now playing",
                "icon": "mdi:gamepad-variant",
                "state_topic": np,
                "value_template": "{{ value_json.game if value_json.state == 'playing' else 'Idle' }}",
                "json_attributes_topic": np,
            },
        ),
        (
            "binary_sensor",
            "playing",
            {
                "name": "Playing",
                "icon": "mdi:controller",
                "state_topic": np,
                "value_template": "{{ 'ON' if value_json.state == 'playing' else 'OFF' }}",
            },
        ),
        (
            "sensor",
            "now_playing_system",
            {
                "name": "Now playing system",
                "icon": "mdi:nintendo-game-boy",
                "state_topic": np,
                "value_template": "{{ value_json.system if value_json.state == 'playing' else 'None' }}",
            },
        ),
        (
            "sensor",
            "last_session",
            {
                "name": "Last played",
                "icon": "mdi:history",
                "state_topic": last,
                "value_template": "{{ value_json.game }}",
                "json_attributes_topic": last,
            },
        ),
        (
            "sensor",
            "last_session_duration",
            {
                "name": "Last session length",
                "device_class": "duration",
                "unit_of_measurement": "min",
                "state_topic": last,
                "value_template": "{{ (value_json.duration_s / 60) | round(1) }}",
            },
        ),
        (
            "event",
            "game_session",
            {
                "name": "Game session",
                "icon": "mdi:timer-outline",
                "state_topic": f"{base}/session",
                "event_types": ["session_start", "session_end"],
            },
        ),
        (
            "sensor",
            "activity",
            {
                "name": "Activity",
                "icon": "mdi:controller-classic",
                "device_class": "enum",
                "options": list(ACTIVITIES),
                "state_topic": f"{base}/activity",
            },
        ),
    ]
    out: list[Message] = []
    for component, object_id, cfg in entities:
        # Activity follows only this process: when the MiSTer is off it reads "off" rather than
        # "unavailable", Batocera's rule, so an automation can trigger on it.
        extra = {"availability_topic": track.tracker_status} if object_id == "activity" else available
        payload = {**cfg, **extra, "device": device, "unique_id": f"{node}_{object_id}"}
        out.append((f"{track.discovery_prefix}/{component}/{node}/{object_id}/config", json.dumps(payload), True))
    return out


# --- one MiSTer's sessions (pure: tests drive it directly) --------------------------------------
def parse_time(value: Any) -> datetime | None:
    """An RFC 3339 time from Zaparoo, as an aware datetime; None if it isn't one."""
    if not isinstance(value, str) or not value:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


@dataclass(frozen=True)
class Session:
    game: str
    system: str
    rom: str
    started: datetime  # the MiSTer's clock (ASSUMPTION Z-SESSION-TIMES)

    def info(self) -> dict[str, Any]:
        return {"game": self.game, "system": self.system, "rom": self.rom, "source": "player"}


@dataclass(frozen=True)
class Mark:
    """The newest session published for a MiSTer: catching up starts after it."""

    started: datetime
    rom: str

    def covers(self, started: datetime, rom: str) -> bool:
        """Already published: older than the mark, or the mark itself seen through history."""
        return started <= self.started or (rom == self.rom and abs(started - self.started) <= SAME_SESSION)


def session_of(params: Mapping[str, Any], started: datetime) -> Session | None:
    """A Session from media.started or media.active fields (ASSUMPTION Z-NOTIFY); None without a game."""
    rom = params.get("mediaPath")
    if not isinstance(rom, str) or not rom:
        return None
    game = params.get("mediaName")
    system = params.get("systemId")
    return Session(
        game=game if isinstance(game, str) and game else Path(rom).stem,
        system=system if isinstance(system, str) else "",
        rom=rom,
        started=started,
    )


class HostTracker:
    """Turns one MiSTer's Zaparoo events into MQTT messages, and remembers where it got to."""

    def __init__(self, track: TrackSettings, host: HostSettings, mark: Mark | None = None) -> None:
        self.track = track
        self.host = host
        self.base = base_topic(track, host)
        self.session: Session | None = None
        self.mark = mark
        self.activity: str | None = None

    def _who(self) -> dict[str, str]:
        return {"host": self.host.label, "device": self.host.model}

    def _activity(self, value: str) -> list[Message]:
        if value == self.activity:
            return []
        self.activity = value
        return [(f"{self.base}/activity", value, True)]

    def _now_playing(self) -> Message:
        if self.session is None:
            return (f"{self.base}/now_playing", json.dumps({"state": "idle"}), True)
        payload = {"state": "playing", **self.session.info(), "started": iso(self.session.started), **self._who()}
        return (f"{self.base}/now_playing", json.dumps(payload), True)

    def _log(self, session: Session, duration_s: int) -> list[Message]:
        """last_session and a session_end event, if it was long enough; moves the mark either way."""
        if self.mark is None or session.started > self.mark.started:
            self.mark = Mark(session.started, session.rom)
        if duration_s < self.track.min_session_s:
            return []
        record = {
            **session.info(),
            "started": iso(session.started),
            "ended": iso(session.started + timedelta(seconds=duration_s)),
            "duration_s": duration_s,
            **self._who(),
        }
        return [
            (f"{self.base}/last_session", json.dumps(record), True),
            (f"{self.base}/session", json.dumps({"event_type": "session_end", **record}), False),
        ]

    # --- live events -----------------------------------------------------------------------------
    def started(self, session: Session, now: datetime) -> list[Message]:
        out: list[Message] = []
        if self.session is not None:  # a media.stopped went missing: close the old one first
            out += self.stopped(None, now)
        self.session = session
        out.append(self._now_playing())
        # Playing already turns on, but the event also fires when the same game is relaunched, and hands
        # the game to an automation (Batocera's reasoning).
        start = {"event_type": "session_start", **session.info(), "started": iso(session.started), **self._who()}
        out.append((f"{self.base}/session", json.dumps(start), False))
        return out + self._activity("playing")

    def stopped(self, elapsed: Any, now: datetime) -> list[Message]:
        session, self.session = self.session, None
        out: list[Message] = [self._now_playing(), *self._activity("menu")]
        if session is None:
            return out
        # Zaparoo's own count when it sent one (ASSUMPTION Z-NOTIFY); else what our clock saw.
        if isinstance(elapsed, int | float) and not isinstance(elapsed, bool) and elapsed >= 0:
            duration = int(elapsed)
        else:
            duration = max(0, int((now - session.started).total_seconds()))
        return out + self._log(session, duration)

    def offline(self) -> list[Message]:
        """Zaparoo went away. Keep the session: the game may well still be running when it's back."""
        return [(f"{self.base}/status", "offline", True), *self._activity("off")]

    # --- catching up -----------------------------------------------------------------------------
    @staticmethod
    def _same(a: Session, b: Session) -> bool:
        """One session seen twice (not the same game quit and relaunched meanwhile)."""
        return a.rom == b.rom and abs(a.started - b.started) <= SAME_SESSION

    def online(self, active: Session | None, history: list[dict[str, Any]], now: datetime) -> list[Message]:
        """Zaparoo is (back) up: publish what was missed, then what's true now.

        `active` is what media.active says is running; `history` is media.history's entries
        (ASSUMPTION Z-HISTORY), sessions that have ended, including any we saw start live.
        """
        out: list[Message] = [(f"{self.base}/status", "online", True)]
        ended: list[tuple[Session, int]] = []
        for e in history:
            started, rom, play = parse_time(e.get("startedAt")), e.get("mediaPath"), e.get("playTime")
            s = session_of(e, started) if started else None
            if s is not None and isinstance(rom, str) and isinstance(play, int | float):
                ended.append((s, int(play)))
        ended.sort(key=lambda pair: pair[0].started)
        if self.mark is None:
            # First run: start from now rather than replaying all of Zaparoo's history as new events.
            if ended:
                newest, play = ended[-1]
                self.mark = Mark(newest.started, newest.rom)
                logged = [m for m in self._log(newest, play) if not m[0].endswith("/session")]
                out += logged  # last_session without an event: it isn't news
        else:
            for s, play in ended:
                if not self.mark.covers(s.started, s.rom):
                    out += self._log(s, play)
        # What's running now. A session open from before the outage either continues (same game,
        # same start) or ended meanwhile, in which case history above has already logged it.
        if self.session is not None and not (active and self._same(active, self.session)):
            self.session = None
        if active is not None and self.session is None:
            self.session = active  # no session_start: the game didn't just start, the tracker did
        out.append(self._now_playing())
        return out + self._activity("playing" if self.session else "menu")


# --- the mark, across restarts --------------------------------------------------------------------
def load_marks(path: Path) -> dict[str, Mark]:
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("Couldn't read %s (%s); catching up starts from now", path, exc)
        return {}
    marks: dict[str, Mark] = {}
    for key, value in raw.items() if isinstance(raw, dict) else ():
        started = parse_time(value.get("started")) if isinstance(value, dict) else None
        if started and isinstance(value.get("rom"), str):
            marks[key] = Mark(started, value["rom"])
    return marks


def save_marks(path: Path, marks: Mapping[str, Mark]) -> None:
    data = {k: {"started": iso(m.started), "rom": m.rom} for k, m in marks.items()}
    tmp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(path)  # atomic: a crash mid-write leaves the old file
    except OSError as exc:
        log.warning("Couldn't save %s: %s", path, exc)


# --- delivery ---------------------------------------------------------------------------------------
class Outbox:
    """Messages waiting for the broker, and the latest retained value of every topic.

    While the broker is unreachable (Home Assistant restarting, say), retained values just
    update: on reconnect the latest of each is published, never a backlog of stale states.
    Of the events, only session_end waits (a logged session isn't lost); a session_start that
    arrives late would fire its automation at the wrong time, so it's dropped (Batocera's rule).
    """

    MAX_EVENTS = 200

    def __init__(self) -> None:
        self.retained: dict[str, str] = {}
        self.queue: deque[Message] = deque()
        self.connected = False
        self.wake = asyncio.Event()

    def put(self, messages: list[Message]) -> None:
        for msg in messages:
            topic, payload, retain = msg
            if retain:
                self.retained[topic] = payload
                if not self.connected:
                    continue  # reconnected() sends the latest value of each
            elif not self.connected and '"session_end"' not in payload:
                log.info("Broker unreachable: dropping %s", topic)
                continue
            elif len(self.queue) >= self.MAX_EVENTS:
                self.queue.popleft()
            self.queue.append(msg)
        self.wake.set()

    def reconnected(self) -> list[Message]:
        """What to send first after (re)connecting: every retained value, then the waiting events."""
        events = [m for m in self.queue if not m[2]]
        self.queue.clear()
        self.connected = True
        return [(t, p, True) for t, p in self.retained.items()] + events


async def _until_disconnected(client: aiomqtt.Client) -> None:
    """Raise MqttError when the connection ends; until then, wait.

    The tracker subscribes to nothing, so this yields no messages; aiomqtt's iterator raises as soon as
    the connection drops. Without it, an idle tracker would only find out at its next publish, and a
    broker restart would leave Home Assistant without our retained states until then.
    """
    async for _ in client.messages:
        pass
    raise aiomqtt.MqttError("the MQTT connection closed")


async def deliver(track: TrackSettings, outbox: Outbox, client_id: str) -> None:
    """Hold one MQTT connection open, reconnecting with backoff, and publish from the outbox."""
    will = aiomqtt.Will(track.tracker_status, "offline", qos=1, retain=True)
    backoff = 1.0
    while True:
        first: list[Message] = []
        try:
            async with aiomqtt.Client(
                track.mqtt_host,
                track.mqtt_port,
                username=track.mqtt_user,
                password=track.mqtt_password,
                identifier=client_id,
                will=will,
                keepalive=30,
                timeout=10,
            ) as client:
                log.info("Connected to the MQTT broker at %s:%d", track.mqtt_host, track.mqtt_port)
                backoff = 1.0
                watcher = asyncio.create_task(_until_disconnected(client), name="mqtt watch")
                try:
                    await client.publish(track.tracker_status, "online", qos=1, retain=True)
                    first = outbox.reconnected()
                    while True:
                        while first:
                            topic, payload, retain = first[0]
                            await client.publish(topic, payload, qos=1, retain=retain)
                            first.pop(0)
                        while outbox.queue:
                            topic, payload, retain = outbox.queue[0]
                            await client.publish(topic, payload, qos=1, retain=retain)
                            outbox.queue.popleft()  # only once the broker has it
                        outbox.wake.clear()
                        if outbox.queue:
                            continue
                        waiter = asyncio.create_task(outbox.wake.wait())
                        done, _ = await asyncio.wait({waiter, watcher}, return_when=asyncio.FIRST_COMPLETED)
                        if watcher in done:
                            waiter.cancel()
                            watcher.result()  # raises the MqttError
                except asyncio.CancelledError:
                    # Stopping. A clean DISCONNECT doesn't trigger the last will, so say it ourselves, or
                    # Home Assistant keeps showing the last states as current.
                    with contextlib.suppress(aiomqtt.MqttError, TimeoutError):
                        async with asyncio.timeout(1.5):
                            await client.publish(track.tracker_status, "offline", qos=1, retain=True)
                    raise
                finally:
                    await stop_task(watcher, 1.0, "MQTT connection watch")
        except aiomqtt.MqttError as exc:
            outbox.connected = False
            # Events not yet sent go back in front; retained values are resent from outbox.retained anyway.
            outbox.queue.extendleft(reversed([m for m in first if not m[2]]))
            log.warning("MQTT broker %s:%d: %s; retrying in %.0fs", track.mqtt_host, track.mqtt_port, exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


# --- the service ------------------------------------------------------------------------------------
Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


Events = asyncio.Queue[tuple[str, Any]]


def _enqueue_notification(events: Events) -> Callable[[str, dict[str, Any]], None]:
    def listener(method: str, params: dict[str, Any]) -> None:
        events.put_nowait((method, params))

    return listener


def _enqueue_state(events: Events) -> Callable[[bool], None]:
    def listener(up: bool) -> None:
        events.put_nowait(("_up" if up else "_down", None))

    return listener


class Tracker:
    """One HostTracker per MiSTer, fed by its Zaparoo connection, publishing through one Outbox."""

    def __init__(self, settings: Settings, track: TrackSettings, clock: Clock = _utcnow) -> None:
        self.track = track
        self.clock = clock
        self.outbox = Outbox()
        self._marks = load_marks(track.state_file)
        self.hosts: list[tuple[HostTracker, ZaparooClient, asyncio.Queue[tuple[str, Any]]]] = []
        seen: set[str] = set()
        for h in settings.hosts:
            key = slug(h.label)
            if key in seen:  # two names that slug alike would share topics
                log.error("%s: topic name %r is taken by another MiSTer; rename one. Not tracking it.", h.label, key)
                continue
            seen.add(key)
            client = ZaparooClient(h.label, h.host, h.zaparoo_port)
            events: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
            # Notifications and connection changes arrive as callbacks; one queue per MiSTer keeps them in
            # order, and its worker may await Zaparoo (media.active) without holding up the socket.
            client.add_listener(_enqueue_notification(events))
            client.add_state_listener(_enqueue_state(events))
            self.hosts.append((HostTracker(track, h, self._marks.get(key)), client, events))
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        client_id = f"mcp-server-misterfpga-track-{socket.gethostname()}"[:64]
        self._tasks.append(asyncio.create_task(deliver(self.track, self.outbox, client_id), name="mqtt"))
        for ht, client, events in self.hosts:
            self.outbox.put(ht.offline())  # until Zaparoo answers
            self._tasks.append(asyncio.create_task(self._work(ht, client, events), name=f"track {ht.host.label}"))
            await client.start()

    async def stop(self) -> None:
        for _, client, _ in self.hosts:
            await client.stop()
        for task in self._tasks:
            await stop_task(task, 2.0, task.get_name())
        self._tasks.clear()

    def _remember(self, ht: HostTracker) -> None:
        key = slug(ht.host.label)
        if ht.mark is not None and self._marks.get(key) != ht.mark:
            self._marks[key] = ht.mark
            save_marks(self.track.state_file, self._marks)

    async def _active(self, client: ZaparooClient) -> Session | None:
        active = await client.call("media.active")  # ASSUMPTION Z-SESSION-TIMES: started is the MiSTer's
        if not isinstance(active, dict):
            return None
        return session_of(active, parse_time(active.get("started")) or self.clock())

    async def _work(self, ht: HostTracker, client: ZaparooClient, events: asyncio.Queue[tuple[str, Any]]) -> None:
        while True:
            kind, params = await events.get()
            try:
                if kind == "media.started":
                    # The MiSTer's own start time, so history can recognize this session later. If that
                    # read fails, the notification alone still makes a session, on our clock.
                    try:
                        session = await self._active(client)
                    except MisterError as exc:
                        log.info("%s: media.active failed (%s); using this machine's clock", ht.host.label, exc)
                        session = None
                    session = session or session_of(params, self.clock())
                    if session is not None:
                        self.outbox.put(ht.started(session, self.clock()))
                elif kind == "media.stopped":
                    self.outbox.put(ht.stopped(params.get("elapsed"), self.clock()))
                elif kind == "_up":
                    self.outbox.put(discovery_messages(self.track, ht.host, client.version))
                    result = await client.call("media.history", {"limit": HISTORY_LIMIT})
                    history = result.get("entries") if isinstance(result, dict) else None  # ASSUMPTION Z-HISTORY
                    rows = [e for e in history or [] if isinstance(e, dict)]
                    self.outbox.put(ht.online(await self._active(client), rows, self.clock()))
                elif kind == "_down":
                    self.outbox.put(ht.offline())
                self._remember(ht)
            except MisterError as exc:
                # Zaparoo dropped mid-catch-up; the reconnect brings another "_up" and another try.
                log.warning("%s: %s", ht.host.label, exc)


async def run(settings: Settings, track: TrackSettings, stop: asyncio.Event | None = None) -> None:
    tracker = Tracker(settings, track)
    await tracker.start()
    try:
        await (stop or asyncio.Event()).wait()
    finally:
        with contextlib.suppress(Exception):
            await tracker.stop()

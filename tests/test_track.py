"""The play tracker (track.py): session logic with a fixed clock, then end to end.

The end-to-end tests run the real Tracker against the simulated MiSTers (real Zaparoo
WebSockets) and a FakeBroker that speaks enough MQTT 3.1.1 for paho/aiomqtt: CONNECT
(with its last will), QoS 1 PUBLISH, PINGREQ, DISCONNECT. Like mcp-server-batocera's
test_playtracker FakeBroker, but asyncio, since the tracker is.
"""

from __future__ import annotations

import asyncio
import json
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from misterfpga_mcp import track
from misterfpga_mcp.config import HostSettings, load_settings
from misterfpga_mcp.track import (
    HostTracker,
    Mark,
    Outbox,
    Session,
    TrackSettings,
    discovery_messages,
    load_marks,
    load_track_settings,
    save_marks,
)

from .conftest import office, wait_until

T0 = datetime(2026, 10, 24, 19, 0, tzinfo=UTC)
OFFICE = HostSettings("192.168.1.70", name="Office", model="MiSTer Pi")
SM = Session("Super Metroid", "SNES", "/media/fat/games/SNES/Super Metroid (USA).sfc", T0)
SONIC = Session("Sonic the Hedgehog", "Genesis", "/media/fat/games/Genesis/Sonic.md", T0 + timedelta(hours=1))


def settings(tmp_path: Path, **kw: Any) -> TrackSettings:
    return TrackSettings(mqtt_host="127.0.0.1", state_file=tmp_path / "track-state.json", **kw)


def by_topic(msgs: list[tuple[str, str, bool]]) -> dict[str, Any]:
    """Last payload per topic suffix; JSON decoded where it is JSON."""
    out: dict[str, Any] = {}
    for topic, payload, _ in msgs:
        try:
            out[topic.split("/", 2)[-1]] = json.loads(payload)
        except ValueError:
            out[topic.split("/", 2)[-1]] = payload
    return out


def events(msgs: list[tuple[str, str, bool]]) -> list[tuple[str, str]]:
    return [(json.loads(p)["event_type"], json.loads(p)["game"]) for t, p, _ in msgs if t.endswith("/session")]


def entry(s: Session, play: int) -> dict[str, Any]:
    """A media.history entry (ASSUMPTION Z-HISTORY) for session `s`."""
    return {
        "mediaName": s.game,
        "systemId": s.system,
        "mediaPath": s.rom,
        "startedAt": s.started.isoformat(),
        "endedAt": (s.started + timedelta(seconds=play)).isoformat(),
        "playTime": play,
    }


# --- settings -----------------------------------------------------------------------------------
def test_settings_need_a_broker_and_say_what_is_wrong():
    none, why = load_track_settings({})
    assert none is None and "MISTERFPGA_MQTT_HOST" in why[0]
    t, problems = load_track_settings(
        {"MISTERFPGA_MQTT_HOST": "ha.local", "MISTERFPGA_MQTT_PORT": "nope", "MISTERFPGA_MQTT_TOPIC_PREFIX": "a b"}
    )
    assert t is not None and (t.mqtt_host, t.mqtt_port, t.topic_prefix, t.min_session_s) == (
        "ha.local",
        1883,
        "misterfpga",
        60,
    )
    assert sorted(p.split("=")[0] for p in problems) == ["MISTERFPGA_MQTT_PORT", "MISTERFPGA_MQTT_TOPIC_PREFIX"]


# --- discovery ----------------------------------------------------------------------------------
def test_discovery_matches_batocera_and_names_the_device(tmp_path):
    msgs = discovery_messages(settings(tmp_path), OFFICE, "2.20.0")
    configs = {t.split("/")[-2]: json.loads(p) for t, p, retain in msgs if retain}
    assert set(configs) == {
        "now_playing",
        "playing",
        "now_playing_system",
        "last_session",
        "last_session_duration",
        "game_session",
        "activity",
    }
    assert all(t.startswith("homeassistant/") and "/misterfpga_office/" in t for t, _, _ in msgs)
    np = configs["now_playing"]
    assert np["device"] == {
        "identifiers": ["misterfpga_office"],
        "name": "Office MiSTer Pi",
        "manufacturer": "MiSTer FPGA",
        "model": "MiSTer Pi",
        "sw_version": "Zaparoo Core 2.20.0",
    }
    assert np["unique_id"] == "misterfpga_office_now_playing" and np["state_topic"] == "misterfpga/office/now_playing"
    # Everything needs both this process and the MiSTer; Activity only this process, so "off" shows.
    assert np["availability_mode"] == "all"
    assert [a["topic"] for a in np["availability"]] == ["misterfpga/_tracker/status", "misterfpga/office/status"]
    assert configs["activity"]["availability_topic"] == "misterfpga/_tracker/status"
    assert "availability" not in configs["activity"]
    assert configs["activity"]["options"] == ["menu", "playing", "off"]


# --- live sessions ------------------------------------------------------------------------------
def test_a_session_from_start_to_end(tmp_path):
    ht = HostTracker(settings(tmp_path), OFFICE)
    started = ht.started(SM, T0)
    state = by_topic(started)
    assert state["now_playing"] == {
        "state": "playing",
        "game": "Super Metroid",
        "system": "SNES",
        "rom": SM.rom,
        "source": "player",
        "started": "2026-10-24T19:00:00+00:00",
        "host": "Office",
        "device": "MiSTer Pi",
    }
    assert state["activity"] == "playing" and events(started) == [("session_start", "Super Metroid")]
    ended = ht.stopped(1500, T0 + timedelta(seconds=1503))
    state = by_topic(ended)
    assert state["now_playing"] == {"state": "idle"} and state["activity"] == "menu"
    # Length is Zaparoo's elapsed; ended is started + length, both on the MiSTer's clock.
    assert state["last_session"]["duration_s"] == 1500
    assert state["last_session"]["ended"] == "2026-10-24T19:25:00+00:00"
    assert events(ended) == [("session_end", "Super Metroid")]
    assert ht.mark == Mark(SM.started, SM.rom)


def test_short_sessions_are_not_logged(tmp_path):
    ht = HostTracker(settings(tmp_path), OFFICE)
    ht.started(SM, T0)
    out = by_topic(ht.stopped(20, T0 + timedelta(seconds=20)))
    assert out["now_playing"] == {"state": "idle"} and "last_session" not in out and "session" not in out


def test_without_elapsed_our_clock_measures(tmp_path):
    ht = HostTracker(settings(tmp_path), OFFICE)
    ht.started(SM, T0)
    assert by_topic(ht.stopped(None, T0 + timedelta(minutes=5)))["last_session"]["duration_s"] == 300


def test_a_missing_stop_closes_the_old_session_first(tmp_path):
    ht = HostTracker(settings(tmp_path), OFFICE)
    ht.started(SM, T0)
    out = ht.started(SONIC, SONIC.started)
    assert events(out) == [("session_end", "Super Metroid"), ("session_start", "Sonic the Hedgehog")]
    assert by_topic(out)["now_playing"]["game"] == "Sonic the Hedgehog"


# --- outages and catching up ----------------------------------------------------------------------
def test_offline_then_back_with_the_same_game_keeps_the_session(tmp_path):
    ht = HostTracker(settings(tmp_path), OFFICE, Mark(T0 - timedelta(days=1), "old"))
    ht.started(SM, T0)
    assert by_topic(ht.offline()) == {"status": "offline", "activity": "off"}
    out = ht.online(SM, [], T0 + timedelta(minutes=5))
    assert events(out) == []  # no second session_start: the game didn't start again
    assert by_topic(out)["now_playing"]["started"] == "2026-10-24T19:00:00+00:00"
    assert by_topic(out)["activity"] == "playing" and by_topic(out)["status"] == "online"


def test_a_session_that_ended_during_the_outage_is_logged_from_history_once(tmp_path):
    ht = HostTracker(settings(tmp_path), OFFICE, Mark(T0 - timedelta(days=1), "old"))
    ht.started(SM, T0)
    ht.offline()
    # Zaparoo's history entry starts a few seconds earlier than media.active said: still one session.
    history = [entry(Session(SM.game, SM.system, SM.rom, T0 - timedelta(seconds=3)), 900)]
    out = ht.online(None, history, T0 + timedelta(hours=1))
    assert events(out) == [("session_end", "Super Metroid")]
    assert by_topic(out)["now_playing"] == {"state": "idle"} and by_topic(out)["activity"] == "menu"
    # The next reconnect sees the same history and logs nothing.
    assert events(ht.online(None, history, T0 + timedelta(hours=2))) == []


def test_catching_up_logs_only_what_is_newer_than_the_mark(tmp_path):
    older = Session("Contra", "NES", "/media/fat/games/NES/Contra.nes", T0 - timedelta(hours=2))
    ht = HostTracker(settings(tmp_path), OFFICE, Mark(T0 - timedelta(hours=1), "x"))
    history = [entry(SONIC, 600), entry(older, 600), entry(SM, 30)]  # newest first, as Zaparoo lists them
    out = ht.online(None, history, T0 + timedelta(hours=3))
    assert events(out) == [("session_end", "Sonic the Hedgehog")]  # SM is too short; Contra was published
    assert ht.mark == Mark(SONIC.started, SONIC.rom)


def test_the_same_game_relaunched_during_the_outage_is_two_sessions(tmp_path):
    ht = HostTracker(settings(tmp_path), OFFICE, Mark(T0 - timedelta(days=1), "old"))
    ht.started(SM, T0)
    again = Session(SM.game, SM.system, SM.rom, T0 + timedelta(minutes=30))
    out = ht.online(again, [entry(SM, 1200)], T0 + timedelta(minutes=31))
    assert events(out) == [("session_end", "Super Metroid")]
    assert ht.session == again and by_topic(out)["now_playing"]["started"] == again.started.isoformat()


def test_first_run_starts_from_now(tmp_path):
    ht = HostTracker(settings(tmp_path), OFFICE)
    out = ht.online(None, [entry(SONIC, 600), entry(SM, 900)], T0 + timedelta(hours=3))
    assert events(out) == []  # no replay of old history as new events
    assert by_topic(out)["last_session"]["game"] == "Sonic the Hedgehog"
    assert ht.mark == Mark(SONIC.started, SONIC.rom)


def test_marks_survive_a_restart_and_a_bad_file_is_not_fatal(tmp_path):
    path = tmp_path / "s" / "track-state.json"
    save_marks(path, {"office": Mark(T0, SM.rom)})
    assert load_marks(path) == {"office": Mark(T0, SM.rom)}
    path.write_text("{not json")
    assert load_marks(path) == {}


# --- delivery -------------------------------------------------------------------------------------
def test_outbox_keeps_latest_states_and_logged_sessions_while_the_broker_is_away():
    box = Outbox()
    start = ("m/office/session", json.dumps({"event_type": "session_start", "game": "A"}), False)
    end = ("m/office/session", json.dumps({"event_type": "session_end", "game": "A"}), False)
    box.put([("m/office/now_playing", "playing", True), start, ("m/office/now_playing", "idle", True), end])
    assert list(box.queue) == [end]  # states wait in `retained`; a late session_start is dropped
    assert box.reconnected() == [("m/office/now_playing", "idle", True), end]
    box.put([start])
    assert list(box.queue) == [start]  # connected now: events go straight through


# --- end to end: simulated MiSTers, the real Tracker, a fake broker --------------------------------
def _remaining(data: bytes, i: int) -> tuple[int, int]:
    mult, value = 1, 0
    while True:
        b = data[i]
        i += 1
        value += (b & 0x7F) * mult
        mult *= 128
        if not b & 0x80:
            return value, i


class FakeBroker:
    """Enough MQTT 3.1.1 for aiomqtt: records CONNECTs (with the will) and every PUBLISH."""

    def __init__(self) -> None:
        self.connects: list[dict[str, Any]] = []
        self.published: list[tuple[str, str, bool]] = []
        self.port = 0
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        await self.drop()
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def drop(self) -> None:
        """Close every client connection, as a broker restart would."""
        for w in self._writers:
            w.close()
        self._writers.clear()

    def state(self, suffix: str) -> Any:
        """The last payload published to a topic ending in `suffix`."""
        for topic, payload, _ in reversed(self.published):
            if topic.endswith(suffix):
                try:
                    return json.loads(payload)
                except ValueError:
                    return payload
        return None

    def events(self) -> list[tuple[str, str]]:
        return [
            (json.loads(p)["event_type"], json.loads(p)["game"]) for t, p, _ in self.published if t.endswith("/session")
        ]

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        try:
            while True:
                header = (await reader.readexactly(1))[0]
                raw = b""
                while True:
                    raw += await reader.readexactly(1)
                    if not raw[-1] & 0x80:
                        break
                length, _ = _remaining(raw, 0)
                body = await reader.readexactly(length)
                kind = header >> 4
                if kind == 1:  # CONNECT
                    self.connects.append(self._parse_connect(body))
                    writer.write(b"\x20\x02\x00\x00")
                elif kind == 3:  # PUBLISH
                    n = struct.unpack("!H", body[:2])[0]
                    topic = body[2 : 2 + n].decode()
                    qos = (header >> 1) & 3
                    rest = body[2 + n :]
                    if qos:
                        writer.write(b"\x40\x02" + rest[:2])
                        rest = rest[2:]
                    self.published.append((topic, rest.decode(), bool(header & 1)))
                elif kind == 12:  # PINGREQ
                    writer.write(b"\xd0\x00")
                elif kind == 14:  # DISCONNECT
                    return
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        finally:
            writer.close()

    @staticmethod
    def _parse_connect(body: bytes) -> dict[str, Any]:
        n = struct.unpack("!H", body[:2])[0]
        flags = body[2 + n + 1]
        rest = body[2 + n + 4 :]
        fields: list[bytes] = []
        while rest:
            ln = struct.unpack("!H", rest[:2])[0]
            fields.append(rest[2 : 2 + ln])
            rest = rest[2 + ln :]
        out: dict[str, Any] = {"client_id": fields[0].decode()}
        if flags & 0x04:
            out["will"] = (fields[1].decode(), fields[2].decode(), bool(flags & 0x20))
        if flags & 0x80:
            out["user"] = fields[3 if flags & 0x04 else 1].decode()
        return out


@pytest.fixture
async def broker():
    b = FakeBroker()
    await b.start()
    try:
        yield b
    finally:
        await b.stop()


async def tracking(tmp_path: Path, broker: FakeBroker) -> track.Tracker:
    t = track.Tracker(load_settings(), settings(tmp_path, mqtt_port=broker.port, min_session_s=0))
    await t.start()
    return t


@pytest.mark.anyio
async def test_end_to_end_menu_launch_to_home_assistant(configured, broker, tmp_path):
    tracker = await tracking(tmp_path, broker)
    try:
        await wait_until(
            lambda: broker.state("family_room/activity") == "menu" and broker.state("office/activity") == "menu"
        )
        connect = broker.connects[0]
        assert connect["will"] == ("misterfpga/_tracker/status", "offline", True)
        assert broker.state("_tracker/status") == "online" and broker.state("office/status") == "online"
        devices = {json.loads(p)["device"]["name"] for t, p, _ in broker.published if t.endswith("/now_playing/config")}
        assert devices == {"Office MiSTer Pi", "Family Room SuperStation One"}

        sim_office = office(configured)
        await sim_office.zaparoo.play(101)  # Super Metroid, picked from the MiSTer's own menu
        await wait_until(lambda: broker.events() == [("session_start", "Super Metroid")])
        playing = broker.state("office/now_playing")
        assert playing["state"] == "playing" and playing["host"] == "Office" and playing["device"] == "MiSTer Pi"
        assert playing["started"] == sim_office.device.active["started"]  # the MiSTer's clock
        assert broker.state("office/activity") == "playing"

        await sim_office.zaparoo.quit_game()
        await wait_until(lambda: len(broker.events()) == 2)
        assert broker.events()[1] == ("session_end", "Super Metroid")
        assert broker.state("office/now_playing") == {"state": "idle"}
        assert broker.state("office/last_session")["game"] == "Super Metroid"
    finally:
        await tracker.stop()
    # A clean stop sends no last will, so the tracker says it's going itself.
    assert broker.state("_tracker/status") == "offline"


@pytest.mark.anyio
async def test_a_mister_dropping_off_and_back(configured, broker, tmp_path):
    tracker = await tracking(tmp_path, broker)
    try:
        await wait_until(lambda: broker.state("office/activity") == "menu")
        await office(configured).zaparoo.drop_connections()
        await wait_until(lambda: broker.state("office/status") == "offline")
        assert broker.state("office/activity") == "off"
        await wait_until(lambda: broker.state("office/status") == "online", timeout=10)  # reconnect backoff: 1 s
        assert broker.state("office/activity") == "menu"
    finally:
        await tracker.stop()


@pytest.mark.anyio
async def test_sessions_played_while_the_tracker_was_down_are_caught_up(configured, broker, tmp_path):
    tracker = await tracking(tmp_path, broker)
    sim_office = office(configured)
    await wait_until(lambda: broker.state("office/activity") == "menu")
    await sim_office.zaparoo.play(101)
    await sim_office.zaparoo.quit_game()
    await wait_until(lambda: ("session_end", "Super Metroid") in broker.events())
    await tracker.stop()

    # While it's down: Sonic, played and quit. Zaparoo keeps it in its history (and only there).
    sonic = sim_office.device.by_id(104)
    assert sonic is not None
    sim_office.device.start_game(sonic)["started"] = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    sim_office.device.to_menu()
    published = len(broker.published)
    tracker = await tracking(tmp_path, broker)
    try:
        await wait_until(lambda: any(t.endswith("/session") for t, _, _ in broker.published[published:]))
        await asyncio.sleep(0.3)  # anything else would have arrived by now
        new = [json.loads(p) for t, p, _ in broker.published[published:] if t.endswith("/session")]
        # Sonic, once; Super Metroid was published live before the restart and isn't sent again.
        assert [(e["event_type"], e["game"]) for e in new] == [("session_end", "Sonic the Hedgehog")]
    finally:
        await tracker.stop()


@pytest.mark.anyio
async def test_states_are_resent_after_the_broker_restarts(configured, broker, tmp_path):
    tracker = await tracking(tmp_path, broker)
    try:
        await wait_until(lambda: broker.state("office/activity") == "menu")
        await broker.drop()
        before = len(broker.connects)
        await wait_until(lambda: len(broker.connects) > before, timeout=10)
        # Everything retained comes again, so a broker without persistence is right after its restart.
        await wait_until(lambda: sum(1 for t, _, _ in broker.published if t == "misterfpga/office/activity") >= 2)
    finally:
        await tracker.stop()

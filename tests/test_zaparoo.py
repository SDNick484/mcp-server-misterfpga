"""ZaparooClient against the fake Core on a real socket: framing, errors, reconnects, and a MiSTer that's off."""

from __future__ import annotations

import asyncio
import socket

import pytest

from misterfpga_mcp import zaparoo
from misterfpga_mcp.errors import MisterError, Unavailable, ZaparooError
from misterfpga_mcp.zaparoo import ZaparooClient

from .conftest import connect, error, office, ok, wait_until

pytestmark = pytest.mark.anyio


async def started(port: int, host: str = "127.0.0.1") -> ZaparooClient:
    c = ZaparooClient("Office", host, port)
    await c.start()
    await asyncio.wait_for(c.tried.wait(), 5)
    return c


async def test_requests_match_their_responses_concurrently(sim):
    c = await started(office(sim).zaparoo.port)
    try:
        version, systems, active = await asyncio.gather(c.call("version"), c.call("systems"), c.call("media.active"))
        assert version == {"version": "2.20.0", "platform": "mister"}
        assert len(systems["systems"]) == 6 and active is None
        assert c.version == "2.20.0"
    finally:
        await c.stop()


async def test_error_replies_carry_category_and_reason(sim):
    c = await started(office(sim).zaparoo.port)
    try:
        with pytest.raises(ZaparooError) as info:
            await c.call("run", {"text": "**launch.system:PSX"})
        assert (info.value.category, info.value.reason) == ("launch_repair", "launcher_plugin_missing")
    finally:
        await c.stop()


async def test_methods_outside_the_allow_list_are_never_sent(sim):
    c = await started(office(sim).zaparoo.port)
    try:
        with pytest.raises(ValueError, match="not a Zaparoo method this server sends"):
            await c.call("settings.update", {"encryption": True})
        assert "settings.update" not in office(sim).zaparoo.methods
    finally:
        await c.stop()


async def test_reconnects_after_core_restarts(sim, monkeypatch):
    monkeypatch.setattr(zaparoo, "BACKOFF_MAX", 0.2)
    c = await started(office(sim).zaparoo.port)
    try:
        await office(sim).zaparoo.drop_connections()
        await wait_until(lambda: not c.connected)
        with pytest.raises(Unavailable):
            await c.call("version")
        await wait_until(lambda: c.connected, timeout=5)
        assert (await c.call("version"))["version"] == "2.20.0"
    finally:
        await c.stop()


async def test_a_hung_core_times_out_with_a_useful_message(sim):
    c = await started(office(sim).zaparoo.port)
    try:
        office(sim).zaparoo.silent = True
        with pytest.raises(MisterError, match="didn't answer media within 0s"):
            await c.call("media", timeout=0.2)
    finally:
        await c.stop()


async def test_scanned_cards_are_remembered_and_listeners_survive_bugs(sim):
    c = await started(office(sim).zaparoo.port)
    seen: list[str] = []

    def broken(method: str, params: dict) -> None:
        raise RuntimeError("listener bug")

    c.add_listener(broken)
    c.add_listener(lambda method, params: seen.append(method))
    try:
        await office(sim).zaparoo.scan_card("04aa")
        await wait_until(lambda: c.last_token is not None)
        assert c.last_token.uid == "04aa" and seen == ["tokens.added"]
        assert c.connected  # the broken listener didn't take the connection down
    finally:
        await c.stop()


async def test_ping_is_answered_with_pong(sim, monkeypatch):
    monkeypatch.setattr(zaparoo, "PING_EVERY", 0.05)
    c = await started(office(sim).zaparoo.port)
    try:
        await wait_until(lambda: office(sim).zaparoo.pings >= 2)
        assert c.connected
    finally:
        await c.stop()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_nothing_listening_says_zaparoo_isnt_running():
    c = await started(free_port())
    try:
        assert not c.connected
        assert "Zaparoo Core isn't running on Office (connection refused" in (c.error or "")
        assert "Update All" in (c.error or "")
    finally:
        await c.stop()


async def test_one_mister_off_doesnt_hide_the_other(sim, tmp_path, backup_dir):
    await office(sim).zaparoo.stop()  # the Office MiSTer's Zaparoo is gone
    sim.write_config(tmp_path / "config", backup_dir=backup_dir)
    c = await connect()
    try:
        body = ok(await c.call_tool("search_games", {"query": "metroid"}))
        assert [r["host"] for r in body["results"]] == ["Family Room"]
        assert body["unavailable"][0].startswith("Office: Zaparoo Core isn't running on Office")
        status = {h["name"]: h for h in ok(await c.call_tool("get_status", {}))["hosts"]}
        office_status = status["Office"]
        assert office_status["zaparoo"]["connected"] is False and office_status["reachable"] is True  # SSH works
        assert "search" not in office_status["capabilities"]
        assert any("Update All" in limit for limit in office_status["limitations"])
        # With one MiSTer named, its error is the answer.
        assert "isn't running on Office" in error(await c.call_tool("search_games", {"query": "x", "host": "Office"}))
        # Without Zaparoo, return_to_menu falls back to MiSTer's own command over SSH (ASSUMPTION M-CMD).
        office(sim).device.start_core("SNES")
        body = ok(await c.call_tool("return_to_menu", {"host": "Office"}))
        assert body["results"][0]["outcome"] == "done" and office(sim).device.core == "MENU"
    finally:
        await c.__aexit__(None, None, None)

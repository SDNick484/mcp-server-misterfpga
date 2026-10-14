"""The safety model (docs/DESIGN.md §8): what this server can never do, however a tool is called.

- Zaparoo: only allow-listed methods are ever sent, and no tool argument becomes ZapScript.
- SSH: only the fixed commands run; SFTP stays inside its paths; the host key is pinned.
- Rate limits refuse runaway loops whole.
"""

from __future__ import annotations

from pathlib import Path

import asyncssh
import pytest

from misterfpga_mcp.config import SshSettings
from misterfpga_mcp.errors import MisterError, Unavailable
from misterfpga_mcp.ratelimit import TokenBucket
from misterfpga_mcp.ssh import Command, MisterShell, check_path
from misterfpga_mcp.zaparoo import ALLOWED_METHODS

from . import test_tools
from .conftest import error, office, ok

pytestmark = pytest.mark.anyio


def test_the_zaparoo_allow_list_is_exactly_this():
    # Widening this is a deliberate, reviewed change: update DESIGN.md §8 and this test together.
    expected = {
        "version", "media", "media.search", "media.meta", "media.active", "media.history", "media.history.top",
        "systems", "launchers", "run", "stop", "screenshot", "readers", "readers.write", "mappings",
        "mappings.new", "tokens",
    }  # fmt: skip
    assert expected == ALLOWED_METHODS
    for never in ("settings.update", "device.power.reboot", "update.apply", "input.keyboard", "media.generate"):
        assert never not in ALLOWED_METHODS


def test_the_ssh_commands_are_exactly_these():
    assert {c.value for c in Command} == {
        "cat /tmp/CORENAME",
        "ps -o args",
        "df -k /media/fat",
        "hostname",
        "echo load_core /media/fat/menu.rbf > /dev/MiSTer_cmd",
    }


async def test_a_full_session_sends_only_allowed_methods_and_commands(mcp_client, configured):
    """Every tool, then: did anything outside the allow-lists reach the MiSTers?"""
    calls = [
        ("get_status", {}),
        ("list_systems", {}),
        ("search_games", {"query": "a"}),
        ("play_history", {"mode": "top"}),
        ("get_ini", {"host": "Office"}),
        ("list_nfc", {"host": "Office"}),
        ("launch", {"game": "Office/101"}),
        ("screenshot", {}),
        ("return_to_menu", {}),
        ("set_ini", {"host": "Office", "key": "recents", "value": 0}),
        ("backup_saves", {"host": "Office"}),
        ("map_nfc_card", {"game": "Office/103", "uid": "04112233"}),
        ("write_nfc_card", {"game": "Office/102"}),
        ("run_update", {"host": "Office", "confirm": True}),
    ]
    assert {name for name, _ in calls} == test_tools.TOOL_NAMES
    for name, args in calls:
        ok(await mcp_client.call_tool(name, args))
    for m in configured.misters:
        assert set(m.zaparoo.methods) <= ALLOWED_METHODS
        assert m.ssh.rejected == []


@pytest.mark.parametrize(
    "game",
    ["**execute:reboot", "Office/101; reboot", "@SNES/Super Metroid", "Office/../101", "Office/-1", "Office/1e3"],
)
async def test_a_game_must_be_a_ref_never_zapscript_or_a_path(mcp_client, configured, game):
    assert "isn't a game ref" in error(await mcp_client.call_tool("launch", {"game": game}))
    assert "run" not in office(configured).zaparoo.methods


async def test_system_ids_cant_carry_zapscript(mcp_client, configured):
    msg = error(await mcp_client.call_tool("launch", {"system": "SNES||**execute:reboot", "host": "Office"}))
    assert "has no system" in msg
    assert "run" not in office(configured).zaparoo.methods


async def test_launch_runs_exactly_the_libraries_own_zapscript(mcp_client, configured):
    ok(await mcp_client.call_tool("launch", {"game": "Office/107"}))
    runs = [p for m, p in office(configured).zaparoo.received if m == "run"]
    assert runs == [{"text": "@Arcade/Street Fighter II (region:jp)"}]  # from media.meta, never from the caller


@pytest.mark.parametrize(
    ("path", "purpose"),
    [
        ("/media/fat/MiSTer.ini/../linux/u-boot.txt", "read"),
        ("/media/fat/linux/user-startup.sh", "write"),
        ("/media/fat/MiSTer_example.ini", "write"),
        ("/media/fat/MiSTer.ini", "remove"),
        ("/media/fat/saves/../Scripts/x.sh", "read"),
        ("/media/fat/saves/SNES/x.sav", "write"),
        ("/etc/shadow", "read"),
        ("/media/fat//MiSTer.ini", "read"),
    ],
)
def test_sftp_paths_outside_the_policy_are_refused(path, purpose):
    with pytest.raises(MisterError, match="refusing"):
        check_path(path, purpose)


@pytest.mark.parametrize(
    ("path", "purpose"),
    [
        ("/media/fat/MiSTer.ini", "read"),
        ("/media/fat/MiSTer_crt.ini", "write"),
        ("/media/fat/MiSTer.ini.20261013T201500Z.bak", "remove"),
        ("/media/fat/saves/SNES/Super Metroid (USA).sav", "read"),
    ],
)
def test_sftp_paths_inside_the_policy(path, purpose):
    assert check_path(path, purpose) == path


async def test_a_changed_host_key_is_refused(configured):
    m = office(configured)
    wrong = asyncssh.generate_private_key("ssh-ed25519").export_public_key("openssh").decode()
    shell = MisterShell("Office", m.address, SshSettings(port=m.ssh.port, key=str(configured.key_path), host_key=wrong))
    configured.client_key.write_private_key(str(configured.key_path))
    with pytest.raises(Unavailable, match="doesn't match the one pinned"):
        await shell.run(Command.CORENAME)
    assert m.ssh.commands == []


async def test_no_pinned_host_key_means_no_connection(configured):
    m = office(configured)
    shell = MisterShell("Office", m.address, SshSettings(port=m.ssh.port, key="/nonexistent"))
    with pytest.raises(Unavailable, match="no pinned host key"):
        await shell.run(Command.CORENAME)


async def test_a_key_the_mister_doesnt_know_is_refused(configured, tmp_path: Path):
    m = office(configured)
    stranger = tmp_path / "stranger"
    asyncssh.generate_private_key("ssh-ed25519").write_private_key(str(stranger))
    shell = MisterShell(
        "Office", m.address, SshSettings(port=m.ssh.port, key=str(stranger), host_key=m.ssh.public_host_key)
    )
    with pytest.raises(Unavailable, match="refused the SSH key"):
        await shell.run(Command.CORENAME)


async def test_runaway_launches_are_refused_whole(mcp_client, configured):
    from misterfpga_mcp import server

    now = [0.0]
    h = server.registry().candidates("Office")[0]
    h.launch_bucket = TokenBucket(2, 1 / 15, clock=lambda: now[0])
    ok(await mcp_client.call_tool("launch", {"game": "Office/101"}))
    ok(await mcp_client.call_tool("return_to_menu", {}))
    msg = error(await mcp_client.call_tool("launch", {"game": "Office/102"}))
    assert msg.startswith("Error executing tool launch: Rate limit on Office: refusing to launch Chrono Trigger")
    assert office(configured).device.active is None  # refused before anything was sent
    now[0] = 15.0
    ok(await mcp_client.call_tool("launch", {"game": "Office/102"}))


async def test_dry_run_changes_nothing(configured, monkeypatch):
    from .conftest import connect

    monkeypatch.setenv("MISTERFPGA_DRY_RUN", "1")
    c = await connect()
    try:
        assert ok(await c.call_tool("get_status", {}))["dry_run"] is True
        for name, args in [
            ("launch", {"game": "Office/101"}),
            ("set_ini", {"host": "Office", "key": "recents", "value": 0}),
            ("map_nfc_card", {"game": "Office/103", "uid": "04112233"}),
            ("write_nfc_card", {"game": "Office/102"}),
            ("run_update", {"host": "Office", "confirm": True}),
            ("backup_saves", {"host": "Office"}),
        ]:
            body = ok(await c.call_tool(name, args))
            assert body.get("outcome") == "dry_run", (name, body)
    finally:
        await c.__aexit__(None, None, None)
    d = office(configured).device
    assert d.active is None and d.scripts_run == [] and d.cards_written == [] and len(d.mappings) == 1
    assert {"run", "mappings.new", "readers.write"}.isdisjoint(office(configured).zaparoo.methods)

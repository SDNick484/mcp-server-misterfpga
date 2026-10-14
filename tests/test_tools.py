"""MCP tools through a real MCP client connected in-process, against two simulated MiSTers.

This tests the contract the model actually sees: tools/list (names, titles,
annotations, input and output schemas), argument validation, and results.
"""

from __future__ import annotations

import base64

import pytest
from mcp import Client

from misterfpga_mcp.sim.device import MENU

from .conftest import error, family, office, ok

pytestmark = pytest.mark.anyio

TOOL_NAMES = {
    "get_status",
    "list_systems",
    "search_games",
    "play_history",
    "get_ini",
    "list_nfc",
    "launch",
    "return_to_menu",
    "screenshot",
    "set_ini",
    "backup_saves",
    "run_update",
    "write_nfc_card",
    "map_nfc_card",
}
READ = {"get_status", "list_systems", "search_games", "play_history", "get_ini", "list_nfc"}
DESTRUCTIVE = {"return_to_menu", "run_update", "write_nfc_card"}


async def tools(c: Client) -> dict:
    return {t.name: t for t in (await c.list_tools()).tools}


# --- tools/list ----------------------------------------------------------------------------
async def test_tool_names(mcp_client):
    assert set(await tools(mcp_client)) == TOOL_NAMES


async def test_every_tool_has_title_description_and_explicit_annotations(mcp_client):
    # Without annotations a client must assume the worst. State them all.
    for t in (await tools(mcp_client)).values():
        assert t.title and t.description, t.name
        a = t.annotations
        assert a is not None and a.read_only_hint is not None and a.open_world_hint is False, t.name
        if not a.read_only_hint:
            assert a.destructive_hint is not None and a.idempotent_hint is not None, t.name


async def test_annotations_match_behavior(mcp_client):
    t = await tools(mcp_client)
    assert {n for n, x in t.items() if x.annotations.read_only_hint} == READ
    assert {n for n, x in t.items() if x.annotations.destructive_hint} == DESTRUCTIVE
    assert t["launch"].annotations.idempotent_hint is True  # a second identical launch is refused, not repeated
    assert t["screenshot"].annotations.read_only_hint is False  # it writes a PNG on the SD card


async def test_run_update_requires_host_and_confirmation(mcp_client):
    schema = (await tools(mcp_client))["run_update"].input_schema
    assert set(schema["required"]) == {"host", "confirm"}
    assert schema["properties"]["confirm"].get("const") is True or schema["properties"]["confirm"].get("enum") == [True]


async def test_search_limit_is_bounded(mcp_client):
    props = (await tools(mcp_client))["search_games"].input_schema["properties"]
    assert (props["limit"]["minimum"], props["limit"]["maximum"]) == (1, 50)
    assert props["query"]["maxLength"] == 100


async def test_get_status_publishes_output_schema(mcp_client):
    schema = (await tools(mcp_client))["get_status"].output_schema
    host = schema["$defs"]["HostStatus"]
    assert {"name", "model", "now_playing", "capabilities", "limitations", "attract_mode"} <= set(host["required"])


# --- reads ---------------------------------------------------------------------------------
async def test_get_status_lists_both_misters_with_models(mcp_client):
    body = ok(await mcp_client.call_tool("get_status", {}))
    assert body["dry_run"] is False
    hosts = {h["name"]: h for h in body["hosts"]}
    assert set(hosts) == {"Office", "Family Room"}
    assert hosts["Office"]["model"] == "MiSTer Pi"
    assert hosts["Family Room"]["model"] == "SuperStation One"
    office_status = hosts["Office"]
    assert office_status["zaparoo"] == {"connected": True, "detail": "2.20.0"}
    assert office_status["core"] == MENU and office_status["attract_mode"] is False
    assert office_status["now_playing"] is None
    assert office_status["library"] == {"indexed": True, "indexing": False, "total_media": 8}
    assert office_status["free_space_mb"] > 0
    assert "backup_saves" in office_status["capabilities"] and office_status["limitations"] == []


async def test_get_status_one_host_by_any_spelling(mcp_client):
    body = ok(await mcp_client.call_tool("get_status", {"host": "family room"}))
    assert [h["name"] for h in body["hosts"]] == ["Family Room"]


async def test_unknown_host_names_the_real_ones(mcp_client):
    assert "MiSTers: Office, Family Room" in error(await mcp_client.call_tool("get_status", {"host": "Garage"}))


async def test_search_finds_the_same_game_on_both_misters_with_different_refs(mcp_client):
    body = ok(await mcp_client.call_tool("search_games", {"query": "super metroid"}))
    assert {(r["host"], r["ref"]) for r in body["results"]} == {
        ("Office", "Office/101"),
        ("Family Room", "Family Room/4812"),
    }
    assert body["unavailable"] == [] and body["truncated"] is False


async def test_search_tells_variants_apart(mcp_client):
    body = ok(await mcp_client.call_tool("search_games", {"query": "street fighter", "host": "Office"}))
    assert sorted(r["variant"] for r in body["results"]) == ["region:jp", "region:world"]


async def test_search_by_system_and_truncation(mcp_client):
    body = ok(await mcp_client.call_tool("search_games", {"query": "s", "system": "snes", "limit": 2}))
    assert all(r["system"] == "SNES" for r in body["results"])
    assert body["truncated"] is True


async def test_list_systems_shows_cores(mcp_client):
    body = ok(await mcp_client.call_tool("list_systems", {"host": "Office"}))
    by_id = {s["id"]: s for s in body["systems"]}
    assert by_id["SNES"]["core"] == "_Console/SNES" and by_id["SNES"]["games"] == 3
    assert by_id["ao486"]["games"] == 0  # a computer core: launch(system=...) opens it


async def test_play_history_top_and_recent_merge_hosts(mcp_client):
    top = ok(await mcp_client.call_tool("play_history", {"mode": "top"}))["entries"]
    assert top[0]["game"] == "Super Metroid" and top[0]["play_time_s"] == 7140 and top[0]["sessions"] == 2
    recent = ok(await mcp_client.call_tool("play_history", {}))["entries"]
    assert recent[0]["game"] == "Gunstar Heroes"  # newest first, across both MiSTers
    assert recent[0]["ref"] == "Family Room/4901"


# --- launch / menu -------------------------------------------------------------------------
async def test_launch_by_ref_then_status_shows_it(mcp_client, configured):
    body = ok(await mcp_client.call_tool("launch", {"game": "Family Room/4812"}))
    assert body["outcome"] == "done" and body["warnings"] == []
    assert family(configured).device.core == "SNES"
    status = ok(await mcp_client.call_tool("get_status", {"host": "Family Room"}))["hosts"][0]
    assert status["now_playing"]["game"] == "Super Metroid" and status["now_playing"]["ref"] == "Family Room/4812"
    assert status["core"] == "SNES"


async def test_launch_refuses_to_replace_a_running_game(mcp_client, configured):
    ok(await mcp_client.call_tool("launch", {"game": "Office/101"}))
    msg = error(await mcp_client.call_tool("launch", {"game": "Office/102"}))
    assert "Office is running Super Metroid" in msg and "return_to_menu" in msg
    assert office(configured).device.active["mediaName"] == "Super Metroid"  # untouched


async def test_launch_a_core_without_a_game(mcp_client, configured):
    body = ok(await mcp_client.call_tool("launch", {"system": "ao486", "host": "Office"}))
    assert body["detail"] == "Launched the ao486 core on Office."
    assert office(configured).device.core == "ao486"


async def test_launch_system_needs_a_host_when_there_are_two(mcp_client):
    assert "Pass host: Office, Family Room" in error(await mcp_client.call_tool("launch", {"system": "SNES"}))


async def test_launch_unknown_system(mcp_client):
    msg = error(await mcp_client.call_tool("launch", {"system": "Jaguar", "host": "Office"}))
    assert "Office has no system 'Jaguar'" in msg


async def test_launch_missing_core_is_explained(mcp_client):
    msg = error(await mcp_client.call_tool("launch", {"game": "Office/108"}))
    assert "core for Castlevania Symphony of the Night isn't installed on Office" in msg


async def test_stale_ref_says_search_again(mcp_client):
    assert "search again" in error(await mcp_client.call_tool("launch", {"game": "Office/999"}))


async def test_a_ref_and_a_contradicting_host_is_refused(mcp_client):
    msg = error(await mcp_client.call_tool("launch", {"game": "Office/101", "host": "Family Room"}))
    assert "is a game on Office, but host='Family Room' was given" in msg


async def test_return_to_menu_finds_the_one_running_mister(mcp_client, configured):
    ok(await mcp_client.call_tool("launch", {"game": "Family Room/4901"}))
    body = ok(await mcp_client.call_tool("return_to_menu", {}))
    assert [(r["host"], r["outcome"]) for r in body["results"]] == [("Family Room", "done")]
    assert family(configured).device.core == MENU


async def test_return_to_menu_with_two_running_asks(mcp_client):
    ok(await mcp_client.call_tool("launch", {"game": "Office/101"}))
    ok(await mcp_client.call_tool("launch", {"game": "Family Room/4812"}))
    msg = error(await mcp_client.call_tool("return_to_menu", {}))
    assert "Games are running on Office, Family Room; pass host" in msg


async def test_return_to_menu_when_nothing_runs(mcp_client):
    body = ok(await mcp_client.call_tool("return_to_menu", {}))
    assert body["results"][0]["outcome"] == "unchanged"
    body = ok(await mcp_client.call_tool("return_to_menu", {"host": "Office"}))
    assert body["results"][0] == {
        "host": "Office",
        "outcome": "unchanged",
        "detail": "Office is already in the menu.",
        "warnings": [],
    }


# --- screenshot ----------------------------------------------------------------------------
async def test_screenshot_returns_an_image(mcp_client):
    result = await mcp_client.call_tool("screenshot", {"host": "Office"})
    assert not result.is_error
    image = result.content[0]
    assert image.type == "image" and image.mime_type == "image/png"
    assert base64.b64decode(image.data).startswith(b"\x89PNG")
    assert result.structured_content == {"host": "Office", "saved_path": "/media/fat/screenshots/MiSTer_sim.png"}


# --- update --------------------------------------------------------------------------------
async def test_run_update_runs_the_script_on_screen(mcp_client, configured):
    body = ok(await mcp_client.call_tool("run_update", {"host": "Family Room", "confirm": True}))
    assert "may reboot" in body["detail"]
    assert family(configured).device.scripts_run == ["update_all.sh"]


async def test_run_update_refuses_while_a_game_runs(mcp_client, configured):
    ok(await mcp_client.call_tool("launch", {"game": "Office/101"}))
    msg = error(await mcp_client.call_tool("run_update", {"host": "Office", "confirm": True}))
    assert "return to the menu first" in msg
    assert office(configured).device.scripts_run == []


async def test_run_update_without_confirm_is_rejected_by_the_schema(mcp_client, configured):
    result = await mcp_client.call_tool("run_update", {"host": "Office", "confirm": False})
    assert result.is_error
    assert office(configured).device.scripts_run == []


# --- attract mode --------------------------------------------------------------------------
async def test_attract_mode_shows_in_status(mcp_client, configured):
    device = office(configured).device
    device.attract = True  # MiSTer Super Attract Mode's loop_core is running (ASSUMPTION S-ACTIVE)
    device.start_game(device.by_id(104))
    status = ok(await mcp_client.call_tool("get_status", {"host": "Office"}))["hosts"][0]
    assert status["attract_mode"] is True and status["now_playing"]["game"] == "Sonic the Hedgehog"
    # Picking up a controller: SAM exits and reloads the game, which is now really being played
    # (ASSUMPTION S-HANDOFF). The play tracker (phase 2) logs only sessions that start with attract off.
    device.attract = False
    device.start_game(device.by_id(104))
    status = ok(await mcp_client.call_tool("get_status", {"host": "Office"}))["hosts"][0]
    assert status["attract_mode"] is False and status["now_playing"]["game"] == "Sonic the Hedgehog"


async def test_play_history_limit_applies_to_the_merged_list(mcp_client):
    entries = ok(await mcp_client.call_tool("play_history", {"limit": 1}))["entries"]
    assert [e["game"] for e in entries] == ["Gunstar Heroes"]

"""NFC: writing a game to a card, mapping a card's UID to a game, and the guards on both (ASSUMPTION Z-NFC)."""

from __future__ import annotations

import pytest

from misterfpga_mcp.host import normalize_uid

from .conftest import error, family, office, ok

pytestmark = pytest.mark.anyio


async def test_list_nfc(mcp_client):
    body = ok(await mcp_client.call_tool("list_nfc", {"host": "Office"}))
    assert body["readers"] == [{"id": "pn532-sim", "driver": "pn532", "connected": True, "can_write": True}]
    assert body["mappings"][0]["uid"] == "04aabbccdd" and body["mappings"][0]["ours"] is False
    assert body["last_card"] is None


async def test_write_card_writes_the_games_own_zapscript(mcp_client, configured):
    body = ok(await mcp_client.call_tool("write_nfc_card", {"game": "Office/102"}))
    assert body["detail"] == "Wrote Chrono Trigger to the card on Office's reader."
    assert office(configured).device.cards_written == ["@SNES/Chrono Trigger"]


async def test_write_card_needs_a_writer_on_that_mister(mcp_client, configured):
    msg = error(await mcp_client.call_tool("write_nfc_card", {"game": "Family Room/4812"}))
    assert "No NFC reader that can write is connected to Family Room" in msg
    assert family(configured).device.cards_written == []


async def test_map_the_card_just_scanned(mcp_client, configured):
    await office(configured).zaparoo.scan_card("04:A1:B2:C3:D4:E5:F6")
    body = ok(await mcp_client.call_tool("map_nfc_card", {"game": "Office/105"}))
    assert body["detail"] == "Card 04a1b2c3d4e5f6 now launches Contra on Office."
    mapping = office(configured).device.mappings[-1]
    assert mapping["type"] == "uid" and mapping["match"] == "exact" and mapping["pattern"] == "04a1b2c3d4e5f6"
    assert mapping["override"] == "@NES/Contra" and mapping["label"] == "mcp: Contra"


async def test_map_without_a_recent_scan_asks_for_one(mcp_client):
    msg = error(await mcp_client.call_tool("map_nfc_card", {"game": "Office/105"}))
    assert "No card has been scanned on Office in the last 2 minutes" in msg


async def test_a_card_that_is_already_mapped_is_refused(mcp_client, configured):
    msg = error(await mcp_client.call_tool("map_nfc_card", {"game": "Office/101", "uid": "04 AA BB CC DD"}))
    assert "already mapped on Office (Contra card)" in msg
    assert len(office(configured).device.mappings) == 1


async def test_bad_uid(mcp_client):
    assert "doesn't look like a card UID" in error(
        await mcp_client.call_tool("map_nfc_card", {"game": "Office/101", "uid": "hello"})
    )


def test_normalize_uid():
    assert normalize_uid("04:A1:b2 C3") == "04a1b2c3"
    assert normalize_uid("zz") == "" and normalize_uid("04") == ""

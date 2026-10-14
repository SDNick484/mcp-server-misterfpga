"""Replay real MiSTers' recorded answers (doctor --dump) through the tools.

The simulator follows Zaparoo's documentation; a recording is what a real Core
actually sent. Each file in tests/fixtures/recorded/ is served by the fake Core
in place of its simulated answers, and the read tools must turn it into valid
results. Skipped until there are recordings (HARDWARE_VALIDATION.md step 2).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .conftest import connect, office, ok

RECORDED = sorted((Path(__file__).parent / "fixtures" / "recorded").glob("*.json"))

pytestmark = pytest.mark.anyio


@pytest.mark.skipif(not RECORDED, reason="no recordings yet: doctor --dump tests/fixtures/recorded")
@pytest.mark.parametrize("recording", RECORDED, ids=[p.stem for p in RECORDED])
async def test_recorded_answers_parse(recording, configured):
    office(configured).zaparoo.recorded = json.loads(recording.read_text())
    c = await connect()
    try:
        status = ok(await c.call_tool("get_status", {"host": "Office"}))["hosts"][0]
        assert status["zaparoo"]["connected"] is True and status["library"] is not None
        systems = ok(await c.call_tool("list_systems", {"host": "Office"}))["systems"]
        assert systems and all(s["id"] for s in systems)
        ok(await c.call_tool("search_games", {"query": "a", "host": "Office"}))
        ok(await c.call_tool("list_nfc", {"host": "Office"}))
    finally:
        await c.__aexit__(None, None, None)

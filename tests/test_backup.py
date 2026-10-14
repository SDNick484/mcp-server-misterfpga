"""backup_saves: complete snapshots, hard links for unchanged files, pruning, and never touching the MiSTer."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from misterfpga_mcp.backup import slug, snapshots
from misterfpga_mcp.sim.fake_mister import sd_path

from .conftest import connect, error, office, ok

pytestmark = pytest.mark.anyio


def files_under(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


async def test_first_backup_copies_everything(mcp_client, configured, backup_dir):
    body = ok(await mcp_client.call_tool("backup_saves", {"host": "Office"}))
    assert (body["outcome"], body["files"], body["copied"], body["linked"]) == ("done", 4, 4, 0)
    snap = Path(body["snapshot"])
    assert snap.parent == backup_dir / "office"
    fat = sd_path(office(configured).device, "/media/fat")
    expected = {k: v for k, v in files_under(fat).items() if k.startswith(("saves/", "savestates/"))}
    assert files_under(snap) == expected  # byte for byte, nothing else (no MiSTer.ini, no screenshots)


async def test_second_backup_links_unchanged_files(mcp_client, configured):
    first = ok(await mcp_client.call_tool("backup_saves", {"host": "Office"}))
    save = sd_path(office(configured).device, "/media/fat/saves/SNES/Super Metroid (USA).sav")
    save.write_bytes(b"new progress")
    os.utime(save, (save.stat().st_atime, save.stat().st_mtime + 60))
    second = ok(await mcp_client.call_tool("backup_saves", {"host": "Office"}))
    assert (second["copied"], second["linked"]) == (1, 3)
    assert second["snapshot"] != first["snapshot"]  # same second: the name gets a suffix
    old, new = Path(first["snapshot"]), Path(second["snapshot"])
    sonic = "saves/Genesis/Sonic the Hedgehog (USA, Europe).sav"
    assert (old / sonic).stat().st_ino == (new / sonic).stat().st_ino  # a hard link, not a second copy
    assert (new / "saves/SNES/Super Metroid (USA).sav").read_bytes() == b"new progress"
    assert (old / "saves/SNES/Super Metroid (USA).sav").read_bytes() != b"new progress"


async def test_old_snapshots_are_pruned(configured, backup_dir, tmp_path):
    configured.write_config(tmp_path / "config", backup_dir=backup_dir)
    host_dir = backup_dir / "office"
    for day in range(1, 31):
        (host_dir / f"2026-09-{day:02d}_120000").mkdir(parents=True)
    (host_dir / "notes").mkdir()
    c = await connect()
    try:
        body = ok(await c.call_tool("backup_saves", {"host": "Office"}))
    finally:
        await c.__aexit__(None, None, None)
    assert body["pruned"] == 1
    assert len(snapshots(host_dir)) == 30 and not (host_dir / "2026-09-01_120000").exists()
    assert (host_dir / "notes").exists()  # only snapshot folders are ever removed


async def test_dry_run_writes_nothing(mcp_client, backup_dir):
    body = ok(await mcp_client.call_tool("backup_saves", {"host": "Office", "dry_run": True}))
    assert (body["outcome"], body["copied"], body["snapshot"]) == ("dry_run", 4, None)
    assert list(backup_dir.iterdir()) == []


async def test_a_failed_backup_leaves_no_partial_snapshot(mcp_client, configured, backup_dir, monkeypatch):
    from misterfpga_mcp import backup as backup_module

    monkeypatch.setattr(backup_module, "MAX_BYTES", 10_000)  # the save state alone is 40 kB
    msg = error(await mcp_client.call_tool("backup_saves", {"host": "Office"}))
    assert "would copy more than 10 KiB in one go" in msg
    assert list((backup_dir / "office").iterdir()) == []


async def test_without_a_backup_dir_it_says_how_to_set_one(sim, tmp_path):
    sim.write_config(tmp_path / "config")  # no backup_dir
    c = await connect()
    try:
        msg = error(await c.call_tool("backup_saves", {"host": "Office"}))
        status = ok(await c.call_tool("get_status", {"host": "Office"}))["hosts"][0]
    finally:
        await c.__aexit__(None, None, None)
    assert "set backup_dir in config.json, or MISTERFPGA_BACKUP_DIR" in msg
    assert "backup_saves" not in status["capabilities"]
    assert any("backup_dir" in limit for limit in status["limitations"])


async def test_the_mister_is_only_read(mcp_client, configured):
    fat = sd_path(office(configured).device, "/media/fat")
    before = {p: p.stat().st_mtime_ns for p in fat.rglob("*")}
    ok(await mcp_client.call_tool("backup_saves", {"host": "Office"}))
    assert {p: p.stat().st_mtime_ns for p in fat.rglob("*")} == before


def test_slug():
    assert slug("Family Room") == "family_room" and slug("  ") == "mister" and slug("Pi #2") == "pi_2"

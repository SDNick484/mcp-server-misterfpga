"""MiSTer.ini: parsing like MiSTer, minimal edits, validation, and set_ini end to end over SFTP."""

from __future__ import annotations

import pytest

from misterfpga_mcp.errors import MisterError
from misterfpga_mcp.ini import DENIED, KEYS, IniFile, check_section, check_value, key_info
from misterfpga_mcp.sim.fake_mister import sd_path

from .conftest import error, family, office, ok

SAMPLE = """\
; my settings
[MiSTer]
video_mode=8 ; 1080p
vscale_mode=0
VSCALE_MODE=1

[SNES]
vscale_mode=2

[snes]
direct_video=0
"""


# --- parsing (ASSUMPTION M-INI-SECTIONS, M-INI-KEYS) --------------------------------------------
def test_sections_merge_like_mister():
    s = IniFile(SAMPLE).sections()
    assert s["MiSTer"] == {"video_mode": "8", "vscale_mode": "1"}  # case-insensitive keys; the later one wins
    assert s["SNES"] == {"vscale_mode": "2", "direct_video": "0"}  # [snes] is the same section as [SNES]


def test_keys_before_the_first_section_are_ignored_like_mister():
    assert IniFile("video_mode=8\n[MiSTer]\nrecents=1\n").sections() == {"MiSTer": {"recents": "1"}}


# --- editing ------------------------------------------------------------------------------------
def test_replacing_keeps_everything_else():
    ini = IniFile(SAMPLE)
    change = ini.set("MiSTer", "video_mode", "0")
    assert change.old == "8" and change.new == "0"
    assert "video_mode=0 ; 1080p" in change.text  # the comment stays
    assert change.text.startswith("; my settings\n[MiSTer]\n")
    changed = [line for line in change.diff.splitlines()[2:] if line.startswith(("-", "+"))]
    assert changed == ["-video_mode=8 ; 1080p", "+video_mode=0 ; 1080p"]


def test_replacing_a_repeated_key_changes_the_effective_one():
    change = IniFile(SAMPLE).set("MiSTer", "vscale_mode", "3")
    assert "vscale_mode=0" in change.text and "VSCALE_MODE=3" in change.text
    assert IniFile(change.text).get("MiSTer", "vscale_mode") == "3"


def test_adding_goes_to_the_end_of_the_last_block_of_the_section():
    text = IniFile(SAMPLE).set("SNES", "vsync_adjust", "1").text
    assert text.endswith("[snes]\ndirect_video=0\nvsync_adjust=1\n")


def test_adding_a_section():
    text = IniFile("[MiSTer]\nrecents=1\n").set("arcade_vertical", "vscale_mode", "1").text
    assert text == "[MiSTer]\nrecents=1\n\n[arcade_vertical]\nvscale_mode=1\n"


def test_removing_removes_every_line_of_it_in_that_section():
    text = IniFile(SAMPLE).set("MiSTer", "vscale_mode", None).text
    assert "vscale_mode" not in text.split("[SNES]")[0].lower()
    assert "vscale_mode=2" in text  # [SNES] untouched


def test_crlf_files_stay_crlf():
    text = IniFile("[MiSTer]\r\nrecents=0\r\n").set("MiSTer", "recents", "1").text
    assert text == "[MiSTer]\r\nrecents=1\r\n"


def test_an_empty_file_gets_a_mister_section():
    assert IniFile("").set("MiSTer", "recents", "1").text == "[MiSTer]\nrecents=1\n"


# --- validation -----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("key", "value", "written"),
    [
        ("vscale_mode", 5, "5"),
        ("VSCALE_MODE", "3", "3"),
        ("hdmi_cec_clock", "12.5", "12.5"),
        ("jamma_vid", "0x1234", "0x1234"),
        ("osd_timeout", 30, "30"),
        ("video_mode", "1920,88,44,148,1080,4,5,36,148500", "1920,88,44,148,1080,4,5,36,148500"),
        ("log_file_entry", 1, "1"),  # allowed: Zaparoo's tracking advice needs it (ASSUMPTION Z-TRACKS-MENU)
        ("recents", True, "1"),
    ],
)
def test_valid_values(key, value, written):
    assert check_value(key_info(key), value) == written


@pytest.mark.parametrize(
    ("key", "value", "says"),
    [
        ("vscale_mode", 6, "out of range"),
        ("vscale_mode", "two", "isn't valid"),
        ("jamma_vid", "1234", "isn't valid"),  # hex needs 0x, as MiSTer warns
        ("video_mode", "8; rm -rf /", "isn't valid"),  # ';' would start a comment; cfg.cpp drops such characters
        ("font", "x" * 300, "isn't valid"),
    ],
)
def test_invalid_values(key, value, says):
    with pytest.raises(MisterError, match=says):
        check_value(key_info(key), value)


@pytest.mark.parametrize("key", sorted(DENIED))
def test_denied_keys(key):
    with pytest.raises(MisterError, match="can't be set through this server"):
        check_value(key_info(key), "x")


def test_bootcore_is_denied_and_array_keys_are_read_only():
    assert "BOOTCORE" in DENIED  # decided 2026-10-13 (DESIGN.md §12, Q5)
    with pytest.raises(MisterError, match="array key"):
        check_value(key_info("player_1_controller"), "x")


def test_unknown_keys_suggest_close_ones():
    with pytest.raises(MisterError, match="Did you mean vscale_mode"):
        key_info("vscale_mod")


def test_the_key_table_matches_main_mister():
    assert len(KEYS) == 112  # ini_vars[] at Main_MiSTer 2dd3662 (ASSUMPTION M-INI-KEYS)
    assert KEYS["VSCALE_MODE"].max == 5 and KEYS["BOOTCORE_TIMEOUT"].min == 2


@pytest.mark.parametrize(
    ("given", "written"),
    [("mister", "MiSTer"), ("[SNES]", "SNES"), ("PSX*", "PSX*"), ("VIDEO=1920x1080@60", "video=1920x1080@60")],
)
def test_sections(given, written):
    assert check_section(given) == written


@pytest.mark.parametrize("bad", ["", "SNES]\n[MiSTer", "a/b", "video=big"])
def test_bad_sections(bad):
    with pytest.raises(MisterError):
        check_section(bad)


# --- end to end over SFTP ----------------------------------------------------------------------------
@pytest.mark.anyio
async def test_get_ini_reads_sections_and_lists_files(mcp_client):
    body = ok(await mcp_client.call_tool("get_ini", {"host": "Office"}))
    assert body["files"] == ["MiSTer.ini", "MiSTer_crt.ini"]
    assert body["sections"]["arcade_vertical"] == {"vscale_mode": "1"}
    crt = ok(await mcp_client.call_tool("get_ini", {"host": "Office", "file": "MiSTer_crt.ini", "section": "mister"}))
    assert crt["sections"] == {"MiSTer": {"vga_scaler": "0", "composite_sync": "1"}}


@pytest.mark.anyio
async def test_set_ini_per_core_writes_a_backup_first(mcp_client, configured):
    body = ok(
        await mcp_client.call_tool(
            "set_ini", {"host": "Family Room", "section": "snes", "key": "vscale_mode", "value": 3}
        )
    )
    assert (body["outcome"], body["old"], body["new"], body["section"]) == ("done", "1", "3", "SNES")
    assert "next time a core loads" in body["detail"]
    device = family(configured).device
    assert "[SNES]\nvscale_mode=3" in sd_path(device, "/media/fat/MiSTer.ini").read_text()
    backup = sd_path(device, body["backup"]).read_text()
    assert "[SNES]\nvscale_mode=1" in backup  # the file as it was
    assert not sd_path(device, "/media/fat/MiSTer.ini.tmp").exists()


@pytest.mark.anyio
async def test_set_ini_dry_run_and_unchanged_write_nothing(mcp_client, configured):
    before = sd_path(office(configured).device, "/media/fat/MiSTer.ini").read_text()
    dry = ok(await mcp_client.call_tool("set_ini", {"host": "Office", "key": "recents", "value": 0, "dry_run": True}))
    assert dry["outcome"] == "dry_run" and "-recents=1" in dry["diff"] and dry["backup"] is None
    same = ok(await mcp_client.call_tool("set_ini", {"host": "Office", "key": "recents", "value": 1}))
    assert same["outcome"] == "unchanged"
    assert sd_path(office(configured).device, "/media/fat/MiSTer.ini").read_text() == before
    assert not list(sd_path(office(configured).device, "/media/fat").glob("MiSTer.ini.*.bak"))


@pytest.mark.anyio
async def test_set_ini_warns_about_a_core_that_isnt_installed(mcp_client):
    body = ok(
        await mcp_client.call_tool(
            "set_ini", {"host": "Office", "section": "SNSE", "key": "vscale_mode", "value": 1, "dry_run": True}
        )
    )
    assert "No installed core on Office is called SNSE" in body["warnings"][0]


@pytest.mark.anyio
async def test_set_ini_without_posix_rename_still_replaces_the_file(mcp_client, configured):
    office(configured).ssh.sftp_posix_rename = False  # ASSUMPTION M-INI-RENAME: the fallback path
    ok(await mcp_client.call_tool("set_ini", {"host": "Office", "key": "log_file_entry", "value": 1}))
    text = sd_path(office(configured).device, "/media/fat/MiSTer.ini").read_text()
    assert "log_file_entry=1" in text


@pytest.mark.anyio
async def test_old_backups_are_pruned_to_ten(mcp_client, configured):
    device = office(configured).device
    for i in range(12):
        sd_path(device, f"/media/fat/MiSTer.ini.2026010{i % 10}T00000{i % 10}Z.bak").write_text("old")
    sd_path(device, "/media/fat/MiSTer.ini.keep-me.bak").write_text("not ours")
    ok(await mcp_client.call_tool("set_ini", {"host": "Office", "key": "vscale_mode", "value": 2}))
    fat = sd_path(device, "/media/fat")
    ours = sorted(p.name for p in fat.glob("MiSTer.ini.*Z.bak"))
    assert len(ours) == 10
    assert (fat / "MiSTer.ini.keep-me.bak").exists()  # never touches files it didn't make


@pytest.mark.anyio
async def test_set_ini_refusals(mcp_client):
    assert "can't be set" in error(
        await mcp_client.call_tool("set_ini", {"host": "Office", "key": "main", "value": "MiSTer_test"})
    )
    assert "has no MiSTer_nope.ini" in error(
        await mcp_client.call_tool(
            "set_ini", {"host": "Office", "key": "recents", "value": 1, "file": "MiSTer_nope.ini"}
        )
    )
    assert "isn't a MiSTer ini file name" in error(
        await mcp_client.call_tool("set_ini", {"host": "Office", "key": "recents", "value": 1, "file": "../etc/passwd"})
    )

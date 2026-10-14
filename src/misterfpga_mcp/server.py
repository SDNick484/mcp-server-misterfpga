"""MCP tools, resources and a prompt.

How a decorated function becomes a tool (same as the sibling projects):
  - the docstring is the tool's description, written for the model;
  - the signature becomes the inputSchema (Field(ge=, le=) -> minimum/maximum);
  - a TypedDict return type becomes the outputSchema, sent as structured content.

The shape of the tool set (docs/DESIGN.md §6):
  - get_status first: every MiSTer, what it's doing, and what works on it.
  - Games are found (search_games, play_history) and launched by ref
    ("Family Room/4812"). The model never supplies a path or ZapScript: the
    server turns a ref back into Zaparoo's own launch script (media.meta).
  - Ending a game is its own, destructive call (return_to_menu); launch refuses
    to replace a running game instead of quietly ending it (Batocera's rule).
  - MiSTer.ini edits go through MiSTer's own key table, with a backup each time.

Why there's no list_hosts: get_status already lists every MiSTer, and two tools
that both list them split the model's choice for no gain (Harmony's reasoning).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Literal, TypeVar

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import Field
from typing_extensions import TypedDict

from .config import load_settings
from .errors import MisterError
from .host import HostStatus, MisterHost, NowPlaying, Outcome, ref
from .hosts import HostRegistry
from .ini import DENIED, KEYS, STRING_MAX
from .limits import HISTORY_MAX, SEARCH_MAX

log = logging.getLogger(__name__)

_registry: HostRegistry | None = None


def registry() -> HostRegistry:
    assert _registry is not None, "server lifespan has not started"
    return _registry


async def ready() -> HostRegistry:
    """registry(), but the first tool call after startup waits briefly for MiSTers still connecting."""
    r = registry()
    await r.ready()
    return r


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
    """Open every MiSTer's Zaparoo connection once at startup; close them at shutdown."""
    global _registry
    _registry = HostRegistry(load_settings())
    await _registry.start()
    try:
        yield
    finally:
        await _registry.stop()
        _registry = None


mcp = MCPServer(
    "misterfpga",
    instructions=(
        "Controls MiSTer FPGA devices (each usually on its own TV) through Zaparoo Core and SSH. Call get_status "
        "first: it lists each MiSTer by name and model, what it's playing, and its capabilities and limitations. "
        "Find games with search_games or play_history; launch them with launch(game=<ref>). launch refuses while "
        "a game is running: ask before ending one with return_to_menu, since unsaved progress is lost. Pass host "
        "only when the call doesn't already say which MiSTer (a ref does)."
    ),
    lifespan=lifespan,
)

# Annotations: explicit on every tool, since a tool without them is assumed destructive, non-idempotent and
# open-world. open_world is False everywhere: we only talk to MiSTers on the LAN. Destructive here means it can
# lose something: unsaved game progress (return_to_menu, run_update, which may also reboot) or a card's contents
# (write_nfc_card). Batocera's rule.
_READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
_ACT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
_ACT_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
_DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)
_DESTRUCTIVE_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)

Host = Annotated[
    str | None,
    Field(description="MiSTer name from get_status. Omit when the call already says which (a ref does)."),
]
GameRef = Annotated[
    str,
    Field(description="A game ref from search_games or play_history, e.g. 'Family Room/4812'.", max_length=80),
]
System = Annotated[
    str | None,
    Field(description="A system id from list_systems, e.g. 'SNES' or 'Genesis'.", max_length=40),
]
IniFileName = Annotated[
    str,
    Field(description="MiSTer.ini, or an alternate such as MiSTer_crt.ini (get_ini lists them).", max_length=60),
]


# --- result shapes (each becomes an outputSchema) ---------------------------------------
class Status(TypedDict):
    dry_run: bool  # true: writes are reported, not done
    hosts: list[HostStatus]


class SystemInfo(TypedDict):
    host: str
    id: str
    name: str
    games: int | None
    core: str | None  # the MiSTer core that runs it, e.g. "_Console/SNES"


class Systems(TypedDict):
    systems: list[SystemInfo]
    unavailable: list[str]  # MiSTers that couldn't be asked, and why


class GameInfo(TypedDict):
    ref: str | None  # pass to launch; None when Zaparoo has no row for it
    host: str
    name: str
    system: str
    system_name: str
    variant: str | None  # what tells same-named files apart, e.g. "region:us"
    path: str


class Search(TypedDict):
    results: list[GameInfo]
    truncated: bool  # more matched than limit; narrow the query or pass system
    unavailable: list[str]


class HistoryEntry(TypedDict):
    ref: str | None
    host: str
    game: str
    system: str
    last_played: str | None
    play_time_s: int  # this session (recent), or all sessions together (top)
    sessions: int | None  # top only


class History(TypedDict):
    entries: list[HistoryEntry]
    unavailable: list[str]


class Results(TypedDict):
    results: list[Outcome]


class IniView(TypedDict):
    host: str
    file: str
    files: list[str]  # every MiSTer*.ini on that MiSTer
    sections: dict[str, dict[str, str]]  # {section: {key: value}}, keys lower-case


class IniChange(TypedDict):
    host: str
    outcome: Literal["done", "unchanged", "dry_run"]
    detail: str
    file: str
    section: str
    key: str
    old: str | None
    new: str | None
    backup: str | None  # where the previous file was saved on the MiSTer
    diff: str
    warnings: list[str]


class BackupInfo(TypedDict):
    host: str
    outcome: Literal["done", "dry_run"]
    snapshot: str | None
    files: int
    copied: int
    linked: int  # unchanged since the last snapshot: hard-linked, not downloaded
    bytes_copied: int
    pruned: int  # old snapshots removed (beyond backup_keep)


class ReaderInfo(TypedDict):
    id: str
    driver: str
    connected: bool
    can_write: bool


class MappingInfo(TypedDict):
    id: str
    label: str
    enabled: bool
    uid: str | None  # set when the mapping matches a card UID
    launches: str  # its ZapScript
    ours: bool  # created by this server (label starts "mcp: ")


class NfcInfo(TypedDict):
    host: str
    readers: list[ReaderInfo]
    mappings: list[MappingInfo]
    last_card: str | None  # UID of the card on the reader now, else the last one scanned
    last_card_at: str | None


# --- reads ------------------------------------------------------------------------------
@mcp.tool(title="Get MiSTer status", annotations=_READ)
async def get_status(host: Host = None) -> Status:
    """Start here. Every MiSTer (or just host): its model, what it's playing, and what works on it right now.

    capabilities names what works (search, launch, screenshot, ini_write, backup_saves, ...); limitations says
    what doesn't and how to fix it, e.g. Zaparoo Core not installed, or SSH not set up. core is the running
    MiSTer core (MENU in the menu) and attract_mode is true while MiSTer Super Attract Mode cycles games; both
    need SSH. dry_run true means this server reports writes instead of doing them.
    """
    r = await ready()
    statuses = await asyncio.gather(*(h.status() for h in r.candidates(host)))
    return {"dry_run": r.settings.dry_run, "hosts": list(statuses)}


T = TypeVar("T")


async def _gather(
    pool: list[MisterHost], work: Callable[[MisterHost], Awaitable[T]]
) -> tuple[list[tuple[MisterHost, T]], list[str]]:
    """Run ``work(host)`` on each MiSTer; collect results, and a sentence for each one that couldn't answer.

    One MiSTer being off shouldn't hide the other's results, so failures become entries in ``unavailable``,
    except when there was only one MiSTer to ask: then its error is the answer.
    """

    async def one(h: MisterHost) -> tuple[MisterHost, T | None, str | None]:
        try:
            return h, await work(h), None
        except MisterError as exc:
            return h, None, f"{h.name}: {exc}"

    done = await asyncio.gather(*(one(h) for h in pool))
    if len(pool) == 1 and done[0][2] is not None:
        raise MisterError(done[0][2])
    ok = [(h, out) for h, out, err in done if err is None and out is not None]
    return ok, [err for _, _, err in done if err is not None]


@mcp.tool(title="List systems", annotations=_READ)
async def list_systems(host: Host = None) -> Systems:
    """The systems each MiSTer can launch (consoles, computers, arcade), with how many games each has indexed and
    the MiSTer core that runs it. Use a system's id with search_games, or launch(system=...) to open the core
    with no game (e.g. a computer core)."""
    r = await ready()

    async def work(h: MisterHost) -> list[SystemInfo]:
        cores = await h.cores()
        out: list[SystemInfo] = []
        for s in await h.systems():
            sid = str(s.get("id") or "")
            core = cores.get(sid)
            out.append(
                {
                    "host": h.name,
                    "id": sid,
                    "name": str(s.get("name") or sid),
                    "games": s.get("mediaCount") if isinstance(s.get("mediaCount"), int) else None,
                    "core": core[1] if core else None,
                }
            )
        return out

    rows, unavailable = await _gather(r.candidates(host), work)
    return {"systems": [s for _, systems in rows for s in systems], "unavailable": unavailable}


def _variant(row: dict[str, object]) -> str | None:
    tags = row.get("disambiguatingTags")
    if not isinstance(tags, list):
        return None
    parts = [f"{t.get('type')}:{t.get('tag')}" for t in tags if isinstance(t, dict) and t.get("tag")]
    return ", ".join(parts) or None


@mcp.tool(title="Search for games", annotations=_READ)
async def search_games(
    query: Annotated[str, Field(min_length=1, max_length=100, description="Words in the game's name, e.g. 'metroid'.")],
    system: System = None,
    host: Host = None,
    limit: Annotated[int, Field(ge=1, le=SEARCH_MAX, description="Most results per MiSTer.")] = 20,
) -> Search:
    """Find games in each MiSTer's Zaparoo library by name (every word must match), optionally in one system.

    Each result has a ref for launch. The same game on two MiSTers comes back twice, with two refs: the ref says
    which TV it launches on. variant tells apart files with the same name (region, revision, hack).
    """
    r = await ready()

    async def work(h: MisterHost) -> list[dict[str, object]]:
        return await h.search(query, system, limit)

    rows, unavailable = await _gather(r.candidates(host), work)
    results: list[GameInfo] = []
    truncated = False
    for h, found in rows:
        truncated = truncated or len(found) >= limit
        for row in found:
            sysinfo = row.get("system") if isinstance(row.get("system"), dict) else {}
            assert isinstance(sysinfo, dict)
            results.append(
                {
                    "ref": ref(h.name, row.get("mediaId")),
                    "host": h.name,
                    "name": str(row.get("name") or ""),
                    "system": str(sysinfo.get("id") or ""),
                    "system_name": str(sysinfo.get("name") or ""),
                    "variant": _variant(row),
                    "path": str(row.get("relativePath") or row.get("path") or ""),
                }
            )
    return {"results": results, "truncated": truncated, "unavailable": unavailable}


def _when(stamp: str | None) -> datetime:
    """An RFC 3339 time for sorting; times from different MiSTers can carry different offsets."""
    try:
        t = datetime.fromisoformat((stamp or "").replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


@mcp.tool(title="Play history", annotations=_READ)
async def play_history(
    mode: Annotated[
        Literal["recent", "top"],
        Field(description="recent: last played first, one row per game. top: most played by total time."),
    ] = "recent",
    system: System = None,
    host: Host = None,
    limit: Annotated[int, Field(ge=1, le=HISTORY_MAX)] = 10,
) -> History:
    """What's been played on each MiSTer, from Zaparoo's own records: games launched from the MiSTer's menu
    count too. Rows have a ref, so 'play the last thing again' is one launch call."""
    r = await ready()

    async def work(h: MisterHost) -> list[dict[str, object]]:
        return await h.history(mode, system, limit)

    rows, unavailable = await _gather(r.candidates(host), work)
    entries: list[HistoryEntry] = []
    for h, found in rows:
        for e in found:
            seconds = e.get("totalPlayTime") if mode == "top" else e.get("playTime")
            entries.append(
                {
                    "ref": ref(h.name, e.get("mediaId")),
                    "host": h.name,
                    "game": str(e.get("mediaName") or ""),
                    "system": str(e.get("systemName") or e.get("systemId") or ""),
                    "last_played": str(e.get("lastPlayedAt") or e.get("startedAt") or "") or None,
                    "play_time_s": int(seconds) if isinstance(seconds, int | float) else 0,
                    "sessions": count if isinstance(count := e.get("sessionCount"), int) else None,
                }
            )
    # Merge the MiSTers into one list in the order the mode promises (each MiSTer's own list is already sorted).
    if mode == "top":
        entries.sort(key=lambda e: -e["play_time_s"])
    else:
        entries.sort(key=lambda e: _when(e["last_played"]), reverse=True)
    return {"entries": entries[:limit], "unavailable": unavailable}


@mcp.tool(title="Read MiSTer.ini", annotations=_READ)
async def get_ini(
    section: Annotated[
        str | None, Field(description="Only this section, e.g. MiSTer or SNES. Omit for all.", max_length=60)
    ] = None,
    file: IniFileName = "MiSTer.ini",
    host: Host = None,
) -> IniView:
    """Read a MiSTer's MiSTer.ini (or an alternate ini), parsed into sections the way MiSTer reads it. [MiSTer]
    is global; [SNES] and other core-named sections override it for that core. Needs SSH."""
    h = (await ready()).one(host, "be read")
    ini = await h.read_ini(file)
    sections = ini.sections()
    if section is not None:
        sections = {k: v for k, v in sections.items() if k.lower() == section.strip().strip("[]").lower()}
    return {"host": h.name, "file": file, "files": await h.ini_files(), "sections": sections}


@mcp.tool(title="List NFC readers and cards", annotations=_READ)
async def list_nfc(host: Host = None) -> NfcInfo:
    """A MiSTer's NFC readers, the card-to-game mappings Zaparoo has, and the last card scanned (its UID is what
    map_nfc_card uses by default)."""
    h = (await ready()).one(host, "be checked for NFC")
    readers: list[ReaderInfo] = [
        {
            "id": str(rd.get("readerId") or rd.get("id") or ""),
            "driver": str(rd.get("driver") or ""),
            "connected": bool(rd.get("connected", True)),
            "can_write": "write" in (rd.get("capabilities") or []),
        }
        for rd in await h.readers()
    ]
    mappings: list[MappingInfo] = [
        {
            "id": str(m.get("id") or ""),
            "label": str(m.get("label") or ""),
            "enabled": bool(m.get("enabled")),
            "uid": str(m.get("pattern")) if m.get("type") == "uid" else None,
            "launches": str(m.get("override") or ""),
            "ours": str(m.get("label") or "").startswith("mcp: "),
        }
        for m in await h.mappings()
    ]
    last = await h.last_card()
    return {
        "host": h.name,
        "readers": readers,
        "mappings": mappings,
        "last_card": last[0] if last else None,
        "last_card_at": last[1].isoformat() if last else None,
    }


# --- actions ----------------------------------------------------------------------------
@mcp.tool(title="Launch a game or core", annotations=_ACT_IDEMPOTENT)
async def launch(
    game: Annotated[
        str | None,
        Field(description="A game ref from search_games or play_history, e.g. 'Family Room/4812'.", max_length=80),
    ] = None,
    system: Annotated[
        str | None,
        Field(
            description="Instead of a game: a system id from list_systems, to open its core with no game.",
            max_length=40,
        ),
    ] = None,
    host: Host = None,
) -> Outcome:
    """Launch a game (by ref) or open a system's core (by id) and wait until the MiSTer reports it running.

    Refuses while anything is running on that MiSTer: ending a game loses unsaved progress, so that's
    return_to_menu's job, and you should ask first. Rapid repeated launches are rate-limited.
    """
    r = await ready()
    if game is not None:
        h, media_id = r.game(game, host)
        return await h.launch(media_id, None)
    if system is None:
        raise MisterError("Pass game (a ref from search_games) or system (an id from list_systems).")
    return await r.one(host, "open the core").launch(None, system)


@mcp.tool(title="Return to the MiSTer menu", annotations=_DESTRUCTIVE_IDEMPOTENT)
async def return_to_menu(host: Host = None) -> Results:
    """End whatever is running and go back to the MiSTer menu. Unsaved progress in the game is lost, so confirm
    with the user first. Without host, acts on the one MiSTer that's running something."""
    r = await ready()
    targets = await r.busy(host, "return to the menu")
    if not targets:
        return {
            "results": [
                {"host": "", "outcome": "unchanged", "detail": "Nothing is running on any MiSTer.", "warnings": []}
            ]
        }
    return {"results": [await h.return_to_menu() for h in targets]}


@mcp.tool(title="Take a screenshot", annotations=_ACT)
async def screenshot(host: Host = None) -> CallToolResult:
    """Capture what a MiSTer is showing, as an image. MiSTer also saves it under /media/fat/screenshots. Without
    host, uses the one MiSTer that's running something."""
    r = await ready()
    pool = await r.busy(host, "take a screenshot")
    h = pool[0] if len(pool) == 1 else r.one(host, "take a screenshot")
    data, path = await h.screenshot()
    meta = {"host": h.name, "saved_path": path}
    return CallToolResult(
        content=[
            ImageContent(type="image", data=base64.b64encode(data).decode(), mime_type="image/png"),
            TextContent(type="text", text=json.dumps(meta)),
        ],
        structured_content=meta,
    )


@mcp.tool(title="Change a MiSTer.ini setting", annotations=_ACT_IDEMPOTENT)
async def set_ini(
    key: Annotated[
        str, Field(description="A MiSTer.ini key, e.g. vscale_mode (misterfpga://ini-keys lists them).", max_length=40)
    ],
    value: Annotated[
        str | int | float | None,
        Field(description="The new value, checked against the key's type and range. null removes the key."),
    ],
    section: Annotated[
        str,
        Field(
            description="MiSTer (global), arcade, arcade_vertical, video=<WxH@Hz>, or a core name like SNES.",
            max_length=60,
        ),
    ] = "MiSTer",
    file: IniFileName = "MiSTer.ini",
    host: Host = None,
    dry_run: Annotated[bool, Field(description="Show the change (diff) without writing it.")] = False,
) -> IniChange:
    """Set (or remove) one MiSTer.ini setting, globally or for one core, keeping the rest of the file as it is.

    The previous file is saved next to it first (MiSTer.ini.<time>.bak). The change takes effect the next time
    a core loads. A few keys are refused (main, bootcore, shared_folder, debug; see misterfpga://ini-keys).
    Needs SSH.
    """
    h = (await ready()).one(host, "be changed")
    out, change, backup = await h.set_ini(key, value, section, file, dry_run)
    return {
        "host": out["host"],
        "outcome": out["outcome"],
        "detail": out["detail"],
        "file": file,
        "section": change.section,
        "key": change.key,
        "old": change.old,
        "new": change.new,
        "backup": backup,
        "diff": change.diff,
        "warnings": out["warnings"],
    }


@mcp.tool(title="Back up saves", annotations=_ACT_IDEMPOTENT)
async def backup_saves(
    ctx: Context,
    host: Host = None,
    dry_run: Annotated[bool, Field(description="Count what would be copied, write nothing.")] = False,
) -> BackupInfo:
    """Copy a MiSTer's saves and save states into a new dated snapshot in the configured backup folder.

    Files unchanged since the last snapshot are hard-linked rather than downloaded, so each snapshot is complete
    but only changes cost space. The MiSTer is only read. Old snapshots beyond the configured number are removed.
    Needs SSH and a backup folder (backup_dir).
    """
    h = (await ready()).one(host, "be backed up")

    async def progress(files: int, current: str) -> None:
        await ctx.report_progress(files, message=f"{files} files ({current})")

    result = await h.backup(dry_run, progress)
    return {
        "host": h.name,
        "outcome": "dry_run" if dry_run or h.dry_run else "done",
        "snapshot": result.snapshot,
        "files": result.files,
        "copied": result.copied,
        "linked": result.linked,
        "bytes_copied": result.bytes_copied,
        "pruned": result.pruned,
    }


@mcp.tool(title="Update the MiSTer", annotations=_DESTRUCTIVE)
async def run_update(
    host: Annotated[str, Field(description="MiSTer name from get_status. Required: there's no default for this.")],
    confirm: Annotated[
        Literal[True], Field(description="Must be true: you've confirmed with the user that it may reboot.")
    ],
) -> Outcome:
    """Start Update All on a MiSTer's own screen (cores, Main, Linux, and whatever else it's set to update).

    It runs for several minutes and the MiSTer may reboot at the end. Refuses while a game is running.
    Limited to once an hour per MiSTer.
    """
    h = (await ready()).one(host, "be updated")
    return await h.run_update()


@mcp.tool(title="Write a game to an NFC card", annotations=_DESTRUCTIVE)
async def write_nfc_card(
    game: GameRef,
    reader: Annotated[
        str | None, Field(description="Reader id from list_nfc, when there's more than one.", max_length=80)
    ] = None,
) -> Outcome:
    """Write a game onto the NFC card sitting on the reader attached to the game's MiSTer, so tapping it launches
    the game. Overwrites whatever the card held. Ask the user to place the card first; the call waits up to a
    minute for it."""
    h, media_id = (await ready()).game(game, None)
    return await h.write_card(media_id, reader)


@mcp.tool(title="Map an NFC card to a game", annotations=_ACT_IDEMPOTENT)
async def map_nfc_card(
    game: GameRef,
    uid: Annotated[
        str | None,
        Field(
            description="The card's UID. Omit to use the card scanned on that MiSTer in the last 2 minutes.",
            max_length=80,
        ),
    ] = None,
) -> Outcome:
    """Make a card launch a game without writing to it: Zaparoo remembers 'this card's UID means this game'.
    Works with read-only cards and toys. Ask the user to scan the card on the game's MiSTer first, then call this
    without uid. Refuses a card that's already mapped."""
    h, media_id = (await ready()).game(game, None)
    return await h.map_card(media_id, uid)


# --- resources --------------------------------------------------------------------------
@mcp.resource(
    "misterfpga://ini-keys",
    name="ini-keys",
    title="MiSTer.ini keys set_ini accepts",
    mime_type="application/json",
)
def ini_keys_resource() -> str:
    keys = {
        name.lower(): {
            "type": info.describe(),
            "settable": info.settable,
            **({"why_not": DENIED[name]} if name in DENIED else {}),
        }
        for name, info in sorted(KEYS.items())
    }
    return json.dumps({"source": "Main_MiSTer cfg.cpp ini_vars[]", "string_max": STRING_MAX, "keys": keys}, indent=1)


# --- prompts ----------------------------------------------------------------------------
@mcp.prompt(title="Make NFC cards for games")
def make_nfc_cards(games: str = "", host: str = "") -> str:
    """Walk through turning a list of games into NFC cards, one card at a time."""
    which = f"the games: {games}" if games else "the games I name (ask me which)"
    where = f"on the MiSTer {host!r}" if host else "on the MiSTer that has the NFC reader (ask me which if unsure)"
    return (
        f"Help me make NFC cards for {which}, {where}.\n"
        "1. Call list_nfc to see the readers: a reader with can_write true can write cards; otherwise we'll map "
        "cards by UID.\n"
        "2. For each game, call search_games and show me the matches if there's more than one, so I can pick.\n"
        "3. Then, one card at a time: tell me to put a card on the reader (to write) or to tap it (to map), wait "
        "for me to say it's there, and call write_nfc_card or map_nfc_card. Report each result before the next.\n"
        "4. At the end, list which card got which game."
    )


__all__ = ["mcp", "registry", "NowPlaying"]

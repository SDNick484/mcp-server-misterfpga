"""One MiSTer: its Zaparoo connection, its SSH shell, its rate limits, and what each tool does to it.

server.py decides *which* MiSTer a call is about (through hosts.py) and shapes
the results; this module does the work on one MiSTer. Every write here:
- checks the rate limit before doing anything (all-or-nothing, see ratelimit.py),
- in dry-run mode, says what it would do and does nothing,
- confirms what it can instead of trusting that a request was accepted.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from typing_extensions import TypedDict

from . import limits
from .backup import BackupResult, Progress, backup_saves
from .config import HostSettings, Settings, name_key
from .errors import MisterError, Unavailable, ZaparooError
from .ini import Change, IniFile, check_section, check_value, key_info
from .ratelimit import TokenBucket, refusal
from .ssh import SD, Command, MisterShell, check_path, is_ini_name
from .zaparoo import RUN_TIMEOUT, SETUP_HINT, WRITE_TIMEOUT, ZaparooClient

log = logging.getLogger(__name__)

MENU_CORE = "MENU"  # ASSUMPTION M-CORENAME
UPDATE_SCRIPT = "**mister.script:update_all.sh"  # ASSUMPTION Z-SCRIPT
CONFIRM_SECONDS = 10.0
CARD_FRESH = timedelta(minutes=2)
_UID = re.compile(r"[0-9a-f]{4,64}")
_SYSTEM_ID = re.compile(r"[A-Za-z0-9_+-]{1,40}")


# --- result shapes --------------------------------------------------------------------------
class NowPlaying(TypedDict):
    ref: str | None  # launchable ref, when Zaparoo knows the game's row
    game: str
    system: str
    system_name: str
    started: str


class Backend(TypedDict):
    connected: bool
    detail: str | None  # version when connected, else why not


class Library(TypedDict):
    indexed: bool
    indexing: bool
    total_media: int | None


class HostStatus(TypedDict):
    name: str
    model: str
    reachable: bool
    zaparoo: Backend
    ssh: Backend | None  # None: not configured for this host
    now_playing: NowPlaying | None
    core: str | None  # the running core ("MENU" in the menu); needs SSH
    attract_mode: bool | None  # MiSTer Super Attract Mode cycling games; needs SSH
    library: Library | None
    free_space_mb: int | None
    capabilities: list[str]
    limitations: list[str]


class Outcome(TypedDict):
    host: str
    outcome: Literal["done", "unchanged", "dry_run"]
    detail: str  # one sentence for the user
    warnings: list[str]


def ref(host: str, media_id: object) -> str | None:
    return f"{host}/{media_id}" if isinstance(media_id, int) and not isinstance(media_id, bool) else None


def _outcome(host: str, outcome: Literal["done", "unchanged", "dry_run"], detail: str, *warn: str) -> Outcome:
    return {"host": host, "outcome": outcome, "detail": detail, "warnings": [w for w in warn if w]}


def run_error(exc: ZaparooError, host: str, what: str) -> MisterError:
    """A readable sentence for a failed `run` (ASSUMPTION Z-RUN-WAITS)."""
    reason = exc.reason or ""
    by_category = {
        "media_not_found": f"Zaparoo on {host} couldn't find {what} any more; search again (the library may have "
        "been reindexed).",
        "busy": f"Another launch is already in progress on {host}; try again in a moment.",
        "playtime_limit": f"A Zaparoo playtime limit on {host} blocked launching {what}.",
        "disabled": f"ZapScript is turned off in Zaparoo's settings on {host}.",
        "blocked": f"Zaparoo on {host} refused to launch {what}: {exc}.",
        "timeout": f"Zaparoo on {host} stopped waiting for {what} after 30 s; it may still be loading. Check "
        "get_status before trying again.",
        "unavailable": f"Zaparoo on {host} is shutting down.",
    }
    if exc.category == "launch_repair":
        if reason in ("launcher_not_installed", "launcher_plugin_missing", "launcher_component_missing"):
            return MisterError(f"The core for {what} isn't installed on {host}; Update All installs cores.")
        if reason in ("storage_unavailable", "media_unavailable", "media_access_revoked"):
            return MisterError(
                f"The file for {what} isn't available on {host}: is the SD card, USB drive or network share "
                "with your games mounted?"
            )
        return MisterError(f"{host} can't launch {what} yet: {exc} ({reason or 'no reason given'}).")
    return MisterError(by_category.get(exc.category or "", f"Zaparoo on {host} couldn't launch {what}: {exc}."))


class MisterHost:
    def __init__(self, settings: HostSettings, app: Settings) -> None:
        self.settings = settings
        self.app = app
        self.name = settings.label
        self.zaparoo = ZaparooClient(self.name, settings.host, settings.zaparoo_port)
        self.shell = MisterShell(self.name, settings.host, settings.ssh) if settings.ssh is not None else None
        self.launch_bucket: TokenBucket = limits.bucket(limits.LAUNCH)
        self.screenshot_bucket: TokenBucket = limits.bucket(limits.SCREENSHOT)
        self.ini_bucket: TokenBucket = limits.bucket(limits.INI_WRITE)
        self.nfc_bucket: TokenBucket = limits.bucket(limits.NFC)
        self.update_bucket: TokenBucket = limits.bucket(limits.UPDATE)
        self._backup_lock = asyncio.Lock()

    @property
    def dry_run(self) -> bool:
        return self.app.dry_run

    def answers_to(self, name: str) -> bool:
        key = name_key(name)
        return bool(key) and (key == name_key(self.name) or name.strip() == self.settings.host)

    async def start(self) -> None:
        await self.zaparoo.start()

    async def stop(self) -> None:
        await self.zaparoo.stop()
        if self.shell is not None:
            await self.shell.close()

    def _limit(self, bucket: TokenBucket, what: str) -> None:
        wait = bucket.take()
        if wait:
            raise MisterError(refusal(what, wait, self.name))

    def _ssh(self, what: str) -> MisterShell:
        if self.shell is None:
            raise Unavailable(
                f"{what} needs SSH, which isn't set up for {self.name}. Add an ssh entry for it in config.json "
                "(the README's 'Set up your MiSTer' has the steps)."
            )
        return self.shell

    # --- reads -------------------------------------------------------------------------------
    async def _ssh_text(self, command: Command) -> str | None:
        """A command's output, or None when SSH isn't set up or fails (status reads must never raise)."""
        if self.shell is None:
            return None
        try:
            return (await self.shell.run(command)).strip()
        except MisterError as exc:
            log.debug("%s: %s: %s", self.name, command.name, exc)
            return None

    async def core(self) -> str | None:
        return await self._ssh_text(Command.CORENAME)  # ASSUMPTION M-CORENAME

    async def attract_mode(self) -> bool | None:
        out = await self._ssh_text(Command.PROCESSES)
        if out is None:
            return None
        return any("MiSTer_SAM_on.sh loop_core" in line for line in out.splitlines())  # ASSUMPTION S-ACTIVE

    async def free_space_mb(self) -> int | None:
        out = await self._ssh_text(Command.FREE_SPACE)
        if not out:
            return None
        lines = out.splitlines()
        with contextlib.suppress(IndexError, ValueError):
            return int(lines[-1].split()[3]) // 1024  # Filesystem 1K-blocks Used Available Use% Mounted
        return None

    async def active(self) -> dict[str, Any] | None:
        result = await self.zaparoo.call("media.active")
        return result if isinstance(result, dict) else None

    def now_playing(self, active: dict[str, Any]) -> NowPlaying:
        return {
            "ref": ref(self.name, active.get("mediaId")),
            "game": str(active.get("mediaName") or ""),
            "system": str(active.get("systemId") or ""),
            "system_name": str(active.get("systemName") or ""),
            "started": str(active.get("started") or ""),
        }

    async def busy(self) -> str | None:
        """What's running, in words, or None in the menu. Used to refuse replacing a game."""
        if self.zaparoo.connected:
            active = await self.active()
            if active:
                return str(active.get("mediaName") or "a game")
        core = await self.core()
        if core and core != MENU_CORE:
            return f"the {core} core"
        return None

    async def status(self) -> HostStatus:
        z = self.zaparoo
        now: NowPlaying | None = None
        library: Library | None = None
        if z.connected:
            try:
                media = await z.call("media")
                actives = media.get("active") if isinstance(media, dict) else None
                primary = [a for a in actives or [] if isinstance(a, dict) and a.get("slot", "primary") == "primary"]
                if primary:
                    now = self.now_playing(primary[0])
                db = media.get("database") if isinstance(media, dict) else None
                if isinstance(db, dict):
                    library = {
                        "indexed": bool(db.get("exists")),
                        "indexing": bool(db.get("indexing")),
                        "total_media": db.get("totalMedia") if isinstance(db.get("totalMedia"), int) else None,
                    }
            except MisterError as exc:
                log.debug("%s: media: %s", self.name, exc)
        core, attract, free = await asyncio.gather(self.core(), self.attract_mode(), self.free_space_mb())
        caps, limits_ = self.capabilities(library)
        ssh: Backend | None = None
        if self.shell is not None:
            ssh = {"connected": self.shell.connected, "detail": self.shell.error}
        return {
            "name": self.name,
            "model": self.settings.model,
            "reachable": z.connected or (self.shell is not None and self.shell.connected),
            "zaparoo": {"connected": z.connected, "detail": z.version if z.connected else z.error},
            "ssh": ssh,
            "now_playing": now,
            "core": core,
            "attract_mode": attract,
            "library": library,
            "free_space_mb": free,
            "capabilities": caps,
            "limitations": limits_,
        }

    def capabilities(self, library: Library | None = None) -> tuple[list[str], list[str]]:
        """What works on this MiSTer right now, and what doesn't with what would fix it (Sofabaton's model)."""
        caps: list[str] = []
        limits_: list[str] = []
        if self.zaparoo.connected:
            caps += ["search", "launch", "history", "screenshot", "nfc_read", "nfc_write", "nfc_map", "update"]
            if library is not None and not library["indexed"]:
                limits_.append(
                    f"Zaparoo on {self.name} hasn't indexed any games yet, so search finds nothing: open Zaparoo's "
                    f"web UI (http://{self.settings.host}:{self.settings.zaparoo_port}/app/) and update the media "
                    "database."
                )
        else:
            why = self.zaparoo.error or f"Zaparoo on {self.name} isn't connected yet."
            hint = "" if SETUP_HINT in why else f" {SETUP_HINT}"
            limits_.append(f"{why}{hint} Without it: no search, launching, history, screenshots or NFC.")
        if self.shell is None:
            limits_.append(
                f"SSH isn't set up for {self.name}: MiSTer.ini editing, save backups, attract-mode detection and "
                "the running core's name are unavailable. See the README's 'Set up your MiSTer'."
            )
        elif self.shell.error and not self.shell.connected:
            limits_.append(self.shell.error)
        else:
            caps += ["ini_read", "ini_write", "core_name", "attract_detection"]
            if self.app.backup_dir is not None:
                caps.append("backup_saves")
            else:
                limits_.append(
                    "backup_saves needs a folder to write to: set backup_dir in config.json, or "
                    "MISTERFPGA_BACKUP_DIR (any writable folder: a local disk, NFS, SMB, an OMV share)."
                )
        return caps, limits_

    # --- games -------------------------------------------------------------------------------
    async def search(self, query: str, system: str | None, limit: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"query": query, "maxResults": limit, "fuzzySystem": True}
        if system:
            params["systems"] = [system]
        result = await self.zaparoo.call("media.search", params)
        rows = result.get("results") if isinstance(result, dict) else None
        return [r for r in rows or [] if isinstance(r, dict)]

    async def history(self, mode: Literal["recent", "top"], system: str | None, limit: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit, "fuzzySystem": True}
        if system:
            params["systems"] = [system]
        if mode == "recent":
            params["distinctMedia"] = True
        result = await self.zaparoo.call("media.history" if mode == "recent" else "media.history.top", params)
        rows = result.get("entries") if isinstance(result, dict) else None
        return [r for r in rows or [] if isinstance(r, dict)]  # ASSUMPTION Z-HISTORY

    async def systems(self) -> list[dict[str, Any]]:
        result = await self.zaparoo.call("systems")
        rows = result.get("systems") if isinstance(result, dict) else None
        return [r for r in rows or [] if isinstance(r, dict)]

    async def cores(self) -> dict[str, tuple[str, str]]:
        """System id -> (core name, core path) its default launcher runs, e.g. "SNES" -> ("SNES", "_Console/SNES").

        The core name is what MiSTer.ini sections are named after ([SNES]).
        """
        result = await self.zaparoo.call("launchers")
        out: dict[str, tuple[str, str]] = {}
        for launcher in (result.get("launchers") if isinstance(result, dict) else None) or []:
            if not isinstance(launcher, dict) or launcher.get("backend") != "mister_core":
                continue
            core = launcher.get("misterCore")
            system = launcher.get("systemId")
            if not (isinstance(core, dict) and isinstance(system, str) and launcher.get("available", True)):
                continue
            name, path = core.get("name"), core.get("mglPath")
            if isinstance(name, str) and isinstance(path, str) and (system not in out or launcher.get("default")):
                out[system] = (name, path)
        return out

    async def game(self, media_id: int) -> tuple[str, str]:
        """(display name, zapScript) for a media row (ASSUMPTION Z-MEDIAID). Raises when it's gone."""
        try:
            result = await self.zaparoo.call("media.meta", {"mediaId": media_id})
        except ZaparooError as exc:
            raise MisterError(
                f"Game {self.name}/{media_id} isn't in {self.name}'s library any more ({exc}); search again."
            ) from None
        media = result.get("media") if isinstance(result, dict) else None
        if not isinstance(media, dict) or not media.get("zapScript") or media.get("isMissing"):
            raise MisterError(f"Game {self.name}/{media_id} isn't available on {self.name} any more; search again.")
        raw_title = media.get("title")
        title: dict[str, Any] = raw_title if isinstance(raw_title, dict) else {}
        name = str(title.get("name") or Path(str(media.get("path") or "")).stem or f"game {media_id}")
        return name, str(media["zapScript"])

    async def launch(self, media_id: int | None, system: str | None) -> Outcome:
        if (media_id is None) == (system is None):
            raise MisterError("Pass exactly one of game (a ref from search_games) or system (from list_systems).")
        running = await self.busy()
        if running:
            raise MisterError(
                f"{self.name} is running {running}. Call return_to_menu for {self.name} first (that ends the "
                "game, so unsaved progress is lost), then launch."
            )
        if media_id is not None:
            what, script = await self.game(media_id)
        else:
            assert system is not None
            known = {str(s.get("id")).lower(): str(s.get("id")) for s in await self.systems()}
            if not _SYSTEM_ID.fullmatch(system) or system.lower() not in known:
                raise MisterError(f"{self.name} has no system {system!r}; list_systems shows what it has.")
            system = known[system.lower()]
            what, script = f"the {system} core", f"**launch.system:{system}"
        if self.dry_run:
            return _outcome(self.name, "dry_run", f"DRY RUN: would launch {what} on {self.name} ({script}).")
        self._limit(self.launch_bucket, f"launch {what}")
        try:
            await self.zaparoo.call("run", {"text": script}, timeout=RUN_TIMEOUT)  # ASSUMPTION Z-RUN-WAITS
        except ZaparooError as exc:
            raise run_error(exc, self.name, what) from None
        confirmed = await self._confirm(lambda: self._is_running(media_id is not None))
        warn = "" if confirmed else f"Zaparoo accepted the launch, but {self.name} hasn't reported it running yet."
        return _outcome(self.name, "done", f"Launched {what} on {self.name}.", warn)

    async def _is_running(self, game: bool) -> bool:
        if game:
            return await self.active() is not None
        core = await self.core()
        return core is not None and core != MENU_CORE

    async def _confirm(self, check: Any) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CONFIRM_SECONDS
        while True:
            with contextlib.suppress(MisterError):
                if await check():
                    return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.5)

    async def return_to_menu(self) -> Outcome:
        running = await self.busy()
        if running is None:
            return _outcome(self.name, "unchanged", f"{self.name} is already in the menu.")
        if self.dry_run:
            return _outcome(self.name, "dry_run", f"DRY RUN: would stop {running} on {self.name}.")
        self._limit(self.launch_bucket, f"stop {running}")
        if self.zaparoo.connected:
            try:
                await self.zaparoo.call("stop")  # ASSUMPTION Z-STOP-MENU
            except ZaparooError as exc:
                raise MisterError(f"Zaparoo on {self.name} couldn't stop {running}: {exc}") from None
        else:
            await self._ssh("Returning to the menu without Zaparoo").run(Command.MENU)  # ASSUMPTION M-CMD
        confirmed = await self._confirm(lambda: self._in_menu())
        warn = "" if confirmed else f"{self.name} hasn't reported being back in the menu yet."
        return _outcome(self.name, "done", f"Stopped {running}; {self.name} is back in the menu.", warn)

    async def _in_menu(self) -> bool:
        return await self.busy() is None

    async def screenshot(self) -> tuple[bytes, str]:
        self._limit(self.screenshot_bucket, "take a screenshot")
        result = await self.zaparoo.call("screenshot", timeout=20.0)  # ASSUMPTION Z-SCREENSHOT
        if not isinstance(result, dict) or not isinstance(result.get("data"), str):
            raise MisterError(f"Zaparoo on {self.name} returned no image.")
        try:
            data = base64.b64decode(result["data"], validate=True)
        except (binascii.Error, ValueError):
            raise MisterError(f"Zaparoo on {self.name} returned an image that isn't valid base64.") from None
        return data, str(result.get("path") or "")

    # --- MiSTer.ini --------------------------------------------------------------------------
    async def ini_files(self) -> list[str]:
        return await self._ssh("Reading MiSTer.ini").list_inis()

    async def _ini_path(self, file: str) -> str:
        if not is_ini_name(file):
            raise MisterError(f"{file!r} isn't a MiSTer ini file name (MiSTer.ini, or MiSTer_<name>.ini).")
        files = await self.ini_files()
        if file not in files and file != "MiSTer.ini":
            raise MisterError(f"{self.name} has no {file}. It has: {', '.join(files) or '(none)'}.")
        return f"{SD}/{file}"

    async def read_ini(self, file: str) -> IniFile:
        path = await self._ini_path(file)
        shell = self._ssh("Reading MiSTer.ini")
        if not await shell.exists(path):
            return IniFile("")  # no MiSTer.ini yet: MiSTer runs on defaults
        return IniFile((await shell.read(path)).decode("utf-8", errors="replace"))

    async def set_ini(
        self, key: str, value: str | int | float | bool | None, section: str, file: str, dry_run: bool
    ) -> tuple[Outcome, Change, str | None]:
        info = key_info(key)
        name = check_section(section)
        new = None if value is None else check_value(info, value)
        if value is None and not info.settable:
            check_value(info, "")  # raises the "can't be set" error for denied and array keys
        path = await self._ini_path(file)
        ini = await self.read_ini(file)
        change = ini.set(name, info.name.lower(), new)
        what = f"{info.name.lower()} in [{name}] of {file} on {self.name}"
        warn = await self._section_warning(name)
        if not change.changed:
            unchanged = f"{what} is already {new if new is not None else 'unset'}."
            return _outcome(self.name, "unchanged", unchanged, warn), change, None
        if dry_run or self.dry_run:
            return _outcome(self.name, "dry_run", f"DRY RUN: would set {what} to {new}.", warn), change, None
        self._limit(self.ini_bucket, f"change {what}")
        shell = self._ssh("Editing MiSTer.ini")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = check_path(f"{path}.{stamp}.bak", "write")
        await shell.write_replace(path, change.text.encode(), backup)
        await self._prune_ini_backups(shell, path)
        detail = (
            f"Removed {what}." if new is None else f"Set {what} to {new} (was {change.old or 'unset'})."
        ) + " It takes effect the next time a core loads."  # ASSUMPTION M-INI-APPLY
        return _outcome(self.name, "done", detail, warn), change, backup

    async def _section_warning(self, section: str) -> str:
        """A warning when a core section names no core installed on this MiSTer (it may be a typo)."""
        if section in ("MiSTer", "arcade", "arcade_vertical") or section.startswith("video=") or section.endswith("*"):
            return ""
        if not self.zaparoo.connected:
            return ""
        with contextlib.suppress(MisterError):
            names = {name.lower() for name, _ in (await self.cores()).values()}
            if names and section.lower() not in names:
                return (
                    f"No installed core on {self.name} is called {section}; check the spelling (it's case-insensitive)."
                )
        return ""

    async def _prune_ini_backups(self, shell: MisterShell, path: str) -> None:
        mine = [b for b in await shell.list_backups() if b.startswith(path + ".")]
        for old in mine[: -limits.INI_BACKUPS_KEPT]:
            with contextlib.suppress(MisterError):
                await shell.remove(old)

    # --- saves -------------------------------------------------------------------------------
    async def backup(self, dry_run: bool, progress: Progress | None = None) -> BackupResult:
        shell = self._ssh("Backing up saves")
        if self.app.backup_dir is None:
            raise Unavailable(
                "backup_saves needs a folder to write to: set backup_dir in config.json, or MISTERFPGA_BACKUP_DIR."
            )
        if self._backup_lock.locked():
            raise MisterError(f"A backup of {self.name} is already running.")
        async with self._backup_lock:
            return await backup_saves(
                shell,
                self.name,
                self.app.backup_dir,
                self.app.backup_keep,
                dry_run=dry_run or self.dry_run,
                progress=progress,
            )

    # --- updates -----------------------------------------------------------------------------
    async def run_update(self) -> Outcome:
        running = await self.busy()
        if running:
            raise MisterError(
                f"{self.name} is running {running}. Update All can reboot the MiSTer, so return to the menu first."
            )
        if self.dry_run:
            return _outcome(self.name, "dry_run", f"DRY RUN: would start Update All on {self.name}.")
        self._limit(self.update_bucket, "start Update All")
        try:
            await self.zaparoo.call("run", {"text": UPDATE_SCRIPT}, timeout=RUN_TIMEOUT)  # ASSUMPTION Z-SCRIPT
        except ZaparooError as exc:
            raise MisterError(f"Zaparoo on {self.name} couldn't start Update All: {exc}") from None
        return _outcome(
            self.name,
            "done",
            f"Update All is running on {self.name}'s screen. It takes a few minutes, and the MiSTer may reboot "
            "when it finishes.",  # ASSUMPTION U-REBOOT
        )

    # --- NFC ---------------------------------------------------------------------------------
    async def readers(self) -> list[dict[str, Any]]:
        result = await self.zaparoo.call("readers")
        rows = result.get("readers") if isinstance(result, dict) else None
        return [r for r in rows or [] if isinstance(r, dict)]

    async def mappings(self) -> list[dict[str, Any]]:
        result = await self.zaparoo.call("mappings")
        rows = result.get("mappings") if isinstance(result, dict) else None
        return [r for r in rows or [] if isinstance(r, dict)]

    async def last_card(self) -> tuple[str, datetime] | None:
        """The card on the reader now, else the last one scanned: (uid, when)."""
        result = await self.zaparoo.call("tokens")
        if not isinstance(result, dict):
            return None
        candidates = [t for t in result.get("active") or [] if isinstance(t, dict)]
        if isinstance(result.get("last"), dict):
            candidates.append(result["last"])
        for token in candidates:
            uid = normalize_uid(str(token.get("uid") or ""))
            when = _parse_time(token.get("scanTime"))
            if uid and when is not None:
                return uid, when
        return None

    async def write_card(self, media_id: int, reader: str | None) -> Outcome:
        what, script = await self.game(media_id)
        writable = [
            r for r in await self.readers() if r.get("connected", True) and "write" in (r.get("capabilities") or [])
        ]
        if reader is not None:
            writable = [r for r in writable if reader in (r.get("readerId"), r.get("id"))]
        if not writable:
            raise MisterError(
                f"No NFC reader that can write is connected to {self.name}"
                + (f" with id {reader!r}" if reader else "")
                + ". Plug one in (a PN532 works), or use map_nfc_card with a read-only card."
            )
        if self.dry_run:
            return _outcome(self.name, "dry_run", f"DRY RUN: would write {what} to the card on {self.name}'s reader.")
        self._limit(self.nfc_bucket, f"write {what} to a card")
        params: dict[str, Any] = {"text": script}
        if reader is not None:
            params["readerId"] = writable[0].get("readerId")
        try:
            await self.zaparoo.call("readers.write", params, timeout=WRITE_TIMEOUT)  # ASSUMPTION Z-NFC
        except ZaparooError as exc:
            raise MisterError(f"Writing the card on {self.name} failed: {exc}. Is a card on the reader?") from None
        return _outcome(self.name, "done", f"Wrote {what} to the card on {self.name}'s reader.")

    async def map_card(self, media_id: int, uid: str | None) -> Outcome:
        what, script = await self.game(media_id)
        if uid is not None:
            card = normalize_uid(uid)
            if not card:
                raise MisterError(f"{uid!r} doesn't look like a card UID (hex digits, e.g. 04a2b3c4d5e680).")
        else:
            last = await self.last_card()
            if last is None or datetime.now(UTC) - last[1] > CARD_FRESH:
                raise MisterError(
                    f"No card has been scanned on {self.name} in the last 2 minutes. Scan the card on "
                    f"{self.name}'s reader, then try again (or pass uid)."
                )
            card = last[0]
        for m in await self.mappings():
            if m.get("type") == "uid" and normalize_uid(str(m.get("pattern") or "")) == card:
                label = m.get("label") or m.get("override") or "something"
                raise MisterError(
                    f"Card {card} is already mapped on {self.name} ({label}). Remove that mapping in "
                    "the Zaparoo app first if you want to reuse the card."
                )
        if self.dry_run:
            return _outcome(self.name, "dry_run", f"DRY RUN: would map card {card} to {what} on {self.name}.")
        self._limit(self.nfc_bucket, f"map a card to {what}")
        await self.zaparoo.call(
            "mappings.new",
            {
                "label": f"mcp: {what}"[:100],
                "enabled": True,
                "type": "uid",
                "match": "exact",
                "pattern": card,
                "override": script,
            },
        )
        return _outcome(self.name, "done", f"Card {card} now launches {what} on {self.name}.")


def normalize_uid(uid: str) -> str:
    """Zaparoo's UID normal form: no spaces or colons, lower case (ASSUMPTION Z-NFC); '' if it isn't hex."""
    s = uid.replace(" ", "").replace(":", "").replace("-", "").lower()
    return s if _UID.fullmatch(s) else ""


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)

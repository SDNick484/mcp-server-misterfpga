"""`doctor`: check each MiSTer layer by layer, and say what failed and what to do. Read-only.

The layers, in the order a failure in one explains the next:

  config     config.json parses; hosts, ssh and backup_dir settings make sense
  zaparoo    TCP to the Zaparoo port -> the WebSocket API -> version -> the media library
  ssh        key and pinned host key set -> login -> /tmp/CORENAME -> MiSTer.ini tracking settings
  backups    backup_dir exists and is writable

Nothing here changes a MiSTer: no launches, no screenshots (they write a file),
no INI writes. `--dump DIR` also writes each MiSTer's read-only answers
(redacted) to DIR/<host>.json; those recordings can go in
tests/fixtures/recorded/ so test_recorded.py replays real answers against the
parsing code.
"""

from __future__ import annotations

import asyncio
import errno
import json
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from .assumptions import ASSUMPTIONS
from .backup import slug
from .config import Settings
from .errors import MisterError
from .host import MisterHost
from .ini import IniFile
from .logsafe import redact
from .ssh import SD, Command

Level = Literal["ok", "warn", "fail", "skip"]


@dataclass
class Step:
    layer: str
    check: str
    level: Level
    detail: str
    fix: str = ""


@dataclass
class Report:
    host: str
    steps: list[Step] = field(default_factory=list)

    def add(self, layer: str, check: str, level: Level, detail: str, fix: str = "") -> Level:
        self.steps.append(Step(layer, check, level, detail, fix))
        return level


async def _tcp(host: str, port: int, timeout: float) -> str | None:
    """None when something accepts TCP on host:port, else why not."""
    try:
        async with asyncio.timeout(timeout):
            _, writer = await asyncio.open_connection(host, port)
        writer.close()
        await writer.wait_closed()
        return None
    except TimeoutError:
        return "timed out: is the MiSTer switched on and on the network?"
    except OSError as exc:
        if exc.errno == errno.ECONNREFUSED:
            return (
                "connection refused: the MiSTer is up, but Zaparoo Core isn't listening (not installed, or not started)"
            )
        return f"{exc.__class__.__name__}: {exc}"


async def check_host(h: MisterHost, settings: Settings, timeout: float, dump: Path | None) -> Report:
    r = Report(h.name)
    s = h.settings
    recorded: dict[str, Any] = {}

    # --- zaparoo ---------------------------------------------------------------------------------
    why = await _tcp(s.host, s.zaparoo_port, timeout)
    if why is not None:
        r.add(
            "zaparoo",
            f"TCP {s.zaparoo_port}",
            "fail",
            why,
            "Install Zaparoo from Update All (Tools & Scripts), then Scripts → zaparoo → enable the service.",
        )
    else:
        r.add("zaparoo", f"TCP {s.zaparoo_port}", "ok", "something is listening")
        await h.zaparoo.start()
        try:
            async with asyncio.timeout(timeout + 2):
                await h.zaparoo.tried.wait()
        except TimeoutError:
            pass
        if not h.zaparoo.connected:
            r.add(
                "zaparoo",
                "WebSocket API",
                "fail",
                h.zaparoo.error or "no connection",
                "Check zaparoo_port, and that this is Zaparoo Core (http://<mister>:7497/app/ should open).",
            )
        else:
            r.add("zaparoo", "WebSocket API", "ok", f"Zaparoo Core {h.zaparoo.version or '(version unknown)'}")
            try:
                media = await h.zaparoo.call("media")
                recorded["media"] = media
                db = media.get("database", {}) if isinstance(media, dict) else {}
                if db.get("exists"):
                    r.add("zaparoo", "media library", "ok", f"{db.get('totalMedia', '?')} games indexed")
                else:
                    r.add(
                        "zaparoo",
                        "media library",
                        "warn",
                        "no games indexed yet",
                        "Open Zaparoo's web UI and update the media database, or search_games finds nothing.",
                    )
                for method in ("version", "systems", "launchers", "readers", "mappings"):
                    recorded[method] = await h.zaparoo.call(method)
                recorded["media.search"] = await h.zaparoo.call("media.search", {"query": "", "maxResults": 5})
            except MisterError as exc:
                r.add(
                    "zaparoo",
                    "legacy access",
                    "fail",
                    str(exc),
                    "Zaparoo refused an unauthenticated client. Is encryption or an API key turned on in its "
                    "config.toml? This server needs it off on MiSTer (docs/DESIGN.md §1.4).",
                )
        await h.zaparoo.stop()

    # --- ssh -------------------------------------------------------------------------------------
    if h.shell is None:
        r.add(
            "ssh",
            "configured",
            "skip",
            "no ssh entry: INI, backups, attract detection and core name are off",
            "ssh-copy-id root@<mister> (password 1), then mcp-server-misterfpga check --host <address> --ssh",
        )
    else:
        try:
            core = (await h.shell.run(Command.CORENAME)).strip()
            r.add("ssh", "login", "ok", f"logged in as {h.shell.settings.user}; running core: {core or '?'}")
            text = (
                (await h.shell.read(f"{SD}/MiSTer.ini")).decode("utf-8", "replace")
                if await h.shell.exists(f"{SD}/MiSTer.ini")
                else ""
            )
            ini = IniFile(text).sections()
            main = next((v for k, v in ini.items() if k.lower() == "mister"), {})
            missing = [k for k in ("recents", "log_file_entry") if main.get(k) != "1"]
            if missing:
                r.add(
                    "ssh",
                    "tracking settings",
                    "warn",
                    f"{', '.join(missing)} not 1 in [MiSTer]: Zaparoo may not identify games launched from the menu",
                    "set_ini key=recents value=1 and key=log_file_entry value=1 (Zaparoo's own advice).",
                )
            else:
                r.add("ssh", "tracking settings", "ok", "recents=1 and log_file_entry=1")
        except MisterError as exc:
            r.add("ssh", "login", "fail", str(exc))
        finally:
            await h.shell.close()

    # --- backups ---------------------------------------------------------------------------------
    if settings.backup_dir is None:
        r.add(
            "backups",
            "backup_dir",
            "skip",
            "not set: backup_saves is off",
            "Set backup_dir in config.json or MISTERFPGA_BACKUP_DIR to any writable folder.",
        )
    elif not settings.backup_dir.is_dir():
        r.add("backups", "backup_dir", "fail", f"{settings.backup_dir} doesn't exist or isn't mounted")
    else:
        try:
            with tempfile.NamedTemporaryFile(dir=settings.backup_dir):
                pass
            r.add("backups", "backup_dir", "ok", f"{settings.backup_dir} is writable")
        except OSError as exc:
            r.add("backups", "backup_dir", "fail", f"can't write to {settings.backup_dir}: {exc}")

    if dump is not None and recorded:
        dump.mkdir(parents=True, exist_ok=True)
        (dump / f"{slug(h.name)}.json").write_text(redact(json.dumps(recorded, indent=1)) + "\n")
    return r


async def run(settings: Settings, only: str | None, timeout: float, dump: Path | None) -> list[Report]:
    reports: list[Report] = []
    if settings.problems or not settings.hosts:
        cfg = Report("(config)")
        for p in settings.problems:
            cfg.add("config", "config.json", "warn", p)
        if not settings.hosts:
            cfg.add(
                "config",
                "hosts",
                "fail",
                "no MiSTer configured",
                "mcp-server-misterfpga check --host <address> (see the README's 'Set up your MiSTer')",
            )
        reports.append(cfg)
    for hs in settings.hosts:
        h = MisterHost(hs, settings)
        if only is None or h.answers_to(only):
            reports.append(await check_host(h, settings, timeout, dump))
    return reports


def render(reports: list[Report], as_json: bool) -> str:
    if as_json:
        return json.dumps([{"host": r.host, "steps": [asdict(s) for s in r.steps]} for r in reports], indent=1)
    marks = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL", "skip": "--  "}
    lines: list[str] = []
    for r in reports:
        lines.append(f"\n{r.host}")
        for s in r.steps:
            lines.append(f"  [{marks[s.level]}] {s.layer:<8} {s.check}: {s.detail}")
            if s.fix and s.level != "ok":
                lines.append(f"         fix: {s.fix}")
    pending = [a for a in ASSUMPTIONS if a.status != "hardware-verified"]
    if pending:
        lines.append(f"\n{len(pending)} protocol assumptions not yet checked on hardware (HARDWARE_VALIDATION.md):")
        lines += [f"  {a.id:<15} {a.confidence:<6} {a.status}" for a in pending]
    return "\n".join(lines).lstrip("\n")


def exit_code(reports: list[Report]) -> int:
    return 1 if any(s.level == "fail" for r in reports for s in r.steps) else 0

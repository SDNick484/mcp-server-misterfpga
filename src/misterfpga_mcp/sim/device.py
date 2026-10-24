"""The state of one simulated MiSTer, shared by its fake Zaparoo Core and its fake SSH server.

A real MiSTer has one truth (which core is loaded, which game is running) that
both of its interfaces report; so does the simulator. Launching through the
fake Zaparoo changes what the fake SSH server's /tmp/CORENAME says, and
starting attract mode shows up in both.

The library, history, readers and mappings come from a JSON fixture in
sim/fixtures/, shared by the tests and `simulate`.
"""

from __future__ import annotations

import copy
import json
import struct
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

MENU = "MENU"


def load_fixture(name: str) -> dict[str, Any]:
    text = resources.files("misterfpga_mcp.sim").joinpath("fixtures", f"{name}.json").read_text()
    data: dict[str, Any] = json.loads(text)
    return data


def tiny_png(width: int = 4, height: int = 3) -> bytes:
    """A valid PNG (solid colour), standing in for a MiSTer screenshot."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    raw = b"".join(b"\x00" + b"\x20\x40\x80" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class FakeDevice:
    """One MiSTer. ``sd`` is a real folder standing in for the SD card (the fake SSH server's chroot)."""

    name: str
    fixture: dict[str, Any]
    sd: Path
    core: str = MENU
    active: dict[str, Any] | None = None
    attract: bool = False  # MiSTer Super Attract Mode's loop_core process is running
    scripts_run: list[str] = field(default_factory=list)
    cards_written: list[str] = field(default_factory=list)
    tokens_active: list[dict[str, Any]] = field(default_factory=list)
    last_token: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.fixture = copy.deepcopy(self.fixture)
        self.mappings: list[dict[str, Any]] = list(self.fixture.get("mappings", []))
        self.history: list[dict[str, Any]] = list(self.fixture.get("history", []))
        self._populate_sd()

    @classmethod
    def from_fixture(cls, name: str, sd: Path) -> FakeDevice:
        data = load_fixture(name)
        return cls(name=data["name"], fixture=data, sd=sd)

    def _populate_sd(self) -> None:
        fat = self.sd / "media" / "fat"
        (fat / "screenshots").mkdir(parents=True, exist_ok=True)
        for rel, text in self.fixture.get("files", {}).items():
            path = fat / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text(text)
        for rel, size in self.fixture.get("saves", {}).items():
            path = fat / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(bytes(i % 251 for i in range(size)))

    # --- the library ---------------------------------------------------------------------------
    @property
    def media(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = self.fixture["media"]
        return rows

    def systems(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for s in self.fixture["systems"]:
            count = sum(1 for m in self.media if m["system"]["id"] == s["id"])
            out.append({**s, "mediaCount": count})
        return out

    def launchers(self) -> list[dict[str, Any]]:
        launchers: list[dict[str, Any]] = self.fixture["launchers"]
        return launchers

    def by_id(self, media_id: int) -> dict[str, Any] | None:
        return next((m for m in self.media if m["mediaId"] == media_id), None)

    def by_script(self, script: str) -> dict[str, Any] | None:
        return next((m for m in self.media if m["zapScript"] == script), None)

    def core_for(self, system_id: str) -> str | None:
        for launcher in self.launchers():
            if launcher.get("systemId", "").lower() == system_id.lower() and launcher.get("misterCore"):
                core: str = launcher["misterCore"]["name"]
                return core
        return None

    # --- what's running ---------------------------------------------------------------------------
    def start_game(self, media: dict[str, Any]) -> dict[str, Any]:
        self.core = self.core_for(media["system"]["id"]) or media["system"]["id"]
        self.active = {
            "mediaId": media["mediaId"],
            "launcherId": media["system"]["id"],
            "systemId": media["system"]["id"],
            "systemName": media["system"]["name"],
            "mediaPath": "/media/fat/games/" + media["path"],
            "relativePath": media["path"],
            "mediaName": media["name"],
            "started": now(),
            "zapScript": media["zapScript"],
        }
        return self.active

    def start_core(self, system_id: str) -> None:
        self.core = self.core_for(system_id) or system_id
        self.active = None

    def to_menu(self) -> dict[str, Any] | None:
        """Back to the menu. A game that was running becomes a history entry, as Zaparoo records it."""
        stopped, self.active, self.core = self.active, None, MENU
        if stopped:
            self.history.insert(0, self.session_record(stopped, datetime.now(UTC)))
        return stopped

    @staticmethod
    def session_record(active: dict[str, Any], ended: datetime) -> dict[str, Any]:
        """A media.history entry for a session of `active` that ended at `ended` (ASSUMPTION Z-HISTORY)."""
        started = datetime.fromisoformat(active["started"])
        return {
            **{k: active[k] for k in ("mediaId", "systemId", "systemName", "mediaName", "mediaPath", "launcherId")},
            "zapScript": active.get("zapScript", ""),
            "hasCover": False,
            "startedAt": active["started"],
            "endedAt": ended.isoformat(timespec="seconds"),
            "playTime": max(0, int((ended - started).total_seconds())),
            "sessionSource": "active_media",
            "sessionConfidence": "exact",
        }

    def scan(self, uid: str, text: str = "") -> dict[str, Any]:
        token = {"type": "nfc", "uid": uid, "text": text, "data": "", "scanTime": now(), "readerId": "pn532-sim"}
        self.last_token = token
        return token

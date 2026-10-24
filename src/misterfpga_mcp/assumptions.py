"""Every protocol detail this server relies on that hasn't been checked on a real MiSTer.

Why a registry instead of comments (the siblings' pattern): a comment saying
"unverified" is easy to miss and never goes away. Here each assumption has an
id that:
  - code cites next to where it depends on it (``# ASSUMPTION Z-RUN-WAITS``),
  - README.md lists in its verification table, with confidence and status,
  - HARDWARE_VALIDATION.md cites in the step that confirms it,
  - `mcp-server-misterfpga doctor` prints, with its status.

tests/test_assumptions.py fails if those drift apart.

To record a hardware result, change ``status`` to "hardware-verified" (or
"hardware-contradicted", with what you saw in ``note``) and commit it.

Id prefixes say where a claim comes from: Z- Zaparoo Core, M- MiSTer itself
(Main_MiSTer, MiSTer Linux), S- MiSTer Super Attract Mode, U- Update All,
H- your particular hardware.

Confidence is about the *claim*, judged from its source:
  high   - documented by the project, or read directly in its source
  medium - inferred from its source or docs; plausible but indirect
  low    - our own guess; the simulator implements it but nothing confirms it
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Confidence = Literal["high", "medium", "low"]
Status = Literal["simulator-only", "hardware-verified", "hardware-contradicted"]


@dataclass(frozen=True)
class Assumption:
    id: str
    claim: str
    source: str
    confidence: Confidence
    status: Status = "simulator-only"
    note: str = ""


ASSUMPTIONS: tuple[Assumption, ...] = (
    Assumption(
        "Z-ENDPOINT",
        "Zaparoo Core's API is JSON-RPC 2.0 over a WebSocket at ws://<host>:7497/api/v0.1, with string ids; "
        "the text 'ping' is answered with 'pong'.",
        "zaparoo-core docs/api/index.md (declared stable, additive changes only)",
        "high",
    ),
    Assumption(
        "Z-LEGACY",
        "On MiSTer, Zaparoo admits an unauthenticated plaintext client from the LAN: encryption is off by "
        "default there and MiSTer is a grandfathered 'legacy' platform.",
        "zaparoo-core pkg/api/permissions/permissions.go; cmd/mister sets no encryption default "
        "(cmd/linux, steamos and windows do)",
        "high",
    ),
    Assumption(
        "Z-LEGACY-CAPS",
        "A legacy client may call run, stop, media.*, systems, launchers, mappings, readers and tokens, and "
        "(grandfathered on MiSTer) screenshot; not device.status or update.*.",
        "zaparoo-core docs/api/methods.md, Access",
        "high",
    ),
    Assumption(
        "Z-RUN-WAITS",
        "run returns only after the ZapScript has finished executing (Core waits up to 30 s), and reports "
        "failures as error.data.category (media_not_found, launch_repair, busy, ...).",
        "zaparoo-core docs/api/methods.md, run",
        "high",
    ),
    Assumption(
        "Z-STOP-MENU",
        "stop returns a running FPGA core to the MiSTer menu.",
        "zaparoo-core pkg/api/methods/run.go (StopForMenu) -> platforms/mister ReturnToMenu -> LaunchMenu",
        "medium",
    ),
    Assumption(
        "Z-MEDIAID",
        "media.meta with a mediaId from media.search returns that row's zapScript; mediaIds stay valid until "
        "the library is reindexed.",
        "zaparoo-core docs/api/methods.md, media.meta ('opaque media database row ID')",
        "medium",
    ),
    Assumption(
        "Z-TRACKS-MENU",
        "Games launched from the MiSTer's own menu (not through Zaparoo) still produce media.started and "
        "media.stopped; matching the exact game needs recents=1 and log_file_entry=1 in MiSTer.ini.",
        "zaparoo-core pkg/platforms/mister/tracker; zaparoo.org docs/platforms/mister, Game tracking",
        "medium",
    ),
    Assumption(
        "Z-NOTIFY",
        "media.started carries systemId, systemName, mediaPath and mediaName; media.stopped adds launcherId "
        "and elapsed (seconds).",
        "zaparoo-core docs/api/notifications.md",
        "high",
    ),
    Assumption(
        "Z-HISTORY",
        "media.history and media.history.top include sessions of games launched from the MiSTer menu.",
        "zaparoo-core docs/api/methods.md, media.history (sessionSource active_media)",
        "medium",
    ),
    Assumption(
        "Z-SESSION-TIMES",
        "For one play session, media.active's started and the media.history entry's startedAt are the same "
        "instant (RFC 3339, the MiSTer's clock), within a minute; history's playTime is seconds played.",
        "zaparoo-core docs/api/methods.md, media.active and media.history (one session record behind both)",
        "medium",
    ),
    Assumption(
        "Z-SCREENSHOT",
        "screenshot returns {path, data (base64 PNG), size} on MiSTer and saves the file under /media/fat/screenshots.",
        "zaparoo-core docs/api/methods.md, screenshot",
        "high",
    ),
    Assumption(
        "Z-NFC",
        "readers.write waits for a card on the reader; mapping UIDs are normalized (no spaces or colons, "
        "lower case); a scanned card shows up in tokens / tokens.history with its uid.",
        "zaparoo-core docs/api/methods.md, readers, mappings, tokens",
        "medium",
    ),
    Assumption(
        "Z-SCRIPT",
        "The ZapScript **mister.script:update_all.sh runs Update All on the TV, and run returns once it has started.",
        "zaparoo.org docs/zapscript/mister.md, mister.script",
        "low",
    ),
    Assumption(
        "M-INI-SECTIONS",
        "MiSTer.ini sections are [MiSTer], [arcade], [arcade_vertical], [video=<mode>] and [<core name>] "
        "(case-insensitive, optional trailing * as a prefix wildcard); a line starting with + is an include.",
        "Main_MiSTer cfg.cpp ini_get_section()",
        "high",
    ),
    Assumption(
        "M-INI-KEYS",
        "The keys MiSTer.ini accepts, with their types and ranges, are those in ini_vars[] (Main_MiSTer 2dd3662); "
        "keys are case-insensitive and ';' starts a comment.",
        "Main_MiSTer cfg.cpp ini_vars[], ini_getline()",
        "high",
    ),
    Assumption(
        "M-INI-APPLY",
        "A MiSTer.ini change takes effect the next time a core loads.",
        "Main_MiSTer user_io.cpp calls cfg_parse() at core start; mrext Remote's INI docs",
        "medium",
    ),
    Assumption(
        "M-INI-RENAME",
        "Over SFTP, writing MiSTer.ini.tmp and renaming it over MiSTer.ini works on the SD card's filesystem.",
        "none; our guess (exFAT/FAT rename semantics through MiSTer Linux's SFTP server)",
        "low",
    ),
    Assumption(
        "M-CMD",
        "Writing 'load_core <path>' to /dev/MiSTer_cmd loads that core; load_core /media/fat/menu.rbf is the menu.",
        "Main_MiSTer input.cpp (the MiSTer_cmd handler); MiSTer_SAM uses the same command",
        "high",
    ),
    Assumption(
        "M-CORENAME",
        "/tmp/CORENAME holds the running core's name, MENU in the menu.",
        "zaparoo-core pkg/platforms/mister/config (CoreNameFile, MenuCore)",
        "high",
    ),
    Assumption(
        "M-PATHS",
        "Saves are under /media/fat/saves and save states under /media/fat/savestates, one folder per core.",
        "MiSTer conventions (Main_MiSTer, Zaparoo's device backups)",
        "medium",
    ),
    Assumption(
        "M-SSH",
        "MiSTer Linux accepts root SSH logins on port 22 with a key added by ssh-copy-id, and serves SFTP.",
        "MiSTer Linux defaults (root password 1); Update All's own dev tooling uses ssh/scp as root",
        "medium",
    ),
    Assumption(
        "S-ACTIVE",
        "MiSTer Super Attract Mode is cycling games exactly while a process with 'MiSTer_SAM_on.sh loop_core' "
        "in its arguments exists (visible to busybox 'ps -o args').",
        "MiSTer_SAM MiSTer_SAM_on.sh there_can_be_only_one()",
        "medium",
    ),
    Assumption(
        "S-HANDOFF",
        "With playcurrentgame=yes, picking up a controller during attract mode stops SAM and reloads the game, "
        "producing a new media.started after the loop_core process is gone.",
        "MiSTer_SAM MiSTer_SAM_on.sh play_or_exit",
        "medium",
    ),
    Assumption(
        "U-REBOOT",
        "Update All may reboot the MiSTer when it finishes (autoreboot defaults to true).",
        "Update_All_MiSTer src/update_all/config.py",
        "high",
    ),
    Assumption(
        "H-SS1",
        "The Retro Remake SuperStation One runs MiSTer Linux with the same paths, and runs Zaparoo Core the same "
        "way a DE10-Nano based MiSTer does.",
        "none",
        "low",
    ),
)

BY_ID: dict[str, Assumption] = {a.id: a for a in ASSUMPTIONS}

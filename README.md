# mcp-server-misterfpga

An [MCP](https://modelcontextprotocol.io) server for [MiSTer FPGA](https://github.com/MiSTer-devel)
devices: find and launch games, see what's being played and what's been played most, take
screenshots, make NFC cards, change `MiSTer.ini` settings (globally or per core), back up saves,
and start Update All, from Claude Code, Claude Desktop or, through a Cloudflare Tunnel, the Claude apps
anywhere. It talks to each MiSTer through
[Zaparoo Core](https://zaparoo.org)'s API and, for what Zaparoo doesn't cover, SSH. One server
drives several MiSTers, and a companion service publishes what each one plays to Home
Assistant over MQTT ([Play tracking](#play-tracking-in-home-assistant)).

A sibling of [mcp-server-harmony](https://github.com/SDNick484/mcp-server-harmony),
[mcp-server-sofabaton](https://github.com/SDNick484/mcp-server-sofabaton),
[mcp-server-webos](https://github.com/SDNick484/mcp-server-webos) and
[mcp-server-batocera](https://github.com/SDNick484/mcp-server-batocera), with the same
conventions, and sharing code with them through
[mcp-server-shared](https://github.com/SDNick484/mcp-server-shared).
[docs/DESIGN.md](docs/DESIGN.md) is the design, the research behind it, and the decisions.

**Status:** verified against the simulator only. [HARDWARE_VALIDATION.md](HARDWARE_VALIDATION.md)
is the checklist for a first session with real MiSTers.

## Set up your MiSTer

### 1. Zaparoo Core

Zaparoo isn't on a MiSTer by default. From Zaparoo's
[MiSTer page](https://zaparoo.org/docs/platforms/mister/):

1. Run **Update All**. During its countdown, press UP to open its settings, turn on **Zaparoo**
   under **Tools & Scripts**, choose **SAVE**, and let it run.
2. On the MiSTer, open **Scripts → zaparoo** and accept the prompt to start it as a service at
   boot.
3. From another machine, open `http://<mister-ip>:7497/app/`: that's Zaparoo's web UI. Let it
   build its media database (that's what `search_games` searches).
4. For accurate tracking of games launched from the MiSTer's own menu, Zaparoo asks for
   `recents=1` and `log_file_entry=1` in `[MiSTer]` of `MiSTer.ini`. Once SSH works (below),
   `set_ini` can do it; `doctor` tells you when they're off.

This server talks to Zaparoo without a key or pairing: on MiSTer, Zaparoo admits unauthenticated
clients from the LAN (`Z-LEGACY` below). Don't configure a Zaparoo API key for it: once any key is
configured, Zaparoo refuses every client without one, the Zaparoo phone app included.

### 2. SSH (for MiSTer.ini, save backups, attract-mode detection and the running core)

MiSTer's root password is `1`. Give it a key once, from the machine this server runs on:

```sh
ssh-keygen -t ed25519 -N "" -f ~/.ssh/mister_ed25519
ssh-copy-id -i ~/.ssh/mister_ed25519.pub root@<mister-ip>
```

SSH is optional per MiSTer; without it, `get_status` lists what's unavailable.

### 3. Tell the server about it

```sh
mcp-server-misterfpga check --host <mister-ip> --name "Family Room" --model "SuperStation One" \
    --ssh --ssh-key ~/.ssh/mister_ed25519
```

`check` confirms Zaparoo answers, shows the MiSTer's SSH host key fingerprint and **pins** it once
you say yes, tests the login, and saves the MiSTer to `config.json`. Name MiSTers by where they
are; `--model` says what each one is (a MiSTer Pi, a SuperStation One, a DE10-Nano), because
nothing on the network says so reliably. Then:

```sh
mcp-server-misterfpga doctor
```

## Install

```sh
git clone https://github.com/SDNick484/mcp-server-misterfpga.git && cd mcp-server-misterfpga
python -m venv .venv && . .venv/bin/activate && pip install -e .
```

Python 3.11 or newer. Every compiled dependency ships a musl wheel, so Alpine needs no compiler.

## Use it from Claude

```sh
claude mcp add misterfpga -- /path/to/.venv/bin/mcp-server-misterfpga     # Claude Code, stdio
```

For Claude Desktop, add it to `claude_desktop_config.json` under `mcpServers` with the same
command. To use it from claude.ai or the mobile app too, run it as a service and add it as a connector
(see [Remote access](#remote-access-stdio-or-http-behind-cloudflare-access)).

### Tools

| Tool | Does | Annotations |
| --- | --- | --- |
| `get_status` | each MiSTer: model, what's playing, running core, attract mode, library size, free space, capabilities and limitations | read-only |
| `list_systems` | systems each MiSTer can launch, with game counts and the core that runs each | read-only |
| `search_games` | find games by name, optionally in one system; each result has a `ref` | read-only |
| `play_history` | recently played, or most played by total time, from Zaparoo's own records | read-only |
| `get_ini` | `MiSTer.ini` (or an alternate) parsed into sections, as MiSTer reads it | read-only |
| `list_nfc` | NFC readers, card mappings, the last card scanned | read-only |
| `launch` | a game by `ref`, or a system's core with no game; confirmed running; refuses while anything runs | idempotent |
| `return_to_menu` | end what's running (unsaved progress is lost) | destructive |
| `screenshot` | what the MiSTer shows, as an image | — |
| `set_ini` | one `MiSTer.ini` key, global or per core, with a backup first; `dry_run` shows the diff | idempotent |
| `backup_saves` | saves and save states into a dated snapshot in your backup folder | idempotent |
| `run_update` | start Update All on the MiSTer's screen (it may reboot); needs `host` and `confirm` | destructive |
| `write_nfc_card` | write a game onto the card on the reader (overwrites the card) | destructive |
| `map_nfc_card` | make a card's UID launch a game, without writing to it | idempotent |

A **ref** is `<MiSTer>/<id>`, e.g. `Family Room/4812`: the same game on two MiSTers has two
refs, and the ref decides which TV it launches on. Actions return
`{"host", "outcome": "done" | "unchanged" | "dry_run", "detail", "warnings"}`; failures are tool
errors that say what to do ("Family Room is running Super Metroid. Call return_to_menu ...",
"Which MiSTer should open the core? Pass host: Office, Family Room.").

Resource `misterfpga://ini-keys`: every key `set_ini` accepts, with type and range, and the
refused ones with the reason. Prompt `make_nfc_cards`: one card at a time, for a list of games.

### What the model can't do

- **No paths, no ZapScript, no shell.** Games launch by `ref`; the server asks Zaparoo for that
  game's own launch script. A system launch is built from a validated system id. SSH runs five
  fixed commands, nothing else.
- **SFTP stays in its lane:** reads of `MiSTer*.ini` and `saves/`, `savestates/`; writes of
  `MiSTer*.ini` (with a backup); deletes of this server's own old INI backups only.
- **No Zaparoo settings, profiles, playtime limits, device power or Zaparoo updates:** the
  methods it may send are an allow-list, pinned by a test.
- **`set_ini` refuses** `main` (another Main binary), `bootcore`, `shared_folder` and `debug`,
  plus array keys (`player_1_controller`, ...); values are checked against MiSTer's own table.
- **Never guess a MiSTer.** An action that could mean either MiSTer is refused, naming both.
- **Never replace a running game.** `launch` refuses; ending a game is its own call.
- Rate limits per MiSTer: launches and returns to the menu (4, then 1 per 15 s), screenshots,
  INI writes, NFC, and Update All (once an hour).

### Dry run

`serve --dry-run` (or `MISTERFPGA_DRY_RUN=1`) reads the MiSTers but changes nothing: every action
returns `outcome: "dry_run"` with what it would do. `set_ini` and `backup_saves` also take a
per-call `dry_run`.

## config.json

`~/.config/mcp-server-misterfpga/config.json` (or `$MISTERFPGA_CONFIG_DIR`); `check` writes it.

```json
{
  "hosts": [
    {"host": "192.168.1.70", "name": "Office", "model": "MiSTer Pi",
     "ssh": {"key": "~/.ssh/mister_ed25519", "host_key": "ssh-ed25519 AAAA..."}},
    {"host": "192.168.1.71", "name": "Family Room", "model": "SuperStation One",
     "manufacturer": "Retro Remake"}
  ],
  "backup_dir": "/mnt/backups/mister-saves",
  "backup_keep": 30
}
```

- `ssh`: `key` (no passwords are stored), `host_key` (pinned by `check --ssh`; a host without one
  is never connected to), optional `user` (root) and `port` (22).
- `zaparoo_port`: 7497 unless you changed Zaparoo's `api_port`.
- `backup_dir` (or `$MISTERFPGA_BACKUP_DIR`) has **no default**: any folder this server can
  write to (a local disk, NFS, SMB, an OpenMediaVault share mounted into the LXC). Until it's set,
  `backup_saves` says how to set it.
- `MISTERFPGA_HOSTS` (comma-separated addresses) replaces the list, keeping names from the file.

## Backups

`backup_saves` reads `/media/fat/saves` and `/media/fat/savestates` over SFTP into
`<backup_dir>/<host>/<date_time>/`. Files unchanged since the last snapshot (same size and
modification time) are hard-linked to it, as `rsync --link-dest` does: every snapshot is
complete on its own, and only changes cost space. A snapshot is written as `.partial` and renamed
when complete. The newest `backup_keep` (30) are kept. The MiSTer is only read.

## Play tracking in Home Assistant

`mcp-server-misterfpga track` publishes what each MiSTer plays to Home Assistant over MQTT, with
the same entities and payloads as
[mcp-server-batocera](https://github.com/SDNick484/mcp-server-batocera)'s playtracker, so one
automation or dashboard card covers the arcade and both MiSTers. It runs as a second service next
to the MCP server, not on the MiSTer: Zaparoo already pushes `media.started` / `media.stopped`
over the network, so nothing goes on the SD card and a reflash can't lose it. It covers games
launched any way: from the MiSTer's menu, an NFC card, the Zaparoo app, or `launch`.

### What gets logged

- A **session** is a game from start to quit. Sessions under `MISTERFPGA_MIN_SESSION_SECONDS`
  (default 60) aren't logged.
- **Missed sessions are caught up.** If the tracker or a MiSTer's connection was down, it reads
  Zaparoo's own play history on reconnect and publishes what it missed, back to the last session it
  published (kept in `/var/lib/mcp-server-misterfpga/track-state.json`). The very first run starts
  from now instead of replaying all of Zaparoo's history.
- A session's `started` comes from the MiSTer, and its length from Zaparoo's own count, so they
  agree with `play_history`.
- **Not yet: attract mode.** MiSTer Super Attract Mode's demo games are real launches, so until
  attract detection lands they're logged like any other session over the minimum length.

### Install

1. In Home Assistant, create a user for it (*Settings > People > Users*, e.g. `misterfpga`). The
   Mosquitto add-on accepts Home Assistant users.
2. In `/etc/mcp-server-misterfpga/env` (see [Service and Cloudflare](#service-and-cloudflare)), set
   `MISTERFPGA_MQTT_HOST`, `_USER` and `_PASSWORD`. The file is readable by the service user only.
3. Start it: `rc-update add mcp-server-misterfpga-track default && rc-service mcp-server-misterfpga-track start`.
   It logs to `/var/log/mcp-server-misterfpga/track.log`.

A device per MiSTer appears in Home Assistant under MQTT, named from its name and model: *Office
MiSTer Pi*, *Family Room SuperStation One*.

### Entities

For a MiSTer named Office with model MiSTer Pi (check the device page for the exact IDs):

| Entity | State | Attributes |
| --- | --- | --- |
| `sensor.office_mister_pi_now_playing` | game name, or `Idle` | system, rom, started, source, host, device |
| `binary_sensor.office_mister_pi_playing` | on while a game runs | |
| `sensor.office_mister_pi_now_playing_system` | system id (`SNES`) | |
| `sensor.office_mister_pi_last_played` | last game logged | system, rom, started, ended, duration_s, host, device |
| `sensor.office_mister_pi_last_session_length` | minutes | |
| `event.office_mister_pi_game_session` | `session_start`, `session_end` | game, system, rom, source, started (+ ended, duration_s at the end), host, device |
| `sensor.office_mister_pi_activity` | `menu`, `playing`, `off` | |
| availability | `misterfpga/<mister>/status` and `misterfpga/_tracker/status` | |

**For automations** (lights when a game starts), use *Activity*. It follows only the tracker, so
when a MiSTer is switched off it reads `off` rather than *unavailable*. `menu` means no game is
running; a core opened without a game (say, ao486) also reads `menu`. The other entities go
*unavailable* when either the MiSTer or the tracker is gone, so a stale "playing" never shows.

Topics are `misterfpga/<mister>/now_playing` (retained), `last_session` (retained), `session`
(events), `activity` (retained) and `status`, where `<mister>` is the name in lower case
(`family_room`). Unlike Batocera's script, the tracker keeps its broker connection open, so its MQTT
last will marks everything unavailable if it dies. There's no equivalent of Batocera's *Cabinet*
event: Zaparoo has no power events, and a MiSTer rebooting looks the same as one switched off.

If the broker is unreachable (Home Assistant restarting, say), the latest state of each entity is
sent when it's back, along with logged sessions; a `session_start` that would arrive late is
dropped, so its automation doesn't fire at the wrong time (Batocera's rule).

Daily play time on one MiSTer:

```yaml
sensor:
  - platform: history_stats
    name: Office MiSTer play time today
    entity_id: binary_sensor.office_mister_pi_playing
    state: "on"
    type: time
    start: "{{ today_at() }}"
    end: "{{ now() }}"
```

## Remote access: stdio, or HTTP behind Cloudflare Access

There are two ways to run it:

- **stdio**: Claude Code or Claude Desktop launches it on a machine on your LAN.
- **`serve --http`**: Streamable HTTP on `127.0.0.1:8716/misterfpga/mcp`, as a long-lived service (see
  [Service and Cloudflare](#service-and-cloudflare)). Published through a Cloudflare Tunnel with Cloudflare
  Access in front, it's one URL you add as a connector in Claude Code, Claude Desktop and the mobile app,
  the same way as its siblings (Onkyo, Shield TV, Harmony, Sofabaton, webOS, Batocera).

The HTTP transport is `remote.py`, shared byte for byte with those siblings through
[mcp-server-shared](https://github.com/SDNick484/mcp-server-shared). What sits behind it is more powerful
than a TV remote: this server can start an update that **reboots a MiSTer**, and its SSH key is **root** on
them. Zaparoo, on MiSTer, has no authentication either. So the server is the gate, and it doesn't rely on
Cloudflare's edge alone:

- **With Access configured, every request needs a valid Access assertion**, checked here: signature
  (Access's published keys), the application's audience tag, issuer, expiry, and `MCP_ALLOWED_EMAILS` if
  set. Anything else gets a plain 403, so a request that reaches the port from the LAN, around the tunnel,
  is refused too. **Set `MCP_ALLOWED_EMAILS`** to yourself: it keeps that control yours even if the Access
  policy is later widened for another app.
- **Without Access, it serves only the machine it runs on.** It won't listen on anything but loopback,
  won't start with a `--public-host`, and answers 403 to anything a tunnel or proxy forwarded.
  `--insecure-no-auth` turns that off for a short trusted-LAN test; **never run the service with it**: it
  would give anything that can reach the port reboot-capable, root-over-SSH control of your MiSTers. Its
  Host and Origin checks block DNS-rebinding from a browser, not a direct request.
- **Never expose Zaparoo (7497) or SSH (22) themselves.** Only this server's port goes through the tunnel.
- What a caller can do is the same over HTTP as over stdio: the [limits below](#what-the-model-cant-do)
  are in the tools, not the transport. `run_update` still needs `host` and `confirm`, and is rate-limited
  to once an hour per MiSTer.

### Service and Cloudflare

`deploy/alpine/` installs it as an OpenRC service on an always-on Alpine machine on the MiSTers' LAN (a
Proxmox LXC, for example), in the siblings' `deploy/alpine/` layout so they can share a container: owned
by its own user `mcp-mister`, with the SSH key and `config.json` in `/var/lib/mcp-server-misterfpga`
(0700), and the HTTP and Access settings in `/etc/mcp-server-misterfpga/env`:

```sh
sh deploy/alpine/install.sh        # prints the next steps: key, ssh-copy-id, check, doctor, start
```

The service runs `/opt/mcp-server-misterfpga/run.sh` (`serve --http`) under `supervise-daemon` (restarted
if it exits) as `mcp-mister`, logging to `/var/log/mcp-server-misterfpga/server.log`. Its settings are the
`MCP_HTTP_*`, `MCP_PUBLIC_HOSTS` and `CF_ACCESS_*` lines in the env file:

| Flag | Variable | Default |
| --- | --- | --- |
| `--bind` | `MCP_HTTP_BIND` | `127.0.0.1` |
| `--port` | `MCP_HTTP_PORT` | `8716` (the siblings use 8711 to 8715, Batocera 8717, Pixelcade 8718) |
| `--path` | `MCP_HTTP_PATH` | `/misterfpga/mcp` |
| `--public-host` | `MCP_PUBLIC_HOSTS` | none: only loopback Host headers pass |
| | `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD` | none: serves only this machine |
| | `MCP_ALLOWED_EMAILS` | anyone Access lets in (set it) |

To use it as a connector, with cloudflared running on the same machine (keep the `127.0.0.1` bind):

1. **Tunnel route:** a public hostname, say `mcp.example.com`, path `misterfpga/*`, to
   `http://localhost:8716`. Several servers can share the hostname, one path each.
2. **Access application** (self-hosted) covering that hostname and path, with a policy for who may use it
   and Managed OAuth turned on, so Claude's connector can sign in through Access. Note its AUD tag.
3. **The env file:** `MCP_PUBLIC_HOSTS=mcp.example.com`, `CF_ACCESS_TEAM_DOMAIN=<team>.cloudflareaccess.com`,
   `CF_ACCESS_AUD=<the AUD tag>`, `MCP_ALLOWED_EMAILS=<you>`, then `rc-service mcp-server-misterfpga restart`.
4. **The connector:** `https://mcp.example.com/misterfpga/mcp`.

`GET /healthz` answers without auth, for monitoring. Details are in `src/misterfpga_mcp/remote.py`. CI runs
`deploy/alpine/smoke-test.sh` in a stock `python:3.12-alpine` container: install, the test suite on musl,
the OpenRC service as its user, an MCP call over HTTP against the simulator, and the Access settings from
the env file turning the Access check on.

## Logs

stderr only (stdout is the MCP stdio transport). Addresses and MACs are redacted unless
`--no-redact` (`MISTERFPGA_LOG_UNREDACTED=1`); keys and tokens always are. `-v` adds protocol
traffic, including SSH.

## Testing without a MiSTer

```sh
pip install -e ".[dev]" && pytest && ruff check . && ruff format --check . && mypy
mcp-server-misterfpga simulate                # two simulated MiSTers; prints what to run next, e.g.:
MISTERFPGA_CONFIG_DIR=~/.config/mcp-server-misterfpga-sim/config mcp-server-misterfpga call launch game="Office/101"
```

The simulator (`src/misterfpga_mcp/sim/`) runs, for each MiSTer, a fake Zaparoo Core speaking
JSON-RPC over a real WebSocket and a fake MiSTer SSH server (real SSH and SFTP, chrooted to a
folder standing in for the SD card), over one shared device state, so launching through Zaparoo
changes what `/tmp/CORENAME` says over SSH. The server's real clients connect to both. Two
MiSTers on 127.0.0.1 and 127.0.0.2: Office (a MiSTer Pi) and Family Room (a SuperStation One),
with *Super Metroid* on both on purpose.

`tests/test_track.py` runs the play tracker against the same simulator and a small fake MQTT
broker: menu launches, outages, catching up after a restart, a broker restart.

`doctor --dump DIR` records a real MiSTer's read-only Zaparoo answers (redacted); put them in
`tests/fixtures/recorded/` and `test_recorded.py` replays them through the tools.

## Shared code

`src/misterfpga_mcp/remote.py`, `logsafe.py`, `tasks.py`, `ratelimit.py`, their tests and
`scripts/sync_shared.py` come from mcp-server-shared, pinned by `shared.lock`. Don't edit them
here: change them there, then `python scripts/sync_shared.py --update`. CI's `shared-drift` job
fails on a copy that differs.

## What has been confirmed on hardware

Every protocol detail this server relies on is in `src/misterfpga_mcp/assumptions.py`, with its
source and how confident that source is. `doctor` prints the unconfirmed ones;
HARDWARE_VALIDATION.md says which step confirms each.

| Assumption | Confidence | Status | Claim |
| --- | --- | --- | --- |
| `Z-ENDPOINT` | high | simulator-only | Zaparoo's API: JSON-RPC 2.0 over `ws://<host>:7497/api/v0.1`; `ping` → `pong` |
| `Z-LEGACY` | high | simulator-only | MiSTer admits unauthenticated LAN clients (no encryption by default) |
| `Z-LEGACY-CAPS` | high | simulator-only | such a client may launch, search, screenshot, use NFC; not device or update methods |
| `Z-RUN-WAITS` | high | simulator-only | `run` returns after the launch finishes, with categorized errors |
| `Z-STOP-MENU` | medium | simulator-only | `stop` returns an FPGA core to the menu |
| `Z-MEDIAID` | medium | simulator-only | `media.meta` by `mediaId` gives the launch script; ids last until a reindex |
| `Z-TRACKS-MENU` | medium | simulator-only | menu launches are tracked too (with `recents=1`, `log_file_entry=1`) |
| `Z-NOTIFY` | high | simulator-only | `media.started` / `media.stopped` payloads |
| `Z-HISTORY` | medium | simulator-only | play history includes menu launches |
| `Z-SESSION-TIMES` | medium | simulator-only | a session's live start and its history entry agree (the tracker's catch-up) |
| `Z-SCREENSHOT` | high | simulator-only | `screenshot` returns a base64 PNG and where it was saved |
| `Z-NFC` | medium | simulator-only | `readers.write` waits for a card; UIDs are normalized; scans show in `tokens` |
| `Z-SCRIPT` | low | simulator-only | `**mister.script:update_all.sh` runs Update All on screen |
| `M-INI-SECTIONS` | high | simulator-only | how MiSTer matches `[sections]` |
| `M-INI-KEYS` | high | simulator-only | the key table, from Main_MiSTer `cfg.cpp` |
| `M-INI-APPLY` | medium | simulator-only | INI changes apply at the next core load |
| `M-INI-RENAME` | low | simulator-only | write-then-rename works on the SD card over SFTP |
| `M-CMD` | high | simulator-only | `load_core` on `/dev/MiSTer_cmd` |
| `M-CORENAME` | high | simulator-only | `/tmp/CORENAME`, `MENU` in the menu |
| `M-PATHS` | medium | simulator-only | saves and save states locations |
| `M-SSH` | medium | simulator-only | root SSH with a key, and SFTP |
| `S-ACTIVE` | medium | simulator-only | attract mode ⇔ SAM's `loop_core` process |
| `S-HANDOFF` | medium | simulator-only | picking up a controller in attract mode reloads the game |
| `U-REBOOT` | high | simulator-only | Update All may reboot when it finishes |
| `H-SS1` | low | simulator-only | the SuperStation One behaves like any MiSTer here |

## License

MIT. Zaparoo Core, Main_MiSTer and mrext are GPL-3.0; this server uses their documented
protocols and cites facts read from their source (such as the INI key table), and copies none of
their code.

# mcp-server-misterfpga

An [MCP](https://modelcontextprotocol.io) server for [MiSTer FPGA](https://github.com/MiSTer-devel)
devices: find and launch games, see what's being played and what's been played most, take
screenshots, make NFC cards, change `MiSTer.ini` settings (globally or per core), back up saves,
and start Update All, from Claude Code or Claude Desktop. It talks to each MiSTer through
[Zaparoo Core](https://zaparoo.org)'s API and, for what Zaparoo doesn't cover, SSH. One server
drives several MiSTers.

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
command. This server is meant to stay on your LAN (see [Local only](#local-only-stdio-or-http-on-loopback)).

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

## Local only: stdio, or HTTP on loopback

**This server is meant to stay on your LAN.** Unlike its AV siblings (Onkyo, Shield TV, Harmony,
Sofabaton, webOS), it isn't published through a Cloudflare Tunnel, so it isn't reachable from
claude.ai or the Claude mobile app. That's on purpose: it can start an update that reboots a
MiSTer, and its SSH key is root on them.

There are two ways to run it:

- **stdio**, the usual way: Claude Code or Claude Desktop launches it on a machine on your LAN.
- **`serve --http`**: Streamable HTTP on `127.0.0.1:8716/misterfpga/mcp`, through the `remote.py`
  it shares with its siblings (Onkyo 8711 … webOS 8715). `deploy/alpine/` runs this as a service.
  The loopback bind keeps it inside that machine or container; nothing else on the LAN can reach it.

Because `serve --http` runs on `remote.py`, this repo keeps mcp-server-shared's `remote` module.
Batocera and Pixelcade exclude it, but they only ever speak stdio. Here, "LAN-only" means no
tunnel route, not no HTTP.

On any address other than loopback, `remote.py` refuses to start without Cloudflare Access, unless
you pass `--insecure-no-auth`. **Don't use that flag for this server.** It exists for short tests
on a trusted LAN, and here it would give anything that can reach the port reboot-capable,
root-over-SSH control of your MiSTers. Its Host and Origin checks block DNS-rebinding from a
browser, not a direct request.

`deploy/alpine/` installs it as an OpenRC service (in an Alpine LXC, for example), owned by its
own user, with the SSH key and `config.json` in `/var/lib/mcp-server-misterfpga` (0700):

```sh
sh deploy/alpine/install.sh        # prints the next steps: key, ssh-copy-id, check, doctor, start
```

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

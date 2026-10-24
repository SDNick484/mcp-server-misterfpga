# mcp-server-misterfpga: Phase 0 design

Status: **approved 2026-10-13**, with the answers to the open questions recorded in §12 and
folded into the sections they affect. It stays in the repo as written; a "What changed during the
build" section gets added at the end (the same convention as mcp-server-webos's `docs/PLAN.md`).

Read for this design (2026-10-13):

- **Siblings:** mcp-server-shared `b78e89f`, mcp-server-harmony and -sofabaton `main`, mcp-server-webos
  `main` plus its `shared-sync` branch, mcp-server-batocera `main`.
- **Upstream, from source:**
  - Zaparoo Core `c31d322` (latest release v2.20.0): `docs/api/*.md`, `pkg/api`,
    `pkg/platforms/mister`.
  - zaparoo.org `e24cea8`: the ZapScript docs.
  - wizzomafizzo/mrext `1c4ae0c`: `docs/remote*.md`.
  - MiSTer-devel/Main_MiSTer `2dd3662`: `cfg.cpp`, `input.cpp`.
  - mrchrisster/MiSTer_SAM `97ab5cc`.
  - theypsilon/Update_All_MiSTer `10f9b9a`.
  - hudsonbrendon/python-mister-fpga `618ab6d`.

Nothing here has been checked against your hardware. Every protocol claim the design leans on is
listed as an assumption in §9.

---

## 1. What the research changed

The prompt pictured three backends: Zaparoo first, mrext Remote second, SSH as a fallback. The
sources point to a simpler and safer split.

1. **Zaparoo Core already covers nearly everything.** Its API (JSON-RPC 2.0 over WebSocket,
   `ws://<host>:7497/api/v0.1`) is declared stable, with additive changes only. It covers:
   - search, launch (`run`), stop, and active media;
   - play history (`media.history`, `media.history.top`), which Zaparoo records itself;
   - screenshots (returned as base64);
   - NFC: readers, `readers.write`, and UID→ZapScript mappings;
   - systems, and launchers with their MiSTer core;
   - `media.started` / `media.stopped` push notifications.

   It also tracks games launched from MiSTer's own menu, not just its own launches.
   `pkg/platforms/mister/tracker` watches `/tmp/ACTIVEGAME` and `/tmp/CORENAME`.
2. **mrext Remote isn't needed in v1.** Its own docs now say to use the Zaparoo Core API for new
   search, launch, and tracking integrations, and to keep Remote for MiSTer-specific things such
   as INI editing. Its INI API can't help us either: it only edits the `[MiSTer]` section and
   doesn't support per-core sections like `[SNES]` ("Sections... are not currently supported").
   That's exactly the per-core case you asked for. So INI work goes over SSH. Remote would add a
   third fake, a third set of assumptions, and a third failure mode, all for features we'd get
   elsewhere. **Decided (Q1): left out of v1**, kept as a possible later backend.
3. **python-mister-fpga: don't depend on it.**
   - It wraps the mrext Remote API, which we're not using.
   - It had five days of activity (2026-05-31 to 06-04) and nothing since.
   - It pulls in `asyncssh`, which we'd use anyway.

   A thin Zaparoo client of our own is about 200 lines and fully under our tests.
4. **Unauthenticated access is enough on MiSTer, but only for some methods.**
   - Zaparoo's `cmd/mister` doesn't turn on encryption by default (Linux, SteamOS and Windows
     do). MiSTer is one of the "legacy" platforms where unauthenticated plaintext clients are
     still admitted.
   - Legacy clients get `run`, `stop`, `media.*`, mappings, readers, and grandfathered
     `screenshot` and `input`.
   - They don't get `device.status`, `update.check`, `update.apply`, backups, or power.
   - A static API key would grant admin. But `HTTPAuthMiddleware` returns 401 to *every*
     key-less remote request once any key is configured, which could lock out the Zaparoo phone
     app. **Decided (Q2): no key.** v1 uses legacy access only, and gets disk and core
     information over SSH.
   - **Zaparoo isn't installed on a MiSTer by default (Q9).** It's one item in Update All's
     Tools & Scripts. The README gets a "Set up your MiSTer" section with the steps, and
     `doctor` and the `get_status` limitation point at it (§4).
5. **Updates can run on the TV instead of headless.** ZapScript `**mister.script:update_all.sh`
   runs update_all on-screen, exactly as if you'd picked it from the Scripts menu.
   - You see the progress and can intervene.
   - Nothing runs blind over SSH.
   - Update_All defaults to `autoreboot = true`, which is why `run_update` is destructive (§6).
6. **The attract mode to filter is MiSTer Super Attract Mode (SAM).** Its `gametimer` defaults to
   180 s, so a 60-second minimum session (Batocera's filter) won't catch it.
   - SAM runs its game loop as `MiSTer_SAM_on.sh loop_core`. Its own `there_can_be_only_one`
     finds itself that way.
   - So "a game started while that process exists" marks an attract launch.
   - With `playcurrentgame=yes`, SAM kills itself and *reloads* the game when you pick up a
     controller. That produces a fresh `media.started` after SAM has gone, which gets logged as
     real play. It's the same shape as Batocera's START-on-screensaver case.
7. **`stop` returns to the menu.** The API's `stop` calls `StopActiveLauncher(StopForMenu)`. For
   an FPGA core (no tracked process), that falls through to `ReturnToMenu()` → `LaunchMenu()`. So
   `return_to_menu` needs nothing else.
8. **Licenses.** Zaparoo Core, Main_MiSTer and mrext are GPL-3.0, and this repo is MIT. We use
   their documented protocols and cite facts read from their source, such as the INI key table.
   No code is copied. This is the same rule webos applied to lgpowercontrol.
9. **The shared layout to copy is webos's `shared-sync` branch.** Harmony's and Sofabaton's
   `main` don't have `shared.lock` or `tasks.py` yet; those are on their `shared-sync` branches.
   webos `shared-sync` has the full layout: `shared.lock` at `b78e89f`, the weekly
   `shared-drift` job, and `deploy/alpine/`.

## 2. Conventions copied from the siblings

| Convention | From | Here |
|---|---|---|
| src layout, setuptools, `mcp>=2.3,<3`, ruff (E,W,F,I,UP,B,SIM; line 120), strict mypy, pytest + anyio | all | same; package `misterfpga_mcp`, script `mcp-server-misterfpga`, env prefix `MISTERFPGA_` |
| CI: 3.11–3.14 matrix, `python:3.12-alpine` smoke job running `deploy/alpine/smoke-test.sh`, weekly schedule (America/Los_Angeles) + `workflow_dispatch` | webos `shared-sync` | same |
| `remote.py`, `logsafe.py`, `tasks.py` + their tests, via `scripts/sync_shared.py` and `shared.lock`; `shared-drift` job; `anyio_backend` fixture returning `"asyncio"` | mcp-server-shared README | same; never hand-edited |
| `MCPServer` with lifespan; docstrings are the model's docs; `Annotated[..., Field(...)]` args; `TypedDict` results become outputSchema; explicit `ToolAnnotations` and `title` on every tool, enforced by `test_tools.py` | Harmony, webOS | same; `_READ`, `_ACT`, `_ACT_IDEMPOTENT`, plus `_DESTRUCTIVE` (Batocera's rule: destructive = can lose unsaved progress or data) |
| Never guess between devices: optional `host` everywhere, ambiguity error naming the candidates, `ref` strings the model can copy | Harmony `hubs.py` | `HostRegistry` (§5) |
| `get_status` reports `capabilities` and `limitations`, and each limitation says what would fix it | Sofabaton `_capabilities()` | per host, per backend (§4) |
| Assumption registry (`id`, `claim`, `source`, `confidence`, `status`), cited as `# ASSUMPTION <id>`, in README and HARDWARE_VALIDATION.md, enforced by `test_assumptions.py` | Harmony, Sofabaton | §9 |
| Wire-level fakes the real clients connect to, used by tests *and* `simulate` | Harmony `sim/fake_hub.py` | fake Zaparoo (aiohttp) + fake MiSTer SSH (asyncssh) |
| CLI `serve` / `check` / `call` / `doctor` (`--dump`) / `simulate`; `--dry-run`; `--no-redact` | Harmony | same |
| Token-bucket rate limits per device; per-call caps | Harmony/Sofabaton `limits.py` | per host (§8) |
| stderr-only logging through `logsafe` | all | same |
| OpenRC service on Alpine: `supervise-daemon`, `run.sh`, `/etc/<name>/env` (0640), state in `/var/lib/<name>` (0700), dedicated user | webos `deploy/alpine/` | same; port **8716** (Onkyo 8711 … webOS 8715) |
| The model never supplies a path or a raw command | Batocera | games launch by `ref` only; ZapScript is built by the server (§8) |
| Play-session MQTT topics and HA discovery | Batocera `playtracker` | same shape (§10) |

## 3. Module layout

```
src/misterfpga_mcp/
  config.py       settings: hosts, SSH, backup dir, dry-run; problems collected, never fatal
  zaparoo.py      ZaparooClient: one long-lived WebSocket per host; JSON-RPC ids; reconnect with
                  backoff; ping/pong; pushed-state cache (active media, last token)
  ssh.py          MisterShell: asyncssh connection, pinned host key; a closed set of commands
                  (§8); SFTP read/write helpers
  ini.py          MiSTer.ini parser and writer: keeps comments, order, sections; key table (§6)
  backup.py       save/savestate snapshot pull (SFTP → local, hard-linked snapshots)
  hosts.py        HostRegistry: names, refs, the never-guess rule, startup grace, bounded stop
  capabilities.py per-host capabilities and limitations
  server.py       MCP tools, resources, prompt
  limits.py       rate limits (Harmony's TokenBucket; see the promotion proposal, §7)
  assumptions.py  the registry
  doctor.py, cli.py
  remote.py, logsafe.py, tasks.py      (shared, synced)
  sim/fake_zaparoo.py, sim/fake_mister.py, sim/fixtures/{pi,superstation}.json
deploy/alpine/    initd, run.sh, install.sh, env.example, smoke-test.sh
```

## 4. Backends and which one each tool uses

| Tool | Zaparoo (legacy access) | SSH | Without that backend |
|---|---|---|---|
| `get_status` | active media, indexing state, version | core name (`/tmp/CORENAME`), SAM state, free space | reports what it can; `limitations` says what's missing and why |
| `list_systems`, `search_games`, `play_history` | required | — | limitation: "install Zaparoo Core (update_all → Zaparoo)" |
| `launch`, `return_to_menu` | required | `return_to_menu` fallback: `load_core /media/fat/menu.rbf` | — |
| `screenshot` | required (grandfathered legacy) | — | — |
| `list_nfc`, `write_nfc_card`, `map_nfc_card` | required | — | — |
| `get_ini`, `set_ini` | — | required | limitation: "SSH isn't configured: `check --ssh`" |
| `backup_saves` | — | required (SFTP) | — |
| `run_update` | required (`**mister.script:update_all.sh`) | — | — |

Two backends, and neither substitutes for the other, apart from the menu fallback. That keeps the
capability model honest. A capability is listed only when its backend is connected *and* its
access check passes. `doctor` probes, for example, whether screenshot access is still granted to
legacy clients.

**Capability names:** `search`, `launch`, `history`, `screenshot`, `nfc_read`, `nfc_write`,
`nfc_map`, `ini_read`, `ini_write`, `backup_saves`, `update`, `attract_detection`, `core_name`.

### Setting up a MiSTer (README section, Q9)

Zaparoo Core isn't on a MiSTer by default. The README walks through it, from Zaparoo's
[MiSTer page](https://zaparoo.org/docs/platforms/mister/):

1. Run **Update All**, open its settings (press UP during the countdown), and turn on
   **Zaparoo** under **Tools & Scripts**. Save and let it run.
2. On the MiSTer, open **Scripts → zaparoo** and accept the prompt to start it as a service at
   boot.
3. Check it from another machine: `http://<mister-ip>:7497/app/` opens Zaparoo's web UI.
4. For accurate tracking of games launched from the MiSTer's own menu, set `recents=1` and
   `log_file_entry=1` in `[MiSTer]`. That's Zaparoo's own advice. `set_ini` can do this once SSH
   works; `doctor` checks both and says when they're off.
5. SSH: `ssh-copy-id root@<mister-ip>` (MiSTer's stock root password is `1`), then
   `mcp-server-misterfpga check --host <ip> --ssh` to confirm and pin the host key.

When Zaparoo isn't reachable, `get_status` lists the limitation "Zaparoo Core isn't running on
<host> (port 7497). Install it from Update All → Tools & Scripts; see the README's setup
section." `doctor` tells apart "connection refused" (not installed, or the service isn't
enabled) from "no route" (wrong address, or the MiSTer is off).

## 5. Hosts, names and references

Config lives at `~/.config/mcp-server-misterfpga/config.json`. `MISTERFPGA_HOSTS` is a
comma-separated override, as in Harmony.

```json
{
  "hosts": [
    {"host": "192.168.1.70", "name": "Office", "model": "MiSTer Pi",
     "ssh": {"user": "root", "key": "~/.ssh/mister_ed25519", "host_key": "ssh-ed25519 AAAA..."}},
    {"host": "192.168.1.71", "name": "Family Room", "model": "SuperStation One",
     "manufacturer": "Retro Remake"}
  ],
  "backup_dir": "/mnt/backups/mister-saves"
}
```

- **Names.** The config name if set, else the SSH `hostname`, else the address. `check` saves the
  name and never overwrites one you wrote. Harmony's rule. Naming hosts by where they are
  ("Office", "Family Room") works best, because `model` already says what they are.
- **Model (Q6).** `model` (free text, default `MiSTer FPGA`) and optional `manufacturer` say
  what the device *is*: a MiSTer Pi, a SuperStation One, a DE10-Nano. Nothing this server can
  read with unauthenticated access says which board it's talking to, so it's configured, and
  `check` asks for it. It shows up in `get_status`, in the play tracker's payloads and in the
  Home Assistant device (§10).
- **`backup_dir` (Q7)** has no default. Until `backup_dir` or `MISTERFPGA_BACKUP_DIR` is set,
  `backup_saves` is a limitation that says so. Any directory the server can write to works: a
  local disk, NFS, SMB, or an OMV share mounted into the LXC.
- **Refs.** A game ref is `<host>/<mediaId>`, e.g. `Family Room/4812`. `mediaId` is Zaparoo's
  opaque row id, and `media.meta {mediaId}` turns it back into the exact `zapScript`, so the
  server holds no ref cache. A reindex can renumber rows. A stale ref then gets "not found on
  Family Room; search again", never a guess.
- **Never guess.** Same rule as Harmony:
  - An action without `host` acts only when exactly one host fits: the ref's host, or the one
    host that's running something.
  - With two MiSTers on two TVs, launching on the wrong one is the failure to prevent.
  - Read tools without `host` cover every host, the Onkyo/webOS convention.
- **No separate `list_hosts`.** Harmony's reasoning applies: `get_status` already lists every
  host, and two tools that both list hosts split the model's choice. This changes the prompt's
  starting tool set; the prompt's `mister_status` becomes `get_status`, the sibling name.

## 6. Tools

Fourteen tools. The prompt's list is kept, except: `list_hosts` is folded into `get_status`; and
`list_cores` becomes `list_systems`, because Zaparoo models cores as each system's launcher.
Each system row carries its core. `create_nfc_mappings` splits into `write_nfc_card` and
`map_nfc_card`, the two different ways a card gets a game (below). `play_history` is new.

Every tool has an optional `host: Host` argument (`str | None`, the name from `get_status`) unless
noted. Every result includes `dry_run: bool` where anything could change.

### Read

**`get_status(host?)`** `_READ`

The model is told to call it first. Its result:

```python
class HostStatus(TypedDict):
    name: str
    reachable: bool
    zaparoo: BackendState        # {connected, version, error}; version from `version`
    ssh: BackendState | None     # None when not configured
    now_playing: NowPlaying | None  # {ref, game, system, system_name, started, source}
    core: str | None             # /tmp/CORENAME ("MENU" in the menu); needs SSH
    attract_mode: bool | None    # SAM's loop running; needs SSH
    library: Library | None      # {indexed, indexing, total_media}
    free_space_mb: int | None    # /media/fat; needs SSH
    capabilities: list[str]
    limitations: list[str]       # each says what would fix it

class Status(TypedDict):
    dry_run: bool
    hosts: list[HostStatus]
```

**`list_systems(host?)`** `_READ`

Systems that have a launchable core, with `media_count` and `core` (e.g. `_Console/SNES`). It
comes from Zaparoo `systems` plus `launchers`, keeping launchers with `backend == "mister_core"`.

**`search_games(query, system?, host?, limit=20)`** `_READ`

- **Args:** `query` is 1–100 chars. `system` is a Zaparoo system id, matched with
  `fuzzySystem=true`. `limit` is 1–50.
- **Result:** each row has `ref`, `host`, `name`, `system`, `system_name`, `variant` (the
  `disambiguatingTags` as text, e.g. "region:us"), and `relative_path`. Also `truncated`.
- With your ROMs shared from OMV, both MiSTers will list the same game. They come back as two
  rows with two refs, which is correct: the ref says which TV.

**`play_history(mode="recent"|"top", system?, host?, limit=10)`** `_READ`

Zaparoo `media.history` (with `distinctMedia`) or `media.history.top`. Rows have `ref` (null if
the file's no longer indexed), `game`, `system`, `last_played`, and `play_time_s` or
`total_play_time_s` plus `sessions`. Answers "what have I been playing", "my most-played SNES
games".

**`get_ini(section?, ini=1, host?)`** `_READ`

- `ini` is 1–4, MiSTer's main and alternate INI slots.
- Returns `{file, sections: {name: {key: value}}}`, parsed from the file read over SFTP.
- `section` narrows it to one, matched case-insensitively the way MiSTer does.

**`list_nfc(host?)`** `_READ`

- `readers` with their capabilities.
- `mappings`, each with `label`, `uid`, `game` (resolved), and `ours`, meaning this server
  created it (label prefix `mcp:`).
- `last_scanned`: `{uid, at}` from `tokens.history`.

### Act

**`launch(game?, system?, host?)`** `_ACT_IDEMPOTENT`

- Exactly one of `game` (a ref) or `system` (opens the core, e.g. ao486, with no game).
- The ZapScript comes from `media.meta` (for `game`) or is `**launch.system:<id>`, built by the
  server from a validated id. `run` waits for execution. Then the server polls `media.active` for
  up to 10 s, because it doesn't trust a send (the webOS/Shield lesson).
- **Refuses if anything is running on that host:** "Family Room is playing Super Metroid; call
  return_to_menu first". Same as Batocera's `launch_game`. The model then makes the destructive
  step a separate, visible call. That's also why `launch` itself isn't destructive.
- **Result:** `{host, launched, system, confirmed, warnings}`.
- `run` errors map to readable reasons. `launch_repair` with `launcher_plugin_missing`, for
  example, becomes "the SNES core isn't installed on Family Room".

**`return_to_menu(host?)`** `_DESTRUCTIVE` (idempotent)

- Zaparoo `stop`. Without `host`: acts on the one host that's running something; several →
  error. Nothing running → `unchanged`.
- **Destructive** because unsaved progress in the running game is lost (Batocera's `quit_game`).

**`screenshot(host?)`** `_ACT` (not read-only: MiSTer writes the file to `/media/fat/screenshots`)

- Returns MCP image content (PNG), plus `{host, saved_path}`.
- Needs a single host: without `host`, it uses the one host that's running something; otherwise
  the call is an error.

**`set_ini(key, value, section="MiSTer", ini=1, host?, dry_run=False)`** `_ACT_IDEMPOTENT`

- **`key`:** must be in MiSTer's own key table (`ini_vars[]` in Main_MiSTer `cfg.cpp`, 112 keys
  at `2dd3662`), minus a deny-list:
  - `MAIN`: boots an alternate Main binary.
  - `SHARED_FOLDER`, `DEBUG`: a filesystem path, and debug output.
  - `BOOTCORE` (**decided, Q5**): it can make every boot skip the menu, which is surprising and
    annoying to undo.

  `LOG_FILE_ENTRY` was on this list in the first draft. It's allowed: Zaparoo's setup guide
  asks for `log_file_entry=1` (with `recents=1`) for accurate tracking of menu launches (§4).
- **`value`:** checked against the table's type and range. `UINT8 0..1` must be 0 or 1; strings
  have a length cap and no control characters. `null` removes the key.
- **`section`:** one of these forms, as `ini_get_section()` matches them:
  - `MiSTer`, `arcade`, `arcade_vertical`;
  - `video=<mode>`;
  - a core name, with an optional trailing `*`.

  A core name not installed on that host is allowed, with a warning in the result.
- **Write path:** read → edit in place (comments and order kept; new keys go at the end of their
  section; a missing section is created) → copy the original to
  `/media/fat/MiSTer.ini.<UTC timestamp>.bak` → write `MiSTer.ini.tmp` → rename over the
  original. The newest 10 of this server's `.bak` files are kept; older ones it made are
  removed, and nothing else in `/media/fat` is ever deleted.
- **Result:** `{host, file, section, key, old, new, backup, diff, takes_effect}`. `takes_effect`
  is "next core load", since MiSTer reads the INI when a core starts. Not automatic: the server
  doesn't reload the core for you.
- `dry_run` returns the diff without writing.

**`backup_saves(host?, dry_run=False)`** `_ACT_IDEMPOTENT`

- Pulls `/media/fat/saves` and `/media/fat/savestates` over SFTP into
  `<backup_dir>/<host>/<YYYY-MM-DD_HHMMSS>/`. `backup_dir` is whatever you configure (§5); with
  none, the tool is unavailable and says how to set it.
- Unchanged files (same size and mtime) are hard-linked to the previous snapshot, as
  `rsync --link-dest` does. Each snapshot is complete, and they cost only the changes.
- Keeps the last 30 snapshots (configurable).
- **Never writes to the MiSTer.** It's not read-only because it writes local files. Not
  destructive.
- Caps: 4 GiB and 15 min per call. Progress is reported through MCP progress notifications.
- **Result:** `{host, snapshot, copied, linked, bytes, skipped}`.

**`run_update(host, confirm: Literal[True])`** `_DESTRUCTIVE`

- `host` is required; there's no "just the one" rule for this.
- Refuses while a game is running.
- Runs the constant ZapScript `**mister.script:update_all.sh` and returns once it has started.
  The TV shows progress. The result says so, and that the MiSTer may reboot.
- Rate-limited to once an hour per host.
- **Destructive:** it replaces cores and files, and `autoreboot` defaults to true.

**`write_nfc_card(game, reader?)`** `_DESTRUCTIVE`

- `readers.write` with the game's ZapScript. Put a card on the reader attached to that MiSTer
  first.
- **Destructive** because it overwrites whatever was on the card.
- **Result:** `{host, game, reader}`.

**`map_nfc_card(game, uid?)`** `_ACT_IDEMPOTENT`

- Creates a Zaparoo mapping: `type=uid`, `match=exact`, `pattern=<uid>`,
  `override=<zapScript>`, `label="mcp: <game>"`.
- `uid` defaults to the card on that host's reader now (`tokens`), else the last one scanned
  (`tokens.history`) within 2 minutes. Older or missing → error: "scan the card, then try again".
- Refuses if that UID already has a mapping, and says which game it maps to.
- Works with read-only cards and amiibo-style toys.
- This is how "make me cards for my 20 favorite SNES games" works: the model loops, and you scan
  each card when asked (the `make_nfc_cards` prompt guides that).

**Left out of v1:**

- **Deleting mappings.** Use the Zaparoo app; this is an easy add if you want it.
- **`input.keyboard`.** Your controllers drive the MiSTer; a model typing into cores needs a
  reason.
- **`set_volume`** through `/dev/MiSTer_cmd`. The receiver owns volume (the webOS "volume
  owner" rule).

### Resources and prompt

- `misterfpga://ini-keys`: the allowed keys with type, range, and default, plus the denied ones
  and why. This matches webOS's `webos://keys` pattern.
- Prompt `make_nfc_cards`: walks through picking games, then `map_nfc_card` or `write_nfc_card`
  per card.

## 7. Shared code

- **Synced, never edited:** `remote.py`, `logsafe.py`, `tasks.py` and their tests,
  `scripts/sync_shared.py`, `shared.lock`. Same for the `shared-drift` job.
- **Proposed promotions to mcp-server-shared**, each as a separate PR:
  1. **`TokenBucket`, as a new shared `ratelimit.py`** (decided, Q8; first). Harmony and
     Sofabaton already carry identical `limits.py` copies ("shared with Sofabaton only"), and
     this would be the third. Only the class is shared: each sibling's `limits.py` keeps its own
     numbers (presses, activity changes) and imports `TokenBucket` from `ratelimit.py`.
  2. **The `Assumption` dataclass and the generic half of `test_assumptions.py`.** Every id must
     appear in README and HARDWARE_VALIDATION.md. Harmony, Sofabaton, webOS and this repo all
     carry their own copy.
  3. **A generic "which device" resolver** (`candidates`, `_one`, `qualify` from Harmony's
     `hubs.py`, minus aioharmony). Weaker: each sibling's device object differs. I'd wait until
     a third repo needs it.

  If you'd rather not gate this repo on those PRs, it starts with local copies and switches when
  each promotion merges. `sync_shared.py` then replaces the local file in place.

## 8. Safety model

- **No path, ZapScript, or shell command ever comes from the model.**
  - ZapScript is built only from a `media.meta` result for a ref, `**launch.system:<id>` from a
    validated system id, or the update constant.
  - The `unsafe` flag on `run` is never set. Zaparoo's own `**execute` is behind
    `allow_execute` anyway.
- **SSH runs a closed set of commands.** Each is a constant, and the arguments are validated and
  `shlex.quote`d:
  - `cat /tmp/CORENAME`
  - `ps -o args`, parsed locally, for SAM
  - `df -k /media/fat`
  - `hostname`
  - `echo load_core /media/fat/menu.rbf > /dev/MiSTer_cmd` (the menu fallback)

  File access is SFTP, limited to `/media/fat/MiSTer*.ini*`, `/media/fat/saves/**` and
  `/media/fat/savestates/**`. That's enforced by resolving paths and checking prefixes before
  every call. There's no `exec` tool, the same stance as `adb shell` on ShieldTV.
- **SSH trust.**
  - The host key is pinned by `check --ssh` after you confirm the fingerprint shown on screen
    (TOFU); a changed key is refused.
  - Key authentication only. MiSTer's stock password (`1`) is never stored;
    HARDWARE_VALIDATION.md covers `ssh-copy-id`.
- **Zaparoo methods are allow-listed.**
  - The client can only send: `version`, `media`, `media.search`, `media.meta`, `media.active`,
    `media.history`, `media.history.top`, `systems`, `launchers`, `run`, `stop`, `screenshot`,
    `readers`, `readers.write`, `mappings`, `mappings.new`, `tokens`, `tokens.history`.
  - Not sendable: anything under `settings.*`, `device.*`, `update.*`, `clients.*`, `profiles.*`,
    `media.generate`, `input.*`. A unit test pins the set.
- **Annotations.**
  - `_READ`: the read tools (§6).
  - `_DESTRUCTIVE`: `return_to_menu`, `run_update`, `write_nfc_card`.
  - `_ACT` / `_ACT_IDEMPOTENT`: the rest.
  - `open_world_hint=False` everywhere: LAN only.
- **Rate limits per host** (token buckets):

  | Action | Burst | Sustained |
  |---|---|---|
  | launches + returns to menu | 4 | 1 per 15 s |
  | screenshots | 6 | 1 per 10 s |
  | INI writes | 10 | 1 per 6 s |
  | NFC writes and mappings | 10 | 1 per 3 s |
  | backups | 1 in flight | — |
  | updates | — | 1 per hour |
- **Dry run.** `--dry-run` or `MISTERFPGA_DRY_RUN=1` makes every write report what it would do.
  `set_ini` and `backup_saves` also take a per-call `dry_run`.
- **Redaction.** `logsafe` redacts IPs and MACs; the SSH key path and host key are treated as
  secrets.

## 9. Hardware-free testing and assumptions

- **`FakeZaparoo`** (aiohttp): a real WebSocket JSON-RPC server with the allow-listed methods,
  legacy access rules, push notifications, a media DB from fixtures, and a history table.
- **`FakeMister`** (asyncssh server): a temp-dir SD card (`/media/fat/...`), `/tmp/CORENAME`, a
  SAM process table, a host key, and only the closed command set. Anything else gets an error, so
  a test fails if the server ever sends something new.
- **Fixtures:** two hosts, `Office` (a MiSTer Pi, 127.0.0.1) and `Family Room` (a SuperStation
  One, 127.0.0.2).
  - Both libraries contain *Super Metroid*, on purpose: it's the ambiguity test case.
  - Family Room's `MiSTer.ini` also has a `[SNES]` section.
- **`simulate`** runs both fakes. `doctor --dump` records redacted captures, which
  `test_recorded.py` replays.

Assumption ids, prefix by source: `Z-` Zaparoo, `M-` MiSTer, `S-` SAM, `U-` Update_All,
`H-` hardware. Each starts as `simulator-only`.

| Id | Claim | Source | Confidence |
|---|---|---|---|
| Z-ENDPOINT | `ws://<host>:7497/api/v0.1`, JSON-RPC 2.0, UUID string ids; `ping`→`pong` | Core `docs/api/index.md` | high |
| Z-LEGACY | MiSTer admits unauthenticated plaintext clients; encryption is off by default there | `permissions.go`; `cmd/mister` sets no default (linux/steamos/windows do) | high |
| Z-LEGACY-CAPS | Legacy gets screenshot and input; not `device.status`/`update.*` | `docs/api/methods.md` § Access | high |
| Z-RUN-WAITS | `run` returns after execution finishes; 30 s server timeout; `data.category` on failure | methods.md § run | high |
| Z-STOP-MENU | `stop` returns an FPGA core to the menu | `run.go:365` → `StopForMenu` → `ReturnToMenu` | medium |
| Z-MEDIAID | `media.meta {mediaId}` returns `zapScript`; ids survive until a reindex | methods.md § media.meta | medium |
| Z-TRACKS-MENU | Games launched from MiSTer's own menu produce `media.started`/`stopped`; matching the exact game needs `recents=1` and `log_file_entry=1` | `tracker/tracker.go`; zaparoo.org `platforms/mister` § Game tracking | medium |
| Z-NOTIFY | Started: `systemId, systemName, mediaPath, mediaName`; stopped adds `launcherId, elapsed` | `docs/api/notifications.md` | high |
| Z-HISTORY | `media.history` holds those sessions, incl. menu launches | methods.md § media.history | medium |
| Z-SCREENSHOT | `screenshot` → `{path, data(base64), size}` on MiSTer | methods.md § screenshot | high |
| Z-NFC | `readers.write {text}` waits for a card; mapping UIDs are normalized (no spaces/colons, lowercase); a scanned blank card shows up in `tokens`/`tokens.history` with its UID | methods.md § readers, mappings, tokens | medium |
| Z-SCRIPT | `**mister.script:update_all.sh` runs it on screen; `run` returns once it starts | zaparoo.org `zapscript/mister.md` | low |
| M-INI-SECTIONS | Section match rules as in `ini_get_section()` | Main_MiSTer `cfg.cpp` | high |
| M-INI-KEYS | Key names, types and ranges from `ini_vars[]` @ `2dd3662` | `cfg.cpp` | high |
| M-INI-APPLY | INI changes apply at the next core load | `cfg.cpp` call sites; Remote's docs | medium |
| M-INI-RENAME | Rename over an existing file is safe on the SD card's exFAT/FAT | none; our guess | low |
| M-CMD | `/dev/MiSTer_cmd` accepts `load_core <path>` | `input.cpp` | high |
| M-CORENAME | `/tmp/CORENAME` holds the running core, `MENU` in the menu | Zaparoo `mister/config` | high |
| M-PATHS | Saves in `/media/fat/saves`, states in `/media/fat/savestates` | MiSTer conventions | medium |
| M-SSH | root SSH on 22 with key auth after `ssh-copy-id` | MiSTer Linux docs | medium |
| S-ACTIVE | SAM is attracting ⇔ a `MiSTer_SAM_on.sh loop_core` process exists | SAM's `there_can_be_only_one` | medium |
| S-HANDOFF | With `playcurrentgame=yes`, SAM exits and reloads the game (a new `media.started`) | `MiSTer_SAM_on.sh` `play_or_exit` | medium |
| U-REBOOT | update_all may reboot (`autoreboot` defaults to true) | Update_All `config.py` | high |
| H-SS1 | The SuperStation One runs MiSTer Linux with the same paths and Zaparoo support | none | low |

## 10. Play tracking (phase 2, designed now)

**Where it runs.** A `track` subcommand, run as a second OpenRC service in the same LXC. It's not
a script on the MiSTer as with Batocera.

- MiSTer exposes events over the network (Zaparoo's WebSocket), so nothing gets installed on the
  SD card, and a reflash can't lose it.
- `serve` over stdio lives only as long as a client session, so it can't be the tracker.

**Missed sessions.** When the LXC was down or the connection dropped, the tracker reconciles on
reconnect. It reads `media.history` back to its last recorded `startedAt` and publishes anything
it missed. Batocera's on-device outbox solves the same problem; here Zaparoo is the outbox.

**Topics and payloads: Batocera's shape, plus what the device is (Q6).**

- Topics: `misterfpga/<host>/now_playing` (retained), `last_session` (retained), `session`
  (event, `event_type: session_end`), and `status`. `<host>` is the host name slugged
  (`office`, `family_room`).
- Payload keys: Batocera's `game, system, rom, source, started, ended, duration_s`, plus
  `host` (the name) and `device` (the configured `model`, e.g. `SuperStation One`). A session
  record on its own says which machine it was played on, even outside Home Assistant.
- HA discovery: the same six entities, under one HA device per MiSTer:
  - `name`: name and model together, e.g. "Family Room SuperStation One", so entities read
    "Family Room SuperStation One Now playing";
  - `model`: the configured model; `manufacturer`: the configured one, else "MiSTer FPGA";
  - `sw_version`: the Zaparoo Core version; `identifiers`: `misterfpga_<host>`.

HA then sees three devices (the arcade and the two MiSTers) with identical entity shapes, so one
automation or dashboard card template covers all three.

**Filtering.**

- `source = "attract"` when S-ACTIVE holds at `media.started`. Those sessions aren't published
  (`IGNORE_ATTRACT=yes`, the default).
- `source = "player"` otherwise.
- `MIN_SESSION_SECONDS = 60`, as Batocera.
- Without SSH, attract detection is unavailable; `get_status` says so, and the tracker logs a
  warning once.

## 11. Deployment

- **stdio:** `mcp-server-misterfpga` for Claude Code and Desktop.
- **HTTP:** `serve --http`, binding `127.0.0.1:8716/misterfpga/mcp` through the shared
  `remote.py`, under the same OpenRC layout as webOS.
- **LAN-only (decided, Q3), as Batocera.** *Revised 2026-10-24: remote access through Cloudflare
  Access, as Batocera now does; see "What changed during the build".* The env file ships with no `MCP_PUBLIC_HOSTS` and no
  Cloudflare settings, and the README says why: this server can start an update that reboots a
  MiSTer, and its SSH key is root on both.

For the record, exposing it later would take a Cloudflare Access application for
`/misterfpga/*`, the `CF_ACCESS_*`, `MCP_ALLOWED_EMAILS` and `MCP_PUBLIC_HOSTS` settings, and a
tunnel ingress rule. `remote.py` already refuses a non-loopback bind without Access (unless
`--insecure-no-auth` is passed, which this server's docs say not to do).

## 12. Decisions (2026-10-13)

| # | Question | Decision | Where it landed |
|---|---|---|---|
| 1 | Drop mrext Remote from v1? | Yes | §1.2, §4 |
| 2 | Zaparoo API key? | No key; legacy access, SSH for disk and core | §1.4 |
| 3 | Remote exposure? | LAN-only; **revised 2026-10-24**: Cloudflare Tunnel + Access, as Batocera | §11, What changed |
| 4 | SSH on both MiSTers? | Yes (still optional per host in the code) | §4 |
| 5 | `BOOTCORE` in `set_ini`? | Denied | §6 |
| 6 | Tracker topics? | `misterfpga/<host>/...`, and every payload and HA device says which device it is | §5 (`model`), §10 |
| 7 | Backup location? | User-configured, no default; OMV is one example, not an assumption | §5, §6 |
| 8 | Promote rate limiting to mcp-server-shared first? | Yes | §7 |
| 9 | Zaparoo on the MiSTers? | Not installed by default, but easy to add; the README documents it | §1.4, §4 |

Snapshot retention stays at 30, configurable.

## Sources

- Zaparoo Core API (in-repo source of truth): `docs/api/index.md`, `methods.md`,
  `notifications.md`, `encryption.md`;
  [published copy](https://zaparoo.org/docs/core/api/).
- ZapScript: [MiSTer commands](https://zaparoo.org/docs/zapscript/mister/),
  [execute](https://zaparoo.org/docs/zapscript/utilities/).
- [mrext Remote](https://github.com/wizzomafizzo/mrext/blob/main/docs/remote.md) and
  [its API](https://github.com/wizzomafizzo/mrext/blob/main/docs/remote-api.md).
- [Main_MiSTer](https://github.com/MiSTer-devel/Main_MiSTer): `cfg.cpp` (`ini_vars`,
  `ini_get_section`), `input.cpp` (`/dev/MiSTer_cmd`).
- [MiSTer_SAM](https://github.com/mrchrisster/MiSTer_SAM): `MiSTer_SAM_on.sh`,
  `MiSTer_SAM.ini`.
- [Update_All_MiSTer](https://github.com/theypsilon/Update_All_MiSTer): `src/update_all/config.py`.
- [python-mister-fpga](https://github.com/hudsonbrendon/python-mister-fpga).

## What changed during the build

The build follows this design. Where the code differs, this is where, and why:

- **`get_ini` / `set_ini` take `file`, not `ini=1..4`.** MiSTer orders its alternate INI files by
  a rule (first three candidates, then a case-insensitive sort) that the server would have to
  reproduce exactly, so a slot number risked editing the wrong file. A file name can't be wrong:
  `get_ini` lists the `MiSTer*.ini` files there are, and `file` defaults to `MiSTer.ini`.
- **`tokens`, not `tokens.history`.** `tokens` already returns the card on the reader now and the
  last one scanned (with `scanTime`), which is all `map_nfc_card` needs, so `tokens.history` came
  off the Zaparoo allow-list.
- **Cores come from `launchers`.** Each MiSTer-core launcher reports
  `misterCore: {name, file, mglPath}`. `list_systems` shows `mglPath` (`_Console/SNES`), and
  `set_ini` uses `name` to warn when a core section (`[SNSE]`) matches no installed core.
- **`screenshot` returns MCP image content** (a `CallToolResult` with the PNG plus a small JSON
  part), so it has no outputSchema. In dry-run mode it still takes the screenshot: it only adds a
  file under `screenshots/`.
- **`get_status` with no MiSTer configured is an error** that says how to add one, rather than an
  empty list.
- **Rate-limit messages** come from the shared `ratelimit.refusal()`. `shared.lock` points at the
  mcp-server-shared#1 branch commit until that PR merges; then `sync_shared.py --update` moves it
  to `main`.
- **Two backups in the same second** get `-2`, `-3` suffixes instead of failing.
- **Remote access (Q3 revised, 2026-10-24).** Batocera moved from stdio-only to an HTTP service
  behind Cloudflare Access (mcp-server-batocera `eb263c5`), and this server follows it so every
  sibling is a connector in the Claude apps. Nothing in the code had to change: `serve --http`
  already ran on `remote.py`, which verifies the `Cf-Access-Jwt-Assertion` on every request
  (signature, audience, issuer, expiry, optional `MCP_ALLOWED_EMAILS`) and refuses a non-loopback
  bind or a `--public-host` without Access. What changed is configuration and proof:
  - `env.example` carries the `MCP_PUBLIC_HOSTS`, `CF_ACCESS_*` and `MCP_ALLOWED_EMAILS` lines;
    the README's "Remote access" and "Service and Cloudflare" sections say how to wire the tunnel
    route (`misterfpga/*` → `localhost:8716`) and the Access application.
  - `tests/test_http_tools.py` runs these tools over HTTP with signed assertions, and shows a bad
    assertion never reaches a MiSTer; the Alpine smoke test calls `get_status` over HTTP and checks
    that Access settings in the env file turn the check on.
  - The reasons for LAN-only (a reboot-capable update, a root SSH key) now argue for the server
    being the gate rather than for no exposure: `MCP_ALLOWED_EMAILS` is recommended, and
    `--insecure-no-auth` stays forbidden for the service.
  - `open_world_hint` stays `False`. It describes what a tool reaches (two MiSTers on the LAN, a
    closed set), not who may call it.
- **Play tracking built (§10, 2026-10-24),** with these differences from the design, mostly from
  following Batocera's playtracker as it now stands (`4200b40`: activity, screensaver and cabinet
  events):
  - **Attract mode is deferred.** No SAM check at `media.started` and no live sensor yet, so
    SAM's demo sessions are logged like real play. When it lands: an `attract` value in Activity
    (Batocera's `screensaver`), and `source = "attract"` sessions not logged.
  - **Activity** (`menu`, `playing`, `off`) is Batocera's enum minus the screensaver. It has only
    the tracker's availability, so a switched-off MiSTer reads `off`, not *unavailable*.
  - **No Cabinet event entity.** Zaparoo has no power events; a reboot and a power-off both look
    like a dropped connection, which `status` and Activity already show.
  - **A real last will.** The tracker holds one MQTT connection (`aiomqtt`, chosen over porting
    Batocera's stdlib publisher because the LXC can pip-install and the server is asyncio already),
    so `misterfpga/_tracker/status` goes offline if it dies; on a clean stop it publishes that
    itself. Entities use both statuses (`availability_mode: all`).
  - **Times on the MiSTer's clock.** `started` from `media.active`, length from `elapsed`, `ended`
    = started + length (new assumption `Z-SESSION-TIMES`). Catch-up compares against Zaparoo's
    history, which is on that clock too, so an LXC with a different clock can't double-log.
  - **First run doesn't backfill.** With no `track-state.json`, the newest history entry becomes
    `last_session` (no event) and catch-up starts there.
  - The simulator now records a history entry when a game ends and reports a real `elapsed`, and
    `FakeZaparoo.play` / `quit_game` stand in for the MiSTer's own menu (`Z-TRACKS-MENU`).


# Hardware validation (MiSTer FPGA)

Everything in this repo has been verified **against the simulator only**. This is the checklist
for the first session with real MiSTers. The steps are ordered so each depends only on the ones
before it, and each names the assumptions it confirms (ids from
`src/misterfpga_mcp/assumptions.py`, sources in [docs/DESIGN.md](docs/DESIGN.md) §9).

Plan on about an hour for the first MiSTer and twenty minutes for the second. Steps 1 to 6 are
the core; 7 to 11 each cover one feature and can wait.

## Before you start

- Zaparoo Core installed and running as a service on each MiSTer (README, "Set up your MiSTer").
- An SSH key copied to each (`ssh-copy-id -i ~/.ssh/mister_ed25519.pub root@<ip>`; password `1`).
- The MiSTer's TV in view.
- On the machine that will run the server:

```sh
git clone https://github.com/SDNick484/mcp-server-misterfpga.git && cd mcp-server-misterfpga
python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
pytest -q          # expect: all passed, 1 skipped (no recordings yet)
```

**Recording a result.** When a step confirms an assumption, set its `status` to
`"hardware-verified"` in `assumptions.py` and in README's table. When it contradicts one, set
`"hardware-contradicted"`, put what you saw in `note`, and keep the output. Commit both.

**Tool calls** use `mcp-server-misterfpga call <tool> key=value ...`: the same MCP layer the
model uses, so arguments are validated the same way. Add `-v` to see the protocol traffic.

---

## 1. Zaparoo answers, without a key

```sh
mcp-server-misterfpga check --host <ip> --name "Office" --model "MiSTer Pi"
```

Expect `Zaparoo Core <version>: ok`. That's a WebSocket JSON-RPC session on 7497 with no key or
pairing.

- Confirms **Z-ENDPOINT** (`check -v` shows the frames, and `ping`/`pong` every 20 s),
  **Z-LEGACY**.
- If it says "connection refused": Zaparoo isn't running. If it says the handshake failed, compare
  the API path with Zaparoo's docs.

## 2. Read-only answers, recorded

```sh
mcp-server-misterfpga doctor --dump tests/fixtures/recorded
pytest tests/test_recorded.py -v
```

Expect doctor's zaparoo lines `ok`, and the replay test passing against your MiSTer's real
answers. Review `tests/fixtures/recorded/*.json` before committing it (it lists your library).

- Confirms part of **Z-LEGACY-CAPS** (search, systems, launchers, readers and mappings answer).

## 3. SSH

```sh
mcp-server-misterfpga check --host <ip> --ssh --ssh-key ~/.ssh/mister_ed25519
```

Compare the fingerprint with `ssh-keyscan <ip> | ssh-keygen -lf -`, then answer `y`. Expect
`SSH login as root: ok (running core: MENU, ...)`.

- Confirms **M-SSH**, **M-CORENAME** (`MENU` while in the menu).
- `call get_status` should now show `core`, `attract_mode` and `free_space_mb`.

## 4. Launch, status, back to the menu

```sh
mcp-server-misterfpga call search_games query=<a game you have>
mcp-server-misterfpga call launch game="<ref from the search>"
mcp-server-misterfpga call get_status host=Office
mcp-server-misterfpga call return_to_menu host=Office
```

Expect the game on the TV, and `launch` returning `done` with no warning (so `media.active`
confirmed it). `get_status` should show the game in `now_playing` and its core in `core`. Then
`return_to_menu` should put the MiSTer back in the menu.

- Confirms **Z-MEDIAID** (the ref went through `media.meta`), **Z-RUN-WAITS** (`-v`: the `run`
  reply arrives after the game is up), **Z-STOP-MENU**.
- Also try `call launch game="Office/999999"`: expect "search again" (`media_not_found`).

## 5. Games launched from the MiSTer's own menu

Turn on `recents=1` and `log_file_entry=1` if doctor warned about them (step 7 shows how with
`set_ini`, or edit the file). Then launch a game **with the controller, from the MiSTer menu**.
Next, `call get_status` and `call play_history`, and run `call screenshot host=Office` while it's
running.

- Confirms **Z-TRACKS-MENU** (`now_playing` shows that game), **Z-HISTORY** (after you quit it,
  `play_history` lists it), and **Z-NOTIFY**: run `serve -v` in another terminal and watch
  `media.started`/`media.stopped` arrive with the documented fields.
- The screenshot comes back as an image, and the file is in `/media/fat/screenshots`. That
  confirms **Z-SCREENSHOT** and the rest of **Z-LEGACY-CAPS**.

## 6. Without Zaparoo: the menu over SSH

Launch a game, then stop Zaparoo on the MiSTer (`/media/fat/Scripts/zaparoo.sh -service stop`
over SSH). Within a minute, `call get_status` should list the Zaparoo limitation. Then run
`call return_to_menu host=Office`. Afterwards, restart Zaparoo, or reboot.

- Confirms **M-CMD**: the menu loads from `load_core /media/fat/menu.rbf` on `/dev/MiSTer_cmd`.

## 7. MiSTer.ini

```sh
mcp-server-misterfpga call get_ini host=Office
mcp-server-misterfpga call set_ini host=Office section=SNES key=vscale_mode value=1 dry_run=true
mcp-server-misterfpga call set_ini host=Office section=SNES key=vscale_mode value=1
```

Expect the diff, then `done` with a `backup` path. Load an SNES game: the picture is scaled per
`vscale_mode=1`. Load another core: unchanged. Over SSH, check that `MiSTer.ini.<time>.bak`
exists and that your comments survived.

- Confirms **M-INI-SECTIONS**, **M-INI-KEYS**, **M-INI-APPLY** (the change shows up only after
  the core loads), and **M-INI-RENAME** (no `MiSTer.ini.tmp` left behind; the file is complete).
- Undo it with `value=null`.

## 8. Save backups

Set `backup_dir` (any writable folder), then:

```sh
mcp-server-misterfpga call backup_saves host=Office
mcp-server-misterfpga call backup_saves host=Office      # again: everything should be "linked"
```

Expect the first run's `copied` to match the files under `saves/` and `savestates/`, and the
second's `copied` to be 0. Compare a save with the MiSTer's copy (`cmp`).

- Confirms **M-PATHS**.

## 9. Attract mode

Start MiSTer Super Attract Mode (Scripts → MiSTer_SAM_on, or let the menu idle). While it cycles
games, `call get_status` should show `attract_mode: true`. With `playcurrentgame=yes` in
`MiSTer_SAM.ini`, pick up a controller: SAM exits and the game reloads. `attract_mode` should then
be `false` with the same game playing.

- Confirms **S-ACTIVE**, **S-HANDOFF** (`serve -v`: a new `media.started` after SAM's exit).

## 10. NFC

With a reader on the MiSTer: `call list_nfc host=Office`. Scan a blank card, then:

```sh
mcp-server-misterfpga call map_nfc_card game="<ref>"
```

Tap the card: the game launches. With a writable reader and a card on it, run
`call write_nfc_card game="<ref>"` and tap that card.

- Confirms **Z-NFC** (the scan showed up in `list_nfc` as `last_card`, normalized; the write
  waited for the card).

## 11. Update All

Only when you want an update anyway:

```sh
mcp-server-misterfpga call run_update host=Office confirm=true
```

Expect Update All on the TV within seconds, and the call returning while it runs.

- Confirms **Z-SCRIPT**, **U-REBOOT** (if it updated Linux or Main, the MiSTer reboots at the end).

## 12. The second MiSTer (the SuperStation One)

Repeat steps 1 to 4 and 7 with `--name "Family Room" --model "SuperStation One"`. Then, with both
configured, run `call search_games query=<a game on both>`. It should come back twice, with two
refs. `call launch system=SNES` without `host` should be refused, naming both.

- Confirms **H-SS1**.

## 13. Play tracking in Home Assistant

With the `track` service running (README, "Play tracking in Home Assistant"), check Home Assistant's
MQTT integration: one device per MiSTer, e.g. *Office MiSTer Pi*, with the entities listed there.

1. Launch a game from the MiSTer's menu. *Activity* turns `playing`, *Now playing* shows it, and the
   *Game session* entity fires `session_start`.
2. Play past `MISTERFPGA_MIN_SESSION_SECONDS`, then quit. *Last played* shows it with a length close
   to what you played, and `session_end` fires.
3. Stop the service (`rc-service mcp-server-misterfpga-track stop`), play and quit another game,
   start it again. Within seconds that game becomes *Last played* and a `session_end` fires for it,
   once. Restart the service again: nothing new fires (it isn't logged twice).
4. While a game runs, restart the service: *Now playing* stays on the game and no second
   `session_start` fires.

- Steps 3 and 4 confirm **Z-SESSION-TIMES** (the live session and the history entry are recognized as
  one) and, again, **Z-HISTORY**. Turn the MiSTer off: its entities go unavailable and *Activity*
  reads `off`.

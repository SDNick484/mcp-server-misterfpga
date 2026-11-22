# Changelog

## [Unreleased]

### Added

- `GET /misterfpga/mcp/healthz`, the health check under the MCP path (the only one a tunnel routing `misterfpga/*` reaches), and both health routes report the installed `commit` and `repo` from `/opt/mcp-server-misterfpga/COMMIT`, which `deploy/alpine/install.sh` now writes (pinning the default source to `main`'s current commit first). `remote.py` and the new `deploy/alpine/commit.sh` synced from mcp-server-shared a48209b; the install needs `git`.
- Per-tool authorization (shared `remote.py`, synced from mcp-server-shared): every tool now has an authorization tier (read, everyday or costly). The costly ones, `mister_return_to_menu`, `mister_set_ini`, `mister_run_update` and `mister_write_nfc_card`, ask the person for confirmation before each call and are refused where the app can't show the prompt (today everything but the Claude Code CLI). `call` asks on the terminal.

### Changed

- A Cloudflare Access service token now gets 403 unless its client ID is in `MCP_ALLOWED_SERVICE_TOKENS`, and automated callers may use only the tiers in `MCP_AUTOMATION_TIERS` (default `read,power-off`).

## [0.1.0]

First release on PyPI.

### Added

- MCP server for one or more MiSTer FPGA devices through Zaparoo Core (games, history, screenshots, NFC, updates) and SSH/SFTP (MiSTer.ini, save backups), never guessing between them.
- `track`: a play tracker that publishes sessions to Home Assistant over MQTT, in the same shapes as mcp-server-batocera's playtracker.
- CLI: `serve` (stdio, or `--http` behind Cloudflare Access), `check`, `doctor`, `call`, `track` and `simulate` (simulated Zaparoo and SSH, no hardware needed).
- `deploy/alpine/`: OpenRC services and install script.

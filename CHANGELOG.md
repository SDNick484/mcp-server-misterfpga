# Changelog

## [Unreleased]

## [0.1.0]

First release on PyPI.

### Added

- MCP server for one or more MiSTer FPGA devices through Zaparoo Core (games, history, screenshots, NFC, updates) and SSH/SFTP (MiSTer.ini, save backups), never guessing between them.
- `track`: a play tracker that publishes sessions to Home Assistant over MQTT, in the same shapes as mcp-server-batocera's playtracker.
- CLI: `serve` (stdio, or `--http` behind Cloudflare Access), `check`, `doctor`, `call`, `track` and `simulate` (simulated Zaparoo and SSH, no hardware needed).
- `deploy/alpine/`: OpenRC services and install script.

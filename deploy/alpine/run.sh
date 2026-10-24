#!/bin/sh
# What the OpenRC services run (as user mcp-mister). Loads the environment
# file, then replaces itself with the server, so supervise-daemon watches the
# server process itself. With no arguments it's `serve --http` (the MCP
# service); mcp-server-misterfpga-track passes `track` (the play tracker).
# Run it by hand to see exactly what a service does:
#   su -s /bin/sh mcp-mister -c /opt/mcp-server-misterfpga/run.sh
#   su -s /bin/sh mcp-mister -c '/opt/mcp-server-misterfpga/run.sh track'
set -eu
ENV_FILE="${MCP_MISTERFPGA_ENV:-/etc/mcp-server-misterfpga/env}"
if [ -r "$ENV_FILE" ]; then
	set -a  # export every variable the file sets
	# shellcheck disable=SC1090
	. "$ENV_FILE"
	set +a
fi
export MISTERFPGA_CONFIG_DIR="${MISTERFPGA_CONFIG_DIR:-/var/lib/mcp-server-misterfpga}"
BIN=/opt/mcp-server-misterfpga/venv/bin/mcp-server-misterfpga
if [ "$#" -gt 0 ]; then
	exec "$BIN" "$@"
fi
# MCP_SERVE_ARGS is split into words on purpose (e.g. "--dry-run --no-redact")
# shellcheck disable=SC2086
exec "$BIN" serve --http ${MCP_SERVE_ARGS:-}

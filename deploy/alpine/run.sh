#!/bin/sh
# What the OpenRC service runs (as user mcp-mister). Loads the environment
# file, then replaces itself with the server, so supervise-daemon watches the
# server process itself. Run it by hand to see exactly what the service does:
#   su -s /bin/sh mcp-mister -c /opt/mcp-server-misterfpga/run.sh
set -eu
ENV_FILE="${MCP_MISTERFPGA_ENV:-/etc/mcp-server-misterfpga/env}"
if [ -r "$ENV_FILE" ]; then
	set -a  # export every variable the file sets
	# shellcheck disable=SC1090
	. "$ENV_FILE"
	set +a
fi
export MISTERFPGA_CONFIG_DIR="${MISTERFPGA_CONFIG_DIR:-/var/lib/mcp-server-misterfpga}"
# MCP_SERVE_ARGS is split into words on purpose (e.g. "--dry-run --no-redact")
# shellcheck disable=SC2086
exec /opt/mcp-server-misterfpga/venv/bin/mcp-server-misterfpga serve --http ${MCP_SERVE_ARGS:-}

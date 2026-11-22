#!/bin/sh
# Install mcp-server-misterfpga as an OpenRC service on Alpine (e.g. a Proxmox LXC).
#
#   sh deploy/alpine/install.sh [SOURCE]      # SOURCE: a checkout dir or a pip spec
#
# Layout (each part owned so the service can read its settings, keep its
# credentials to itself, and change nothing else):
#   /opt/mcp-server-misterfpga/venv     the code                    root:root            0755
#   /opt/mcp-server-misterfpga/run.sh   the service command         root:root            0755
#   /opt/mcp-server-misterfpga/COMMIT   the installed commit, if any root:root            0644
#   /etc/mcp-server-misterfpga/env      HTTP, Access, MQTT settings root:mcp-mister      0640
#   /var/lib/mcp-server-misterfpga/     config.json, the SSH key    mcp-mister           0700 / files 0600
#                                          (MISTERFPGA_CONFIG_DIR)
#   /var/log/mcp-server-misterfpga/     server.log, track.log       mcp-mister           0750
#   /etc/init.d/mcp-server-misterfpga   the OpenRC script (MCP over HTTP)
#   /etc/init.d/mcp-server-misterfpga-track   the play tracker (MQTT), optional
# The SSH key is root on your MiSTers, so it belongs to the service user alone.
# backup_dir (in config.json) must be writable by mcp-mister.
# Re-running upgrades the code and keeps settings.
# COMMIT (the SHA installed, and this repo) is what /healthz and /misterfpga/mcp/healthz report; the
# default SOURCE is pinned to main's current commit first, so the stamp names what pip installed.
set -eu

SRC="${1:-git+https://github.com/SDNick484/mcp-server-misterfpga}"
USER_NAME=mcp-mister
PREFIX=/opt/mcp-server-misterfpga
ETC=/etc/mcp-server-misterfpga
STATE=/var/lib/mcp-server-misterfpga
HERE=$(cd "$(dirname "$0")" && pwd)

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

# python3 and its venv module; every compiled dependency (cryptography,
# aiohttp, pydantic-core) ships a musl wheel, so no compiler is needed.
apk add --no-cache python3 py3-pip openrc openssh-client git >/dev/null

if ! id "$USER_NAME" >/dev/null 2>&1; then
	addgroup -S "$USER_NAME"
	adduser -S -D -H -h "$STATE" -s /sbin/nologin -G "$USER_NAME" "$USER_NAME"
fi

# Which commit SOURCE is, worked out before pip (commit.sh, shared with the sibling servers)
. "$HERE/commit.sh"
REPO=SDNick484/mcp-server-misterfpga
pinned=$(commit_source "$SRC" "$REPO")
SRC=$(printf '%s\n' "$pinned" | sed -n 1p)
COMMIT=$(printf '%s\n' "$pinned" | sed -n 2p)

python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet --upgrade "$SRC"
# Only now that pip succeeded (set -e stops before this otherwise), so COMMIT never names code that isn't installed
write_commit "$PREFIX" "$COMMIT" "$REPO"
install -m 0755 "$HERE/run.sh" "$PREFIX/run.sh"

install -d -m 0750 -o root -g "$USER_NAME" "$ETC"
[ -e "$ETC/env" ] || install -m 0640 -o root -g "$USER_NAME" "$HERE/env.example" "$ETC/env"
install -d -m 0700 -o "$USER_NAME" -g "$USER_NAME" "$STATE"
install -d -m 0750 -o "$USER_NAME" -g "$USER_NAME" /var/log/mcp-server-misterfpga
install -m 0755 "$HERE/mcp-server-misterfpga.initd" /etc/init.d/mcp-server-misterfpga
install -m 0755 "$HERE/mcp-server-misterfpga-track.initd" /etc/init.d/mcp-server-misterfpga-track

AS_SERVICE="su -s /bin/sh $USER_NAME -c"
CMD="MISTERFPGA_CONFIG_DIR=$STATE $PREFIX/venv/bin/mcp-server-misterfpga"
echo "Installed $("$PREFIX/venv/bin/mcp-server-misterfpga" --version)."
echo "Next, as the service user: make its SSH key, add it to each MiSTer (password 1), check, then start:"
echo "  $AS_SERVICE 'ssh-keygen -t ed25519 -N \"\" -f $STATE/mister_ed25519'"
echo "  $AS_SERVICE 'ssh-copy-id -i $STATE/mister_ed25519.pub root@<mister ip>'"
echo "  $AS_SERVICE '$CMD check --host <mister ip> --name \"Family Room\" --model \"SuperStation One\" --ssh --ssh-key $STATE/mister_ed25519'"
echo "  $AS_SERVICE '$CMD doctor'"
echo "  rc-update add mcp-server-misterfpga default && rc-service mcp-server-misterfpga start"
echo "It listens on 127.0.0.1:8716/misterfpga/mcp. For a Claude connector, see the README's"
echo "'Service and Cloudflare' section (CF_ACCESS_* and MCP_PUBLIC_HOSTS in $ETC/env)."
echo "Play tracking in Home Assistant (optional): set MISTERFPGA_MQTT_* in $ETC/env, then"
echo "  rc-update add mcp-server-misterfpga-track default && rc-service mcp-server-misterfpga-track start"

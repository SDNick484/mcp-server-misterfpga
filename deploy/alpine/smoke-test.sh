#!/bin/sh
# CI: install and run the service in a stock python:3.12-alpine container,
# against two simulated MiSTers. Proves the dependencies install on musl
# (aiohttp, asyncssh/cryptography, pydantic-core), the install layout and
# permissions, and that OpenRC starts and stops it.
#   docker run --rm -v "$PWD:/src" -w /src python:3.12-alpine sh deploy/alpine/smoke-test.sh
set -eux

# On failure, say where as a GitHub annotation, with the end of the service log.
report() {
	status=$?
	if [ "$status" != 0 ]; then
		tail_log=$(tail -n 30 "${STEP_LOG:-/var/log/mcp-server-misterfpga/server.log}" 2>/dev/null | sed ':a;N;$!ba;s/%/%25/g;s/\n/%0A/g')
		echo "::error title=Alpine smoke test failed (exit $status)::last step: ${STEP:-?}%0A$tail_log"
	fi
}
trap report EXIT

STEP='install'
apk add --no-cache openrc curl >/dev/null
sh deploy/alpine/install.sh /src

STEP='pytest on musl'
/opt/mcp-server-misterfpga/venv/bin/pip install --quiet "/src[dev]"
STEP_LOG=/tmp/pytest.log
cd /src
/opt/mcp-server-misterfpga/venv/bin/pytest -q -p no:cacheprovider -rf >"$STEP_LOG" 2>&1 || { cat "$STEP_LOG"; false; }
tail -n 3 "$STEP_LOG"
cd /
STEP_LOG=''

STATE=/var/lib/mcp-server-misterfpga
BIN=/opt/mcp-server-misterfpga/venv/bin/mcp-server-misterfpga
STEP='simulator'
# Two simulated MiSTers, started as the service user; their config becomes the service's
su -s /bin/sh mcp-mister -c "$BIN simulate --dir $STATE/sim" &
for _ in $(seq 100); do [ -s "$STATE/sim/config/config.json" ] && break; sleep 0.1; done
install -m 0600 -o mcp-mister -g mcp-mister "$STATE/sim/config/config.json" "$STATE/config.json"

STEP='service start'
# OpenRC in a container: it needs to believe it has booted, and that the
# network (which the service `need`s) is provided by the host
printf 'rc_sys="docker"\nrc_provide="loopback net"\n' >>/etc/rc.conf
mkdir -p /run/openrc && touch /run/openrc/softlevel
rc-service mcp-server-misterfpga start
for _ in $(seq 50); do curl -fsS http://127.0.0.1:8716/healthz && break; sleep 0.2; done
rc-service mcp-server-misterfpga status

STEP='service user'
# shellcheck disable=SC2009
pid=$(ps -o pid,args | grep '[m]cp-server-misterfpga serve' | awk 'NR==1 {print $1}')
[ -n "$pid" ] && [ "$(stat -c %U "/proc/$pid")" = mcp-mister ]
STEP='file modes'
[ "$(stat -c %a $STATE)" = 700 ]
[ "$(stat -c '%a %U' $STATE/config.json)" = "600 mcp-mister" ]
STEP='doctor'
su -s /bin/sh mcp-mister -c "MISTERFPGA_CONFIG_DIR=$STATE $BIN doctor --timeout 2"

STEP='service stop'
rc-service mcp-server-misterfpga stop
cat /var/log/mcp-server-misterfpga/server.log
echo "Alpine smoke test passed"

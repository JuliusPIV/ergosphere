#!/usr/bin/env bash
# A config file without a "port" key must not send the installer to the
# default port 9241 when --port names another one and nothing is installed
# in the install dir yet (issue #141).
#
# Run: ERGOSPHERE_TEST_PORT=<spare port> bash tests/install_config_noport_test.sh
# curl is a wrapper that logs every URL it is given and refuses ports
# 9119-9121, 9241 and 9242 without connecting. No network: the package is
# served from 127.0.0.1 and grok is a stub that only answers --version.
# Every server it starts is stopped at the end.
set -u
repo=$(cd "$(dirname "$0")/.." && pwd)
port=${ERGOSPHERE_TEST_PORT:-$(python3 -I "$repo/tests/free_port.py" 3)}
work=$(mktemp -d "${TMPDIR:-/tmp}/ergosphere-noport-test.XXXXXX")
real_curl=$(command -v curl)
pids=()
fail=0

# shellcheck disable=SC2317  # run by the EXIT trap
cleanup() {
  local p
  for p in ${pids[@]+"${pids[@]}"}; do kill "$p" 2>/dev/null; done
  # The server is not our child, so poll until it has released the port.
  for p in ${pids[@]+"${pids[@]}"}; do
    for _ in $(seq 50); do kill -0 "$p" 2>/dev/null || break; sleep 0.1; done
  done
  rm -rf -- "$work"
}
trap cleanup EXIT

ok() { echo "ok:   $1"; }
bad() { echo "FAIL: $1"; fail=1; }

mkdir -p "$work/pkg/ergosphere-test" "$work/bin" "$work/www" "$work/u"
cp -p "$repo/ergosphere.py" "$repo/LICENSE" "$work/pkg/ergosphere-test/"
cp -rp "$repo/vendor" "$work/pkg/ergosphere-test/"
tar -czf "$work/www/src.tar.gz" -C "$work/pkg" ergosphere-test
printf '#!/bin/sh\necho "grok 0.0.0-test"\n' >"$work/bin/grok"
cat >"$work/bin/curl" <<EOF
#!/bin/sh
for a in "\$@"; do
  case "\$a" in
    *://*) printf '%s\n' "\$a" >>"$work/curl.log"
      case "\$a" in *:9119/*|*:9120/*|*:9121/*|*:9241/*|*:9242/*) exit 7 ;; esac ;;
  esac
done
exec "$real_curl" "\$@"
EOF
chmod +x "$work/bin/grok" "$work/bin/curl"

python3 -I -c '
import functools, http.server, sys
h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=sys.argv[1])
h.log_message = lambda *a: None
s = http.server.HTTPServer(("127.0.0.1", 0), h)
open(sys.argv[2], "w").write(str(s.server_address[1]))
s.serve_forever()' "$work/www" "$work/www.port" </dev/null >/dev/null 2>&1 &
pids+=($!)
for _ in $(seq 50); do [[ -s "$work/www.port" ]] && break; sleep 0.1; done
url="http://127.0.0.1:$(cat "$work/www.port")/src.tar.gz"

# The config exists and has agents but no port.
printf '{"bind": "127.0.0.1", "agents": {}}\n' >"$work/u/cfg.json"

run_install() {
  : >"$work/curl.log"
  env -u ERGOSPHERE_PORT HOME="$work/u" PATH="$work/bin:$PATH" TMPDIR="$work" \
    ERGOSPHERE_CONFIG="$work/u/cfg.json" ERGOSPHERE_INSTALL_SOURCE_URL="$url" \
    bash "$repo/install.sh" --auto --dir "$work/u/app" --port "$port" "$@" </dev/null 2>&1
}
field() { sed -n "s/^  $1: *//p" <<<"$2" | head -n 1; }
other_ports() { grep -v -e ":$port/" -e "$url" "$work/curl.log"; }

rc=0
out=$(run_install --check) || rc=$?
if [[ "$rc" -eq 0 ]] && ! other_ports >/dev/null; then
  ok "--check --port $port probes only $port"
else
  bad "--check --port $port: exit $rc, other URLs: $(other_ports | tr '\n' ' ')"
  printf '%s\n' "$out" | tail -n 8
fi

rc=0
out=$(run_install) || rc=$?
pid=$(field pid "$out")
if [[ -n "$pid" ]]; then pids+=("$pid"); fi
if [[ "$rc" -eq 0 && -n "$pid" ]] && ! other_ports >/dev/null; then
  ok "install --port $port runs pid $pid and probes only $port"
else
  bad "install --port $port: exit $rc, pid '${pid:-none}', other URLs: $(other_ports | tr '\n' ' ')"
  printf '%s\n' "$out" | tail -n 8
fi

exit "$fail"

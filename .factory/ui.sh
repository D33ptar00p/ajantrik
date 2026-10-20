#!/usr/bin/env bash
# Factory dashboard launcher.
#
#   .factory/ui.sh [start] [port]   serve .factory/ on 127.0.0.1 and open the dashboard (default port 8000)
#   .factory/ui.sh stop             stop the server this script started
#
# The server runs detached and survives the Claude session; its pid is kept in .factory/.ui.pid.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$DIR/.ui.pid"
LOG="$DIR/.ui.log"
ACTION="start"
if [[ "${1:-}" == "start" || "${1:-}" == "stop" ]]; then
    ACTION="$1"
    shift
fi
PORT="${1:-${FACTORY_UI_PORT:-8000}}"
URL="http://127.0.0.1:$PORT/dashboard.html"

running_pid() {
    [[ -f "$PIDFILE" ]] || return 1
    local pid
    pid="$(cat "$PIDFILE")"
    kill -0 "$pid" 2>/dev/null && echo "$pid"
}

serves_dashboard() {
    curl -fsS --max-time 2 "$URL" 2>/dev/null | grep -q 'name="factory-dashboard"'
}

open_browser() {
    if command -v xdg-open >/dev/null; then
        xdg-open "$URL" >/dev/null 2>&1 &
    elif command -v open >/dev/null; then
        open "$URL"
    elif command -v cmd.exe >/dev/null; then
        cmd.exe /c start "" "$URL"
    else
        return 1
    fi
}

if [[ "$ACTION" == "stop" ]]; then
    if pid="$(running_pid)"; then
        kill "$pid" && rm -f "$PIDFILE"
        echo "factory ui: stopped (pid $pid)"
    else
        rm -f "$PIDFILE"
        echo "factory ui: not running"
    fi
    exit 0
fi

mkdir -p "$DIR/jobs"

if serves_dashboard; then
    echo "factory ui: already running at $URL"
else
    if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
        echo "factory ui: port $PORT is used by something else; run: .factory/ui.sh $((PORT + 1))" >&2
        exit 1
    fi
    # Same as `python3 -m http.server --bind 127.0.0.1`, minus the per-request access log: the
    # dashboard polls every 2 s, so that log would grow forever. Startup errors still reach $LOG.
    nohup python3 -c '
import functools, http.server, sys
class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass
handler = functools.partial(Quiet, directory=sys.argv[2])
http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), handler).serve_forever()
' "$PORT" "$DIR" >"$LOG" 2>&1 &
    echo $! >"$PIDFILE"
    for _ in $(seq 1 20); do
        serves_dashboard && break
        sleep 0.25
    done
    if ! serves_dashboard; then
        echo "factory ui: server did not start; see $LOG" >&2
        exit 1
    fi
    echo "factory ui: serving $DIR at $URL (pid $(cat "$PIDFILE"))"
fi

open_browser || echo "factory ui: open $URL in your browser"

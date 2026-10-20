#!/usr/bin/env bash
# Run a command while a checkout of OmniGrid's web app is served on the factory port.
#
#   .factory/harness/with-app.sh <checkout> [--preview <dist-dir>] -- <command> [args...]
#
# Takes the port lock (shared with gate.sh and every other job), starts `vite` in <checkout>/web
# (or `vite preview` of an existing build with --preview) on 127.0.0.1:$FACTORY_APP_PORT, waits
# for it, runs the command with APP_URL set, then stops the server and releases the lock.
#
# Dev mode serves from "/" and exposes window.__map, which shoot.mjs uses to wait for the map to
# settle and to click features by coordinate. Preview mode serves the build at its base (set
# OMNIGRID_BASE, e.g. /omnigrid/, to match the build) and has no __map.
#
# The checkout needs web/node_modules and data/dist (symlinks are fine). Output is redacted.
set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FACTORY_DIR="$(dirname "$HARNESS_DIR")"
export CLAUDE_PROJECT_DIR="$(cd "$FACTORY_DIR/.." && pwd)" WITH_APP_REDACT="$FACTORY_DIR/security/policy.py"
PORT="${FACTORY_APP_PORT:-5199}"
LOCK="${FACTORY_LOCK:-$FACTORY_DIR/.port.lock}"

if [[ $# -lt 3 ]]; then
    echo "usage: $0 <checkout> [--preview <dist-dir>] -- <command> [args...]" >&2
    exit 2
fi
CHECKOUT="$(cd "$1" && pwd)" || exit 2
shift
MODE=dev
DIST=""
if [[ "${1:-}" == "--preview" ]]; then
    MODE=preview
    DIST="$(cd "${2:?--preview needs a dist dir}" && pwd)" || exit 2
    shift 2
fi
[[ "${1:-}" == "--" ]] || { echo "with-app: expected -- before the command" >&2; exit 2; }
shift

[[ -d "$CHECKOUT/web/node_modules" ]] || { echo "with-app: $CHECKOUT/web/node_modules missing" >&2; exit 2; }
[[ -f "$CHECKOUT/data/dist/manifest.json" ]] || { echo "with-app: $CHECKOUT/data/dist missing" >&2; exit 2; }

BASE_PATH="/"
[[ $MODE == preview ]] && BASE_PATH="${OMNIGRID_BASE:-/}"
export APP_URL="http://127.0.0.1:$PORT$BASE_PATH"
export WITH_APP_CHECKOUT="$CHECKOUT" WITH_APP_MODE="$MODE" WITH_APP_PORT="$PORT" WITH_APP_DIST="$DIST"
export WITH_APP_SERVER_LOG="${WITH_APP_SERVER_LOG:-}"

echo "with-app: waiting for $LOCK" >&2
exec flock "$LOCK" bash -c '
    set -uo pipefail
    SERVER_LOG="${WITH_APP_SERVER_LOG:-$(mktemp -t with-app.XXXXXX.log)}"
    # The server runs in its own process group (npx forks node), so stopping it kills the group.
    # The command below still runs from the directory the caller started in.
    if [[ $WITH_APP_MODE == preview ]]; then
        setsid bash -c "cd \"\$1/web\" && exec npx --no-install vite preview --outDir \"\$2\" --port \"\$3\" --strictPort --host 127.0.0.1" _ \
            "$WITH_APP_CHECKOUT" "$WITH_APP_DIST" "$WITH_APP_PORT" >"$SERVER_LOG" 2>&1 &
    else
        OMNIGRID_BASE=/ setsid bash -c "cd \"\$1/web\" && exec npx --no-install vite --port \"\$2\" --strictPort --host 127.0.0.1" _ \
            "$WITH_APP_CHECKOUT" "$WITH_APP_PORT" >"$SERVER_LOG" 2>&1 &
    fi
    SERVER=$!
    trap "kill -- -$SERVER 2>/dev/null; wait $SERVER 2>/dev/null" EXIT
    redact() { python3 "$WITH_APP_REDACT" redact; }
    for _ in $(seq 1 120); do
        curl -fsS --max-time 2 "$APP_URL" >/dev/null 2>&1 && break
        kill -0 $SERVER 2>/dev/null || { echo "with-app: server exited:" >&2; redact <"$SERVER_LOG" >&2; exit 2; }
        sleep 0.5
    done
    curl -fsS --max-time 2 "$APP_URL" >/dev/null 2>&1 || { echo "with-app: server did not start" >&2; redact <"$SERVER_LOG" >&2; exit 2; }
    echo "with-app: $WITH_APP_CHECKOUT ($WITH_APP_MODE) at $APP_URL" >&2
    "$@" 2>&1 | redact
' _ "$@"

#!/usr/bin/env bash
# Run a command while a checkout of accounts-portal is served on the Auth0-allowed port.
#
#   .factory/harness/with-app.sh <checkout> [--preview] -- <command> [args...]
#
# Takes the port lock (shared with gate.sh and every other job), starts `vite` (or `vite preview`
# of an existing build in <checkout>/dist with --preview) on localhost:$FACTORY_APP_PORT, waits for
# it, runs the command with APP_URL set, then stops the server and releases the lock.
# The checkout needs node_modules and a .env (symlinks are fine). Output (the command's and the
# server log on failure) is redacted: .env secret values and token patterns never reach the agent.
set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FACTORY_DIR="$(dirname "$HARNESS_DIR")"
export CLAUDE_PROJECT_DIR="$(cd "$FACTORY_DIR/.." && pwd)" WITH_APP_REDACT="$FACTORY_DIR/security/policy.py"
PORT="${FACTORY_APP_PORT:-5173}"
LOCK="${FACTORY_LOCK:-$FACTORY_DIR/.port.lock}"

if [[ $# -lt 3 ]]; then
    echo "usage: $0 <checkout> [--preview] -- <command> [args...]" >&2
    exit 2
fi
CHECKOUT="$(cd "$1" && pwd)" || exit 2
shift
MODE=dev
if [[ "${1:-}" == "--preview" ]]; then
    MODE=preview
    shift
fi
[[ "${1:-}" == "--" ]] || { echo "with-app: expected -- before the command" >&2; exit 2; }
shift

[[ -d "$CHECKOUT/node_modules" ]] || { echo "with-app: $CHECKOUT/node_modules missing" >&2; exit 2; }
[[ -e "$CHECKOUT/.env" ]] || { echo "with-app: $CHECKOUT/.env missing" >&2; exit 2; }

export APP_URL="http://localhost:$PORT"
export WITH_APP_CHECKOUT="$CHECKOUT" WITH_APP_MODE="$MODE" WITH_APP_PORT="$PORT"

echo "with-app: waiting for $LOCK" >&2
exec flock "$LOCK" bash -c '
    set -uo pipefail
    SERVER_LOG="$(mktemp -t with-app.XXXXXX.log)"
    cd "$WITH_APP_CHECKOUT"
    if [[ $WITH_APP_MODE == preview ]]; then
        npx --no-install vite preview --port "$WITH_APP_PORT" --strictPort --host localhost >"$SERVER_LOG" 2>&1 &
    else
        npx --no-install vite --port "$WITH_APP_PORT" --strictPort --host localhost >"$SERVER_LOG" 2>&1 &
    fi
    SERVER=$!
    trap "kill $SERVER 2>/dev/null; wait $SERVER 2>/dev/null" EXIT
    redact() { python3 "$WITH_APP_REDACT" redact; }
    for _ in $(seq 1 120); do
        curl -fsS --max-time 2 "$APP_URL/" >/dev/null 2>&1 && break
        kill -0 $SERVER 2>/dev/null || { echo "with-app: server exited:" >&2; redact <"$SERVER_LOG" >&2; exit 2; }
        sleep 0.5
    done
    curl -fsS --max-time 2 "$APP_URL/" >/dev/null 2>&1 || { echo "with-app: server did not start" >&2; redact <"$SERVER_LOG" >&2; exit 2; }
    echo "with-app: $WITH_APP_CHECKOUT ($WITH_APP_MODE) at $APP_URL" >&2
    "$@" 2>&1 | redact
' _ "$@"

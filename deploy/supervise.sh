#!/usr/bin/env bash
#
# Long-lived supervisor for the dashboard, run BY pm2 (see ecosystem.config.js).
#
# PM2 already restarts a process that exits. This covers the two failures it
# cannot see:
#
#   1. liveness — a process whose event loop has wedged stays "online" forever
#      while serving nothing. /healthz reports whether the poll loop is actually
#      still turning, which is a different question from "is the process alive".
#   2. code changes — pick up edits automatically, but only once the new code
#      genuinely imports, so a half-finished save can't take the site down.
#      (This is why PM2's own `watch` is off: it would bounce straight into the
#      broken save and leave nothing running.)
#
# Runs as the dashboard user, not root — it needs nothing more than access to
# its own pm2 daemon.
#
# Deliberately not `set -e`: a failing probe is the normal path here and must
# reach the restart logic rather than kill the loop.
set -uo pipefail

TARGET_APP="${TARGET_APP:-mc-dashboard}"
APP_DIR="${APP_DIR:-/home/westcliff7799/mc-dashboard-1}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8080/healthz}"
INTERVAL="${INTERVAL:-10}"
WATCH_CODE="${WATCH_CODE:-1}"
# /healthz only reads a cached timestamp, so a healthy answer is immediate and
# 5s is already generous. This bounds the cost of a *failing* probe, which is
# what sets the worst-case detection time below.
PROBE_TIMEOUT="${PROBE_TIMEOUT:-5}"
# One bad probe is a hiccup; three in a row is a wedge. Acting on the first
# would make a single slow poll look fatal. Worst case to recovery is
# FAIL_THRESHOLD * (PROBE_TIMEOUT + INTERVAL) — about 45s at these defaults.
FAIL_THRESHOLD="${FAIL_THRESHOLD:-3}"

# State lives in the loop rather than on disk. If this supervisor is itself
# restarted it simply re-baselines, which is the correct behaviour anyway.
failures=0
fingerprint=""

log() { echo "[supervise] $*"; }

app_status() {
    # pm2's own text output is for humans and shifts between versions; the JSON
    # is the stable interface.
    pm2 jlist 2>/dev/null | python3 -c '
import json, sys
name = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    print("unknown"); raise SystemExit
for app in data:
    if app.get("name") == name:
        print(app.get("pm2_env", {}).get("status", "unknown")); break
else:
    print("missing")
' "$TARGET_APP"
}

code_fingerprint() {
    find "$APP_DIR/app" "$APP_DIR/static" -type f \
        \( -name '*.py' -o -name '*.html' -o -name '*.js' -o -name '*.css' \) \
        -printf '%T@ %s %p\n' 2>/dev/null | sort | sha256sum | cut -d' ' -f1
}

restart_target() {
    log "restarting $TARGET_APP: $1"
    pm2 restart "$TARGET_APP" --update-env >/dev/null 2>&1 \
        || log "pm2 restart failed — will retry next tick"
    failures=0
}

log "watching $TARGET_APP every ${INTERVAL}s (health=$HEALTH_URL, watch_code=$WATCH_CODE)"

while true; do
    sleep "$INTERVAL"

    status="$(app_status)"
    case "$status" in
        online) ;;
        errored|stopped_with_error)
            restart_target "pm2 reports status '$status'"
            continue
            ;;
        stopped)
            # Someone ran `pm2 stop` on purpose. Fighting a deliberate stop is
            # worse than the downtime, so say why we did nothing and move on.
            log "status 'stopped' — assuming a deliberate stop, not intervening"
            continue
            ;;
        *)
            # launching / stopping / missing: mid-transition, ask again later.
            continue
            ;;
    esac

    # ---- job 2: restart on code changes ----
    if [[ "$WATCH_CODE" == "1" ]]; then
        current="$(code_fingerprint)"
        # An empty baseline means this is our first tick. Record it and move on,
        # or every supervisor start would bounce the dashboard once for nothing.
        if [[ -n "$fingerprint" && "$current" != "$fingerprint" ]]; then
            fingerprint="$current"
            # Importing is a stricter gate than a syntax check — it catches the
            # bad name and the missing module too — and it runs the code without
            # ever binding the port or starting the poller.
            if (cd "$APP_DIR" && timeout 30 .venv/bin/python -c "import app.main") >/dev/null 2>&1; then
                restart_target "code changed and imports cleanly"
            else
                log "code changed but does not import — keeping the running version up"
            fi
            continue
        fi
        fingerprint="$current"
    fi

    # ---- job 1: liveness ----
    if curl -fsS --max-time "$PROBE_TIMEOUT" -o /dev/null "$HEALTH_URL"; then
        failures=0
        continue
    fi

    failures=$(( failures + 1 ))
    log "health probe failed ($failures/$FAIL_THRESHOLD)"
    if (( failures >= FAIL_THRESHOLD )); then
        restart_target "unhealthy for $failures consecutive probes"
    fi
done

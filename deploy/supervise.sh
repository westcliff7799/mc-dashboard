#!/usr/bin/env bash
#
# One supervision tick, driven by mc-dashboard-supervise.timer.
#
# Two jobs live here rather than in two units, so that when the dashboard
# restarts on its own there is exactly one place to look for the reason:
#
#   1. liveness — systemd restarts a process that *exits*, but a process whose
#      event loop has wedged stays "active" forever while serving nothing.
#      /healthz reports whether the poll loop is actually still turning.
#   2. code changes — pick up edits automatically, but only once the new code
#      genuinely imports, so a half-finished save can't take the site down.
#
# Deliberately not `set -e`: a failing probe is the normal path here and must
# reach the restart logic below rather than abort the script.
set -uo pipefail

UNIT="${UNIT:-mc-dashboard.service}"
APP_DIR="${APP_DIR:-/home/westcliff7799/mc-dashboard-1}"
APP_USER="${APP_USER:-westcliff7799}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8080/healthz}"
WATCH_CODE="${WATCH_CODE:-1}"
# Consecutive bad probes before we act. One bad probe is a hiccup; three across
# 45s is a wedge. Restarting on the first would make a slow poll look fatal.
FAIL_THRESHOLD="${FAIL_THRESHOLD:-3}"

# /run is tmpfs, so this resets every boot — which is what we want for the
# fingerprint baseline. Overridable so the logic can be exercised without root.
STATE_DIR="${STATE_DIR:-/run/mc-dashboard-supervise}"
FAIL_FILE="$STATE_DIR/consecutive-failures"
HASH_FILE="$STATE_DIR/code-fingerprint"
mkdir -p "$STATE_DIR"

# The import gate below runs code out of a directory the dashboard user can
# write. This script runs as root (it has to, to restart the unit), and root
# importing user-writable Python would turn "attacker got the app account"
# straight into "attacker got root". Drop back down for that one step.
as_app_user() {
    if [[ $EUID -eq 0 ]]; then
        runuser -u "$APP_USER" -- "$@"
    else
        "$@"   # already unprivileged, e.g. when exercising this by hand
    fi
}

restart_unit() {
    echo "restarting $UNIT: $1"
    systemctl restart "$UNIT"
    echo 0 >"$FAIL_FILE"
}

state="$(systemctl is-active "$UNIT" 2>/dev/null)"

case "$state" in
    failed)
        # systemd gave up (or the unit crashed out). This is ours to fix.
        restart_unit "unit was in the failed state"
        exit 0
        ;;
    active) ;;
    *)
        # inactive/deactivating: almost always somebody ran `systemctl stop` on
        # purpose. Fighting a deliberate stop is worse than the downtime, so
        # say why we did nothing and leave it alone.
        echo "unit is '$state' — assuming a deliberate stop, not intervening"
        exit 0
        ;;
esac

# ---- job 2: restart on code changes ----
if [[ "$WATCH_CODE" == "1" ]]; then
    fingerprint="$(find "$APP_DIR/app" "$APP_DIR/static" -type f \
        \( -name '*.py' -o -name '*.html' -o -name '*.js' -o -name '*.css' \) \
        -printf '%T@ %s %p\n' 2>/dev/null | sort | sha256sum | cut -d' ' -f1)"
    previous="$(cat "$HASH_FILE" 2>/dev/null)"
    echo "$fingerprint" >"$HASH_FILE"

    # An empty baseline means this is the first tick after a reboot. Record it
    # and move on — otherwise every boot would restart the service once for no
    # reason. /run is tmpfs, so this is the normal state after every boot.
    if [[ -n "$previous" && "$fingerprint" != "$previous" ]]; then
        # Importing is a stricter gate than a syntax check: it catches the bad
        # name and the missing import too, and it runs the module without ever
        # binding the port or starting the poller.
        if (cd "$APP_DIR" && as_app_user timeout 30 .venv/bin/python -c "import app.main") >/dev/null 2>&1; then
            restart_unit "code changed and imports cleanly"
        else
            echo "code changed but does not import — keeping the running version up"
        fi
        exit 0
    fi
fi

# ---- job 1: liveness ----
if curl -fsS --max-time 10 -o /dev/null "$HEALTH_URL"; then
    echo 0 >"$FAIL_FILE"
    exit 0
fi

failures=$(( $(cat "$FAIL_FILE" 2>/dev/null || echo 0) + 1 ))
echo "$failures" >"$FAIL_FILE"
echo "health probe failed ($failures/$FAIL_THRESHOLD)"

if (( failures >= FAIL_THRESHOLD )); then
    restart_unit "unhealthy for $failures consecutive probes"
fi
exit 0

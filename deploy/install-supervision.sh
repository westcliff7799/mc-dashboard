#!/usr/bin/env bash
#
# Install the uptime machinery. Run with sudo from the repo root:
#
#     sudo deploy/install-supervision.sh
#
# Idempotent — safe to re-run after editing any of the units.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "needs root: sudo $0" >&2
    exit 1
fi

echo "==> app + supervisor units"
install -m 644 "$REPO/deploy/mc-dashboard.service"            /etc/systemd/system/
install -m 644 "$REPO/deploy/mc-dashboard-supervise.service"  /etc/systemd/system/
install -m 644 "$REPO/deploy/mc-dashboard-supervise.timer"    /etc/systemd/system/

# Root-owned and outside the repo on purpose: this runs as root, so leaving it
# where the dashboard account can rewrite it would hand that account root.
install -m 755 -o root -g root "$REPO/deploy/supervise.sh" /usr/local/sbin/mc-dashboard-supervise

echo "==> hardware watchdog"
install -d -m 755 /etc/systemd/system.conf.d
install -m 644 "$REPO/deploy/watchdog.conf" /etc/systemd/system.conf.d/

echo "==> reboot on kernel panic"
install -m 644 "$REPO/deploy/99-panic-reboot.conf" /etc/sysctl.d/
sysctl -q -p /etc/sysctl.d/99-panic-reboot.conf

systemctl daemon-reload
systemctl enable --now mc-dashboard.service
systemctl enable --now mc-dashboard-supervise.timer

echo
echo "Done. The watchdog setting needs a reboot to take effect; everything"
echo "else is live now. Verify with:"
echo "    systemctl status mc-dashboard mc-dashboard-supervise.timer"
echo "    curl -i http://127.0.0.1:8080/healthz"
echo "    journalctl -u mc-dashboard-supervise -f"
echo "After rebooting, confirm the watchdog armed:"
echo "    journalctl -b | grep -i watchdog"

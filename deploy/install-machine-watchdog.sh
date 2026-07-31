#!/usr/bin/env bash
#
# The two protections PM2 cannot provide, because by the time they matter PM2
# isn't running either: a kernel hang and a kernel panic. Run once, with sudo:
#
#     sudo deploy/install-machine-watchdog.sh
#
# Idempotent — safe to re-run.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "needs root: sudo $0" >&2
    exit 1
fi

echo "==> hardware watchdog (BCM2835)"
install -d -m 755 /etc/systemd/system.conf.d
install -m 644 "$REPO/deploy/watchdog.conf" /etc/systemd/system.conf.d/

echo "==> reboot on kernel panic"
install -m 644 "$REPO/deploy/99-panic-reboot.conf" /etc/sysctl.d/
sysctl -q -p /etc/sysctl.d/99-panic-reboot.conf

echo
echo "Panic settings are live now. The watchdog arms on the next boot; confirm"
echo "afterwards with:"
echo "    journalctl -b | grep -i watchdog     # 'Watchdog running with a timeout of 14s'"
echo "    sysctl kernel.panic kernel.panic_on_oops"

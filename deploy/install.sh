#!/bin/bash
# Install Atrium as a system service and start it.
#
#   sudo bash deploy/install.sh
#
# Run this from the repo root. It installs the unit, enables it at boot and
# restarts it. Idempotent: safe to re-run after pulling changes.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="$REPO/deploy/atrium.service"
UNIT_DST="/etc/systemd/system/atrium.service"

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
[ -f "$REPO/config.json" ] || { echo "no config.json in $REPO" >&2; exit 1; }

echo "==> validating config"
/usr/bin/python3.12 "$REPO/atrium.py" --check

echo "==> installing unit"
install -m 644 "$UNIT_SRC" "$UNIT_DST"
systemctl daemon-reload
systemctl enable atrium.service

echo "==> restarting"
systemctl restart atrium.service
sleep 1
systemctl is-active --quiet atrium.service || {
    echo "atrium failed to start:"; journalctl -u atrium.service -n 30 --no-pager; exit 1
}

echo "==> checking :80"
code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 http://127.0.0.1/ || true)
echo "    GET / -> $code"
[ "$code" = "200" ] || { echo "board is not answering" >&2; exit 1; }

echo "atrium is live on http://$(hostname -I | awk '{print $1}')/"

#!/usr/bin/env bash
# Install or refresh the GridCase user units on the VPS. Idempotent. Touches only gridcase-* units.
#
#   deploy/install.sh          # server mode (default): server owns ingest + rechecks; timer off
#   deploy/install.sh timer    # ingest-only mode: hourly timer is the writer; no server
#
# DuckDB allows one writer process per file, so exactly one of the two is enabled.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/.." && pwd)"
dest="$HOME/.config/systemd/user"
mode="${1:-server}"
mkdir -p "$dest"
for f in gridcase-ingest.service gridcase-ingest.timer gridcase-server.service; do
  install -m 0644 "$here/$f" "$dest/$f"
done
systemctl --user daemon-reload

if [ "$mode" = "server" ]; then
  if command -v npm >/dev/null 2>&1; then
    (cd "$root/web" && npm ci --no-audit --no-fund && npm run build)
  else
    echo "npm not found; the server will run without the UI bundle" >&2
  fi
  systemctl --user disable --now gridcase-ingest.timer 2>/dev/null || true
  systemctl --user enable --now gridcase-server.service
  systemctl --user restart gridcase-server.service
  systemctl --user status gridcase-server.service --no-pager | head -5 || true
else
  systemctl --user disable --now gridcase-server.service 2>/dev/null || true
  systemctl --user enable --now gridcase-ingest.timer
  systemctl --user list-timers --no-pager | grep gridcase || true
fi

#!/usr/bin/env bash
# Install or refresh the GridCase user units on the VPS. Idempotent. Touches only gridcase-* units.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
dest="$HOME/.config/systemd/user"
mkdir -p "$dest"
for f in gridcase-ingest.service gridcase-ingest.timer; do
  install -m 0644 "$here/$f" "$dest/$f"
done
systemctl --user daemon-reload
systemctl --user enable --now gridcase-ingest.timer
systemctl --user list-timers --no-pager | grep gridcase || true

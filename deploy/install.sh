#!/usr/bin/env bash
# Install or refresh the MarketCase user units on the VPS. Idempotent; touches only gridcase-*.
#   gridcase-collector: saves every Polymarket trade (raw files only, never the database)
#   gridcase-server:    loads trades, runs the hourly watch, serves MCP + API + UI (the only
#                       process that writes the DuckDB store)
#   gridcase-tunnel:    publishes the server at kanumadhok.com/marketcase through a Cloudflare
#                       Tunnel; enabled only once `cloudflared tunnel login` has authorised it
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/.." && pwd)"
dest="$HOME/.config/systemd/user"
mkdir -p "$dest"
for f in gridcase-server.service gridcase-collector.service; do
  install -m 0644 "$here/$f" "$dest/$f"
done
# The power-grid version ran an hourly ingest timer; make sure it is gone.
systemctl --user disable --now gridcase-ingest.timer 2>/dev/null || true
rm -f "$dest/gridcase-ingest.timer" "$dest/gridcase-ingest.service"
systemctl --user daemon-reload
if command -v npm >/dev/null 2>&1; then
  (cd "$root/web" && npm ci --no-audit --no-fund && npm run build)
else
  echo "npm not found; the server will run without the UI bundle" >&2
fi
systemctl --user enable --now gridcase-collector.service
systemctl --user enable --now gridcase-server.service
systemctl --user restart gridcase-server.service
if [ -f "$HOME/.cloudflared/cert.pem" ]; then
  install -m 0644 "$here/gridcase-tunnel.service" "$dest/gridcase-tunnel.service"
  install -m 0600 "$here/cloudflared-marketcase.yml" "$HOME/.cloudflared/marketcase.yml"
  systemctl --user daemon-reload
  systemctl --user enable --now gridcase-tunnel.service
  systemctl --user restart gridcase-tunnel.service
else
  echo "Cloudflare not authorised yet (no ~/.cloudflared/cert.pem): public tunnel skipped" >&2
fi
systemctl --user status gridcase-server.service --no-pager | head -5 || true

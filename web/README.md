# GridCase web

Vite + React + TypeScript + MapLibre + Tailwind. Three hash-routed views (`src/routes.json`):
the region map (`#/`), a case (`#/case/<id>`) and the benchmark (`#/bench`). It only talks to the
JSON API under `/api/*` that `gridcase serve` exposes; the built bundle in `dist/` is served by the
same process at `/`.

```sh
npm ci
npm run dev      # proxies /api and /mcp to 127.0.0.1:8710 (run `uv run gridcase serve` first)
npx vitest run   # component and router tests
npm run build    # -> dist/, which the exit tests and the server pick up
```

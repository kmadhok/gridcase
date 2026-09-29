import { useEffect, useRef, useState } from "react";
import { api, type Region } from "../api";
import { Panel, fmt } from "../components";
import { REGION_NAMES, errorPhrase, fmtTime, hypothesisName } from "../labels";
import { navigate } from "../router";
import { regionGeoJSON } from "../data/regions";

function errorColor(pct: number | null): string {
  if (pct === null) return "#9ca3af";
  const a = Math.min(1, Math.abs(pct) / 10);
  return pct < 0 ? `rgba(180,83,9,${0.25 + 0.6 * a})` : `rgba(21,128,61,${0.25 + 0.6 * a})`;
}

export function MapView() {
  const [regions, setRegions] = useState<Region[] | null>(null);
  const [selected, setSelected] = useState<Region | null>(null);
  const [error, setError] = useState<string | null>(null);
  const mapEl = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api.map().then((m) => setRegions(m.regions)).catch((e) => setError(String(e)));
  }, []);

  useEffect(() => {
    if (!regions || !mapEl.current) return;
    let map: import("maplibre-gl").Map | undefined;
    let cancelled = false;
    // MapLibre (and its CSS) is loaded on demand so the landing page stays light. maplibre-gl 6
    // ships its worker as a separate ES module that Vite's build does not copy on its own, so the
    // worker is bundled explicitly (?worker&url) and handed to MapLibre.
    Promise.all([
      import("maplibre-gl"),
      import("maplibre-gl/dist/maplibre-gl.css"),
      import("maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url"),
    ]).then(([{ Map, setWorkerUrl }, , { default: workerUrl }]) => {
      if (cancelled || !mapEl.current) return;
      setWorkerUrl(workerUrl);
      map = new Map({
        container: mapEl.current,
        style: "https://demotiles.maplibre.org/style.json",
        center: [-96, 37.5],
        zoom: 3.2,
        attributionControl: { compact: true },
      });
      map.on("load", () => {
        if (!map) return;
        const byEntity = Object.fromEntries(regions.map((r) => [r.entity, r]));
        const gj = regionGeoJSON();
        for (const f of gj.features) {
          const r = byEntity[String(f.id)];
          f.properties = { ...f.properties, color: errorColor(r?.forecast_error_pct ?? null) };
        }
        map.addSource("regions", { type: "geojson", data: gj });
        map.addLayer({ id: "regions-fill", type: "fill", source: "regions", paint: { "fill-color": ["get", "color"], "fill-opacity": 0.85 } });
        map.addLayer({ id: "regions-line", type: "line", source: "regions", paint: { "line-color": "#1a1a1a", "line-width": 1 } });
        map.on("click", "regions-fill", (e) => {
          const id = e.features?.[0]?.properties?.entity as string | undefined;
          if (id && byEntity[id]) setSelected(byEntity[id]);
        });
        map.on("mouseenter", "regions-fill", () => { map!.getCanvas().style.cursor = "pointer"; });
        map.on("mouseleave", "regions-fill", () => { map!.getCanvas().style.cursor = ""; });
      });
    });
    return () => { cancelled = true; map?.remove(); };
  }, [regions]);

  const latest = regions?.find((r) => r.latest_hour)?.latest_hour;

  return (
    <div className="grid gap-4 md:grid-cols-[1fr_320px]">
      <div>
        <p className="muted mb-2 text-xs">Actual electricity demand vs. the operator's day-ahead forecast, latest published hour{latest ? ` (${fmtTime(latest)})` : ""}. Orange = less than forecast, green = more.</p>
        <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-4">
          {(regions ?? []).map((r) => (
            <button
              key={r.entity}
              data-testid={`region-${r.entity}`}
              onClick={() => setSelected(r)}
              className={`panel p-3 text-left hover:border-amber-600 ${selected?.entity === r.entity ? "border-amber-600" : ""}`}
            >
              <div className="text-xs muted">{REGION_NAMES[r.entity] ?? r.entity}</div>
              <div className="text-xl font-semibold" style={{ color: r.forecast_error_pct !== null && r.forecast_error_pct < -5 ? "var(--gc-accent)" : undefined }}>
                {r.forecast_error_pct === null ? "–" : `${r.forecast_error_pct > 0 ? "+" : ""}${fmt(r.forecast_error_pct, 1)}%`}
              </div>
              <div className="text-xs muted">{errorPhrase(r.forecast_error_pct)}</div>
            </button>
          ))}
        </div>
        <div ref={mapEl} className="panel h-[440px] w-full overflow-hidden" aria-label="Map of grid regions coloured by how far demand was from forecast" />
        {error && <p className="accent mt-2 text-sm">{error}</p>}
        {!regions && !error && <p className="muted mt-2 text-sm">Loading regions…</p>}
      </div>
      <aside className="space-y-4">
        <Panel title="Selected region">
          {selected ? (
            <div className="space-y-2">
              <div className="text-lg font-semibold">{REGION_NAMES[selected.entity] ?? selected.entity}</div>
              <dl className="grid grid-cols-2 gap-1 text-sm">
                <dt className="muted">Actual demand</dt><dd>{fmt(selected.demand_mw)} MW</dd>
                <dt className="muted">Forecast</dt><dd>{fmt(selected.forecast_mw)} MW</dd>
                <dt className="muted">Difference</dt><dd>{errorPhrase(selected.forecast_error_pct)}</dd>
                <dt className="muted">Investigation</dt><dd>{selected.case_id ? "yes" : "none yet"}</dd>
              </dl>
              {selected.case_id ? (
                <button data-testid="open-case" onClick={() => navigate({ view: "case", id: selected.case_id! })} className="mt-2 w-full rounded bg-amber-700 px-3 py-2 text-sm font-medium text-white hover:bg-amber-800">
                  Open the investigation → {selected.top_hypothesis ? hypothesisName(selected.top_hypothesis) : "in progress"}
                </button>
              ) : (
                <p className="muted text-sm">Nothing to investigate here yet: demand has stayed close to forecast.</p>
              )}
            </div>
          ) : (
            <p className="muted text-sm">Click a region tile or the map to see its numbers and open its investigation.</p>
          )}
        </Panel>
        <Panel title="What this is">
          <p className="text-sm">
            When a grid region uses noticeably more or less power than its operator forecast, GridCase works out why. It checks the data, tests the likely causes against the numbers, and writes a short conclusion in which every sentence links to the exact evidence behind it. Its accuracy is scored on real historical events — see the Scorecard.
          </p>
        </Panel>
      </aside>
    </div>
  );
}

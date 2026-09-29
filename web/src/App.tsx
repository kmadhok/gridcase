import { href, useRoute } from "./router";
import { MapView } from "./views/MapView";
import { CaseView } from "./views/CaseView";
import { BenchView } from "./views/BenchView";

export default function App() {
  const route = useRoute();
  const tab = (label: string, active: boolean, to: string, testid: string) => (
    <a href={to} data-testid={testid} className={`rounded px-3 py-1.5 text-sm ${active ? "bg-amber-700 text-white" : "hover:bg-zinc-200 dark:hover:bg-zinc-800"}`}>{label}</a>
  );
  return (
    <div className="mx-auto max-w-7xl px-4 py-4">
      <nav className="mb-4 flex items-center gap-2">
        <span className="mr-4 text-lg font-semibold">GridCase</span>
        {tab("Map", route.view === "map", href({ view: "map" }), "nav-map")}
        {tab("Scorecard", route.view === "bench", href({ view: "bench" }), "nav-bench")}
        <span className="muted ml-auto text-xs">When the grid misses its forecast, GridCase finds out why — and shows its work.</span>
      </nav>
      <main>
        {route.view === "map" && <MapView />}
        {route.view === "case" && <CaseView id={route.id} />}
        {route.view === "bench" && <BenchView />}
      </main>
    </div>
  );
}

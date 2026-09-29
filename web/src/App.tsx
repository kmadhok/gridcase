import { href, useRoute } from "./router";
import { NowView } from "./views/NowView";
import { CaseView } from "./views/CaseView";

export default function App() {
  const route = useRoute();
  return (
    <div className="mx-auto max-w-7xl px-4 py-4">
      <nav className="mb-4 flex items-center gap-2">
        <a href={href({ view: "now" })} className="mr-4 text-lg font-semibold" data-testid="nav-now">MarketCase</a>
        {route.view !== "now" && (
          <a href={href({ view: "now" })} className="rounded px-3 py-1.5 text-sm hover:bg-zinc-200 dark:hover:bg-zinc-800">← Now</a>
        )}
        <span className="muted ml-auto text-xs">When a Polymarket number moves, MarketCase finds out why — and shows its work.</span>
      </nav>
      <main>
        {route.view === "now" && <NowView />}
        {route.view === "case" && <CaseView id={route.id} />}
      </main>
    </div>
  );
}

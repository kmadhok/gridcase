import { href, useRoute } from "./router";
import { NowView } from "./views/NowView";
import { CaseView } from "./views/CaseView";
import { CatalogView } from "./views/CatalogView";
import { DigestView } from "./views/DigestView";
import { ExploreView } from "./views/ExploreView";

export default function App() {
  const route = useRoute();
  return (
    <div className="mx-auto max-w-7xl px-4 py-4">
      <nav className="mb-4 flex flex-wrap items-center gap-x-2 gap-y-1">
        <a href={href({ view: "now" })} className="mr-4 text-lg font-semibold" data-testid="nav-now">MarketCase</a>
        {route.view !== "now" && (
          <a href={href({ view: "now" })} className="whitespace-nowrap rounded px-3 py-1.5 text-sm hover:bg-zinc-200 dark:hover:bg-zinc-800">← Now</a>
        )}
        <a href={href({ view: "digest" })} aria-current={route.view === "digest" ? "page" : undefined} className="whitespace-nowrap rounded px-3 py-1.5 text-sm hover:bg-zinc-200 dark:hover:bg-zinc-800" data-testid="nav-digest">Morning read</a>
        <a href={href({ view: "explore", query: "" })} aria-current={route.view === "explore" ? "page" : undefined} className="whitespace-nowrap rounded px-3 py-1.5 text-sm hover:bg-zinc-200 dark:hover:bg-zinc-800" data-testid="nav-explore">Explore</a>
        <a href={href({ view: "kpis" })} aria-current={route.view === "kpis" ? "page" : undefined} className="whitespace-nowrap rounded px-3 py-1.5 text-sm hover:bg-zinc-200 dark:hover:bg-zinc-800" data-testid="nav-kpis">What each number means</a>
        <span className="muted ml-auto hidden text-xs md:inline">When a Polymarket number moves, MarketCase finds out why — and shows its work.</span>
      </nav>
      <main>
        {route.view === "now" && <NowView />}
        {route.view === "case" && <CaseView id={route.id} />}
        {route.view === "kpis" && <CatalogView />}
        {route.view === "digest" && <DigestView day={route.day} />}
        {route.view === "explore" && <ExploreView key={route.query} query={route.query} />}
      </main>
    </div>
  );
}

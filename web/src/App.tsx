import { href, useRoute } from "./router";
import { NowView } from "./views/NowView";
import { CaseView } from "./views/CaseView";
import { CatalogView } from "./views/CatalogView";
import { DigestView } from "./views/DigestView";
import { ExploreView } from "./views/ExploreView";

// Tabs: muted until current, then ink on a 2px accent underline.
const TAB = "whitespace-nowrap border-b-2 border-transparent py-2.5 transition-colors duration-150 hover:text-accent aria-[current=page]:border-accent aria-[current=page]:text-ink lg:py-1";

export default function App() {
  const route = useRoute();
  return (
    <div className="px-3 pb-16 md:px-8">
      <header className="z-30 pt-3 lg:sticky lg:top-4 lg:pt-4">
        <nav aria-label="Main" className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-1 rounded-2xl border border-ink bg-sheet px-4 lg:h-16 lg:flex-nowrap lg:px-6">
          <div className="flex h-16 min-w-0 shrink-0 items-center gap-3 lg:h-auto">
            <a href="https://www.kanumadhok.com/" className="display whitespace-nowrap text-xl transition-colors duration-150 hover:text-accent lg:text-[22px]">Kanu Madhok</a>
            <span aria-hidden="true" className="text-rule">/</span>
            <a href={href({ view: "now" })} className="whitespace-nowrap text-[15px] font-medium transition-colors duration-150 hover:text-accent" data-testid="nav-now">MarketCase</a>
          </div>
          <div className="flex min-w-0 basis-full flex-wrap items-center gap-x-5 border-t border-dashed border-rule pb-1 text-[15px] text-ink-soft lg:ml-auto lg:basis-auto lg:flex-nowrap lg:gap-x-6 lg:border-0 lg:pb-0">
            {route.view !== "now" && (
              <a href={href({ view: "now" })} className={TAB}>← Now</a>
            )}
            <a href={href({ view: "digest" })} aria-current={route.view === "digest" ? "page" : undefined} className={TAB} data-testid="nav-digest">Morning read</a>
            <a href={href({ view: "explore", query: "" })} aria-current={route.view === "explore" ? "page" : undefined} className={TAB} data-testid="nav-explore">Explore</a>
            <a href={href({ view: "kpis" })} aria-current={route.view === "kpis" ? "page" : undefined} className={TAB} data-testid="nav-kpis">What each number means</a>
          </div>
        </nav>
      </header>
      <p className="mx-auto mt-4 hidden max-w-6xl text-sm text-ink-muted md:block md:px-1">When a Polymarket number moves, MarketCase finds out why — and shows its work.</p>
      <main className="mx-auto mt-8 max-w-6xl md:mt-12">
        {route.view === "now" && <NowView />}
        {route.view === "case" && <CaseView id={route.id} />}
        {route.view === "kpis" && <CatalogView />}
        {route.view === "digest" && <DigestView day={route.day} />}
        {route.view === "explore" && <ExploreView key={route.query} query={route.query} />}
      </main>
    </div>
  );
}

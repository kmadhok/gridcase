import { useEffect, useState } from "react";
import { api, type CatalogKpi, type CatalogResponse } from "../api";
import { BASE_PATH } from "../basePath";
import { fmtTime, fmtValue } from "../labels";

const STATUS_TEXT: Record<string, string> = {
  normal: "Normal",
  alarm: "Unusual right now",
  learning: "Learning normal",
  stale: "Waiting for data",
};

function KpiCard({ k }: { k: CatalogKpi }) {
  return (
    <article className="panel p-5" data-testid={`kpi-${k.id}`} title={k.id}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="display text-xl leading-tight">{k.label}</h2>
        <span className="display tabular text-2xl leading-none">{fmtValue(k.unit, k.value)}</span>
      </div>
      <p className="mt-2 text-[15px] leading-relaxed text-ink-soft">{k.description}</p>
      <p className="mt-3 border-t border-dashed border-rule pt-3 text-sm text-ink-muted">
        <span className="font-medium text-ink-soft">How it's worked out:</span> {k.formula}
      </p>
      {k.watched && (
        <p className="mt-3 flex flex-wrap items-center gap-2 text-xs">
          <span className="pill border-ink/25 text-ink-soft">Watched every hour</span>
          {k.status && <span className={k.status === "alarm" ? "font-medium text-accent" : k.status === "normal" ? "text-ok" : "text-ink-muted"}>{STATUS_TEXT[k.status] ?? k.status}</span>}
        </p>
      )}
      <details className="mt-3 text-xs">
        <summary className="link-ink w-fit text-ink-soft">Show the exact definition</summary>
        <code data-testid="definition" className="code-block mt-2 block whitespace-pre-wrap break-words px-3 py-2">
          {k.definition}
        </code>
      </details>
    </article>
  );
}

export function CatalogView({ initial }: { initial?: CatalogResponse }) {
  const [cat, setCat] = useState<CatalogResponse | null>(initial ?? null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (initial) return;
    api.catalog().then(setCat).catch((e) => setError(String(e)));
  }, [initial]);
  if (error) return <p className="accent">{error}</p>;
  if (!cat) return <p className="text-ink-muted">Loading the KPI catalog…</p>;

  const watched = cat.kpis.filter((k) => k.watched);
  const others = cat.kpis.filter((k) => !k.watched);
  return (
    <div className="space-y-10">
      <header>
        <h1 className="display text-[40px] leading-[1.05] md:text-6xl">What each number means</h1>
        <p className="mt-4 max-w-3xl text-[15px] leading-relaxed text-ink-soft md:text-base">
          Every KPI is defined once, here, and the investigations and answers use only these definitions.
          Values are for the last complete hour ({fmtTime(cat.hour[0])}).
        </p>
      </header>
      <section aria-label="Watched every hour" className="space-y-4">
        <h2 className="display text-[28px] leading-tight md:text-[32px]">Watched every hour</h2>
        <div className="grid gap-3 md:grid-cols-2">{watched.map((k) => <KpiCard key={k.id} k={k} />)}</div>
      </section>
      <section aria-label="Available to ask about" className="space-y-4">
        <h2 className="display text-[28px] leading-tight md:text-[32px]">Available to ask about</h2>
        <div className="grid gap-3 md:grid-cols-2">{others.map((k) => <KpiCard key={k.id} k={k} />)}</div>
      </section>
      <section aria-label="Ways to slice a KPI" className="panel rounded-3xl p-5 md:p-6">
        <h2 className="label">Any KPI can be sliced by</h2>
        <dl className="mt-4 grid gap-x-6 gap-y-3 text-sm sm:grid-cols-2">
          {cat.dimensions.map((d) => (
            <div key={d.id} title={d.id}>
              <dt className="font-medium">{d.label}</dt>
              <dd className="text-ink-muted">{d.values ? d.values.join(", ") : "any market by name"}</dd>
            </div>
          ))}
        </dl>
      </section>
      <p className="text-sm text-ink-muted">
        The same KPIs are also built as daily tables in a dbt warehouse, with tests that check them against these
        definitions.{" "}
        <a className="link-ink font-medium text-ink" href={`${BASE_PATH}dbt/`}>
          See how the data is modelled →
        </a>
      </p>
    </div>
  );
}

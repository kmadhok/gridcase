import { useEffect, useState } from "react";
import { api, type CatalogKpi, type CatalogResponse } from "../api";
import { BASE_PATH } from "../basePath";
import { fmtTime, fmtValue } from "../labels";

const STATUS_TEXT: Record<string, string> = {
  normal: "Normal",
  alarm: "Unusual right now",
  learning: "Learning normal",
};

function KpiCard({ k }: { k: CatalogKpi }) {
  return (
    <article className="panel p-4" data-testid={`kpi-${k.id}`} title={k.id}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="text-base font-semibold">{k.label}</h2>
        <span className="text-lg font-semibold">{fmtValue(k.unit, k.value)}</span>
      </div>
      <p className="mt-1 text-sm">{k.description}</p>
      <p className="muted mt-2 text-sm">
        <span className="font-medium">How it's worked out:</span> {k.formula}
      </p>
      {k.watched && (
        <p className="mt-2 text-xs">
          <span className="rounded bg-zinc-200 px-2 py-0.5 dark:bg-zinc-700">Watched every hour</span>
          {k.status && <span className="muted ml-2">{STATUS_TEXT[k.status] ?? k.status}</span>}
        </p>
      )}
      <details className="mt-2 text-xs">
        <summary className="cursor-pointer text-amber-700 underline dark:text-amber-400">Show the exact definition</summary>
        <code data-testid="definition" className="mt-1 block whitespace-pre-wrap break-words rounded bg-zinc-100 p-2 dark:bg-zinc-800">
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
  if (!cat) return <p className="muted">Loading the KPI catalog…</p>;

  const watched = cat.kpis.filter((k) => k.watched);
  const others = cat.kpis.filter((k) => !k.watched);
  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold">What each number means</h1>
        <p className="muted text-sm">
          Every KPI is defined once, here, and the investigations and answers use only these definitions.
          Values are for the last complete hour ({fmtTime(cat.hour[0])}).
        </p>
      </header>
      <section aria-label="Watched every hour" className="space-y-3">
        <h2 className="text-sm font-semibold uppercase tracking-wide muted">Watched every hour</h2>
        <div className="grid gap-3 md:grid-cols-2">{watched.map((k) => <KpiCard key={k.id} k={k} />)}</div>
      </section>
      <section aria-label="Available to ask about" className="space-y-3">
        <h2 className="text-sm font-semibold uppercase tracking-wide muted">Available to ask about</h2>
        <div className="grid gap-3 md:grid-cols-2">{others.map((k) => <KpiCard key={k.id} k={k} />)}</div>
      </section>
      <section aria-label="Ways to slice a KPI" className="panel p-4">
        <h2 className="text-sm font-semibold uppercase tracking-wide muted">Any KPI can be sliced by</h2>
        <dl className="mt-2 grid gap-2 text-sm sm:grid-cols-2">
          {cat.dimensions.map((d) => (
            <div key={d.id} title={d.id}>
              <dt className="font-medium">{d.label}</dt>
              <dd className="muted">{d.values ? d.values.join(", ") : "any market by name"}</dd>
            </div>
          ))}
        </dl>
      </section>
      <p className="muted text-sm">
        The same KPIs are also built as daily tables in a dbt warehouse, with tests that check them against these
        definitions.{" "}
        <a className="underline underline-offset-2" style={{ color: "var(--gc-ink)" }} href={`${BASE_PATH}dbt/`}>
          See how the data is modelled →
        </a>
      </p>
    </div>
  );
}

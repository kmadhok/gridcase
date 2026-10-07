import { useEffect, useState } from "react";
import { api, type Definitions } from "../api";
import { BASE_PATH } from "../basePath";
import { fmtTime } from "../labels";
import { href } from "../router";

export interface Period {
  label: string;
  start: string;
  end: string;
}

export interface Calculation {
  kpis: string[]; // the KPIs this visual shows; empty when a check reads trades directly
  slice?: Record<string, string | string[]>;
  periods?: Period[];
  grain?: "hour" | "day" | "week" | "total";
  usualRange?: boolean; // the visual draws each hour's usual range
  queries: string[]; // the exact SQL behind the numbers
  queryNote?: string;
  asOf?: string | null;
  direct?: string; // what a check that reads trades directly does, in words
}

const GRAINS = { hour: "Hour by hour", day: "Day by day", week: "Week by week", total: "One total for the period" };

/** The button every visual carries. */
export function HowCalculatedButton({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  return (
    <button type="button" aria-expanded={open} onClick={onToggle} className="shrink-0 underline underline-offset-2" style={{ color: "var(--gc-ink)" }}>
      How is this calculated?
    </button>
  );
}

/** Everything behind one visual: what the KPI means, how this view slices it, where the data
 * comes from and the exact query, all read from the same definitions the server computes with. */
export function HowCalculatedPanel(c: Calculation) {
  const [defs, setDefs] = useState<Definitions | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let live = true;
    api.definitions().then((d) => live && setDefs(d)).catch(() => live && setFailed(true));
    return () => { live = false; };
  }, []);

  if (failed) return <p className="accent mt-2 text-sm">Couldn't load the definitions just now.</p>;
  if (!defs) return <p className="muted mt-2 text-sm">Loading…</p>;
  const kpis = c.kpis.map((id) => defs.kpis.find((k) => k.id === id)).filter((k): k is Definitions["kpis"][number] => !!k);
  const slice = Object.entries(c.slice ?? {}).filter(([, v]) => v && (!Array.isArray(v) || v.length));
  const rule = defs.usual_range;
  const logScale = c.kpis.some((k) => rule.log_scale.includes(k));

  return (
    <div data-testid="how-calculated" className="mt-3 space-y-4 rounded border border-[var(--gc-line)] p-4 text-sm">
      <Section title="What it measures">
        {kpis.length === 0 && <p>{c.direct ?? "This check reads trades directly, with the query below, rather than one KPI."}</p>}
        {kpis.map((k) => (
          <div key={k.id} className="space-y-1">
            <p><span className="font-medium">{k.label}</span>: {k.description}</p>
            <p className="muted text-xs">Formula: {k.formula}</p>
            <p className="muted text-xs">Defined once, in <span className="mono">ontology/kpis.yaml</span>, as <code className="mono text-[11px]">{k.definition}</code></p>
          </div>
        ))}
      </Section>

      <Section title="For this view">
        <ul className="space-y-1">
          {slice.length === 0 && <li>All markets</li>}
          {slice.map(([dim, v]) => {
            const d = defs.dimensions.find((x) => x.id === dim);
            return (
              <li key={dim}>
                <span>{d?.label ?? dim}: {Array.isArray(v) ? v.join(" or ") : v}</span>
                {d && <span className="muted block text-xs">decided by <code className="mono text-[11px]">{d.sql}</code></span>}
              </li>
            );
          })}
          {(c.periods ?? []).map((p) => <li key={p.label}>{p.label}: {fmtTime(p.start)} to {fmtTime(p.end)}</li>)}
          {c.grain && <li>{GRAINS[c.grain]}</li>}
          {c.asOf && <li>Data as our collector knew it at {fmtTime(c.asOf)}</li>}
          {c.usualRange && (
            <li>
              The grey band is each hour's usual range: the same hour on up to {rule.lookback_days} earlier days, plus or minus {rule.k_sigma} standard deviations{logScale ? " (on a log scale, so tripling and falling to a third count the same)" : ""}, once at least {rule.min_history_days} days of history exist. An hour leaves its usual range beyond that band; the watch opens an investigation after {rule.min_run} such hours in a row, or one beyond {rule.single_hour_sigma} standard deviations.
            </li>
          )}
        </ul>
      </Section>

      <Section title="Where the data comes from">
        <ul className="space-y-1.5">
          {defs.sources.map((s) => (
            <li key={s.name}>
              <span className="font-medium">{s.name}</span>
              {s.url && <> (<a href={s.url} target="_blank" rel="noreferrer" className="underline underline-offset-2">{s.url.replace(/^https:\/\//, "")}</a>)</>}: {s.how}
            </li>
          ))}
          {defs.record_start && <li className="muted text-xs">Our record begins {fmtTime(defs.record_start)}.</li>}
        </ul>
      </Section>

      <Section title={c.queries.length === 1 ? "The exact query" : `The exact queries (${c.queries.length})`}>
        {c.queryNote && <p className="muted mb-2 text-xs">{c.queryNote}</p>}
        {c.queries.map((q, i) => <Query key={i} sql={q} />)}
      </Section>

      <p className="muted text-xs">
        Every KPI on one page: <a href={href({ view: "kpis" })} className="underline underline-offset-2">What each number means</a>
        {" · "}The warehouse's lineage: <a href={`${BASE_PATH}dbt/`} className="underline underline-offset-2">dbt docs</a>
      </p>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 className="muted mb-1 text-xs font-semibold uppercase tracking-wide">{title}</h3>
      {children}
    </section>
  );
}

function Query({ sql }: { sql: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="relative mt-1">
      <pre data-testid="how-sql" className="mono max-h-72 overflow-auto rounded bg-zinc-100 p-2 pr-14 text-[11px] leading-snug dark:bg-zinc-900">{sql}</pre>
      <button
        type="button"
        onClick={() => void navigator.clipboard?.writeText(sql).then(() => setCopied(true))}
        className="absolute right-2 top-2 rounded border border-[var(--gc-line)] bg-[var(--gc-panel)] px-2 py-0.5 text-xs"
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

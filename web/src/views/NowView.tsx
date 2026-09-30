import { useEffect, useState } from "react";
import { api, type NowResponse, type SeriesView } from "../api";
import { Panel } from "../components";
import { fmtTime, fmtValue, hypothesisName, kpiLabel, segmentName } from "../labels";
import { navigate } from "../router";
import { AskPanel } from "./AskPanel";

/** 48 hours of a KPI (line) against its usual range for each hour of the day (band). */
export function Sparkline({ s }: { s: SeriesView }) {
  const w = 240;
  const h = 56;
  const vals = s.points.flatMap((p) => [p.value, p.high, p.low]).filter((v): v is number => v !== null);
  if (vals.length < 2) return null;
  const lo = Math.min(...vals);
  const hi = Math.max(...vals);
  const x = (i: number) => (i / (s.points.length - 1)) * w;
  const y = (v: number) => h - 4 - ((v - lo) / (hi - lo || 1)) * (h - 8);
  const band = s.points
    .map((p, i) => (p.low !== null && p.high !== null ? { i, low: p.low, high: p.high } : null))
    .filter((b): b is { i: number; low: number; high: number } => b !== null);
  const bandPath = band.length > 1
    ? `M ${band.map((b) => `${x(b.i)},${y(b.high)}`).join(" L ")} L ${[...band].reverse().map((b) => `${x(b.i)},${y(b.low)}`).join(" L ")} Z`
    : "";
  const line = s.points
    .map((p, i) => (p.value === null ? null : `${x(i)},${y(p.value)}`))
    .filter(Boolean)
    .join(" L ");
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="h-14 w-full" role="img" aria-label={`${s.label} over the last 48 hours against its usual range`}>
      {bandPath && <path d={bandPath} className="fill-zinc-300/60 dark:fill-zinc-600/40" />}
      {line && <path d={`M ${line}`} fill="none" strokeWidth={1.8} className={s.status === "alarm" ? "stroke-amber-600" : "stroke-zinc-800 dark:stroke-zinc-200"} />}
    </svg>
  );
}

function statusText(s: SeriesView): { text: string; cls: string } {
  if (s.status === "alarm") {
    const times = s.ratio ? `${s.ratio.toFixed(1)}×` : "";
    return { text: `Unusual ${s.direction === "down" ? "↓" : "↑"} ${times} usual`, cls: "bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200" };
  }
  if (s.status === "learning") return { text: `Learning normal · day ${s.history_days} of 3`, cls: "bg-zinc-200 text-zinc-800 dark:bg-zinc-700 dark:text-zinc-200" };
  return { text: "Normal", cls: "bg-emerald-100 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-200" };
}

function Tile({ s, caseId }: { s: SeriesView; caseId?: string }) {
  const st = statusText(s);
  const title = s.segment === "all" ? s.label : `${segmentName(s.segment)} volume`;
  return (
    <div className={`panel p-3 ${s.status === "alarm" ? "border-amber-600" : ""}`} data-testid={`tile-${s.series}`} title={s.series}>
      <div className="text-sm font-medium">{title}</div>
      <span className={`mt-1 inline-block rounded px-2 py-0.5 text-xs ${st.cls}`}>{st.text}</span>
      {s.latest ? (
        <div className="mt-1 flex flex-wrap items-baseline gap-x-2">
          <span className="text-xl font-semibold">{fmtValue(s.unit, s.latest.value)}</span>
          <span className="muted text-xs">last hour{s.latest.usual !== null ? ` · usually ${fmtValue(s.unit, s.latest.usual)}` : ""}</span>
        </div>
      ) : (
        <p className="muted mt-1 text-xs">Waiting for the first complete hour.</p>
      )}
      <Sparkline s={s} />
      {caseId && (
        <button data-testid="open-case" onClick={() => navigate({ view: "case", id: caseId })} className="mt-1 text-xs font-medium text-amber-700 underline dark:text-amber-400">
          See why →
        </button>
      )}
    </div>
  );
}

export function NowView({ initial }: { initial?: NowResponse }) {
  const [now, setNow] = useState<NowResponse | null>(initial ?? null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (initial) return;
    const load = () => api.now().then(setNow).catch((e) => setError(String(e)));
    load();
    const t = setInterval(load, 5 * 60 * 1000);
    return () => clearInterval(t);
  }, [initial]);
  if (error) return <p className="accent">{error}</p>;
  if (!now) return <p className="muted">Loading live Polymarket data…</p>;

  const caseFor = (s: SeriesView) => now.cases.find((c) => c.metric === s.kpi && c.segment === s.segment && c.state !== "closed")?.case_id;
  const groupOf = (s: SeriesView) => s.group ?? (s.segment === "all" ? "team" : "category");
  const team = now.series.filter((s) => groupOf(s) === "team");
  const behaviour = now.series.filter((s) => groupOf(s) === "behaviour");
  const categories = now.series.filter((s) => groupOf(s) === "category");
  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-xl font-semibold">Polymarket, right now</h1>
        <p className="muted text-sm">
          Each hour is compared with the same hour on previous days; when a number leaves its usual range, an investigation opens automatically and explains why.
          {" "}Newest trade {fmtTime(now.freshness.newest_trade)}{now.freshness.record_starts ? ` · collecting since ${fmtTime(now.freshness.record_starts)}` : ""}.
        </p>
      </header>
      <section aria-label="Team KPIs" className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        {team.map((s) => <Tile key={s.series} s={s} caseId={caseFor(s)} />)}
      </section>
      {behaviour.length > 0 && (
        <>
          <h2 className="text-sm font-semibold uppercase tracking-wide muted">How the market behaves</h2>
          <section aria-label="How the market behaves" className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
            {behaviour.map((s) => <Tile key={s.series} s={s} caseId={caseFor(s)} />)}
          </section>
        </>
      )}
      <h2 className="text-sm font-semibold uppercase tracking-wide muted">Volume by category</h2>
      <section aria-label="Volume by category" className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {categories.map((s) => <Tile key={s.series} s={s} caseId={caseFor(s)} />)}
      </section>
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <Panel title="Latest investigations" hint="Opened automatically when a KPI leaves its usual range, or by asking.">
          {now.cases.length === 0 ? (
            <p className="muted text-sm">None yet. The first can open once three days of history exist.</p>
          ) : (
            <ul className="space-y-2" data-testid="cases">
              {now.cases.map((c) => (
                <li key={c.case_id}>
                  <button className="w-full text-left hover:underline" onClick={() => navigate({ view: "case", id: c.case_id })}>
                    <span className="font-medium">{kpiLabel(c.metric)}</span>{" "}
                    <span className="muted">· {segmentName(c.segment)} · {fmtTime(c.as_of)}</span>
                    <span className="block text-sm">{c.top_hypothesis ? `Best explanation: ${hypothesisName(c.top_hypothesis).toLowerCase()}` : "investigating…"}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Panel>
        <AskPanel target={{ page: "now" }} receipts={{}} />
      </div>
    </div>
  );
}

import { useEffect, useState } from "react";
import { api, type NowResponse, type SeriesView } from "../api";
import { bandSegments, chartSegments, formatChartTicks } from "../chart";
import { Panel } from "../components";
import { fmtTime, fmtValue, hypothesisName, kpiLabel, segmentName } from "../labels";
import { statusText } from "../nowStatus";
import { navigate } from "../router";
import { AskPanel } from "./AskPanel";

/** 48 hours of a KPI (line) against its usual range for each hour of the day (band). */
export function Sparkline({ s }: { s: SeriesView }) {
  const [active, setActive] = useState<number | null>(null);
  const first = s.points.findIndex((p) => p.value !== null);
  const pts = first < 0 ? [] : s.points.slice(first);
  const vals = pts.flatMap((p) => [p.value, p.high, p.low]).filter((v): v is number => v !== null);
  if (!vals.length) return null;
  const lo = Math.min(...vals);
  const hi = Math.max(...vals);
  const x = (i: number) => pts.length > 1 ? 4 + i / (pts.length - 1) * 232 : 120;
  const y = (v: number) => 52 - (v - lo) / (hi - lo || 1) * 32;
  const runs = chartSegments(pts, (p) => p.value !== null);
  const color = s.status === "alarm" ? "var(--gc-accent)" : "var(--gc-ink)";
  const hovered = active === null ? null : pts[active];
  const bounds = lo === hi ? [fmtValue(s.unit, lo), fmtValue(s.unit, hi)] : formatChartTicks(s.unit, [lo, hi]);
  const readout = (i: number) => {
    const p = pts[i];
    return `${fmtTime(p.t)} · ${fmtValue(s.unit, p.value)} · ${p.low !== null && p.high !== null ? `usual range ${fmtValue(s.unit, p.low)}–${fmtValue(s.unit, p.high)}` : "No usual range yet"}`;
  };
  return (
    <div className="relative">
      <svg viewBox="0 0 240 60" className="h-[60px] w-full" role="img" aria-label={`${s.label} over the last 48 hours against its usual range`}>
        <text x="4" y="10" fontSize="10" fill="var(--gc-muted)">Min {bounds[0]}</text>
        <text x="236" y="10" textAnchor="end" fontSize="10" fill="var(--gc-muted)">Max {bounds[1]}</text>
        {bandSegments(pts).filter((run) => run.length > 1).map((run) => <path key={run[0]} d={`M ${run.map((i) => `${x(i)},${y(pts[i].high!)}`).join(" L ")} L ${[...run].reverse().map((i) => `${x(i)},${y(pts[i].low!)}`).join(" L ")} Z`} fill="var(--gc-band)" />)}
        {runs.map((run) => run.length === 1
          ? <circle data-testid="isolated-point" key={run[0]} cx={x(run[0])} cy={y(pts[run[0]].value!)} r="2.5" fill={color} />
          : <path data-testid="value-segment" key={run[0]} d={`M ${run.map((i) => `${x(i)},${y(pts[i].value!)}`).join(" L ")}`} fill="none" strokeWidth="1.8" stroke={color} />)}
        {pts.map((p, i) => <rect key={p.t} x={x(i) - (pts.length > 1 ? 116 / (pts.length - 1) : 116)} y="16" width={pts.length > 1 ? 232 / (pts.length - 1) : 232} height="42" fill="transparent" tabIndex={0} role="button" aria-label={readout(i)} onFocus={() => setActive(i)} onBlur={() => setActive(null)} onPointerEnter={() => setActive(i)} onPointerLeave={() => setActive(null)}><title>{readout(i)}</title></rect>)}
      </svg>
      <div className="flex justify-between gap-2 text-[9px] text-ink-muted"><span>{fmtTime(pts[0].t)}</span><span>{fmtTime(pts[pts.length - 1].t)}</span></div>
      {hovered && active !== null && <div className="pointer-events-none absolute bottom-full left-0 z-10 rounded-lg border border-rule bg-sheet p-2 text-xs" role="status">{readout(active)}</div>}
    </div>
  );
}


function Tile({ s, caseId, minimum }: { s: SeriesView; caseId?: string; minimum: number | null }) {
  const st = statusText(s, minimum);
  const title = s.segment === "all" ? s.label : `${segmentName(s.segment)} volume`;
  return (
    <div className={`panel flex flex-col p-4 ${s.status === "alarm" ? "bg-blush" : ""}`} data-testid={`tile-${s.series}`} title={s.series}>
      <div className="text-[15px] font-medium leading-snug tracking-[-0.01em]">{title}</div>
      <span className={`pill mt-2 self-start ${st.cls}`}>{st.text}</span>
      {s.latest ? (
        <div className="mt-3 flex flex-wrap items-baseline gap-x-2">
          <span className="display tabular text-[28px] leading-none">{fmtValue(s.unit, s.latest.value)}</span>
          <span className={`text-xs ${s.status === "alarm" ? "text-ink-soft" : "text-ink-muted"}`}>last hour{s.latest.usual !== null ? ` · usually ${fmtValue(s.unit, s.latest.usual)}` : ""}</span>
        </div>
      ) : (
        <p className="mt-3 text-xs text-ink-muted">Waiting for the first complete hour.</p>
      )}
      <div className="mt-auto pt-3"><Sparkline s={s} /></div>
      {caseId && (
        <button data-testid="open-case" onClick={() => navigate({ view: "case", id: caseId })} className="link-ink mt-2 self-start text-[13px] font-medium text-accent-hi">
          See why →
        </button>
      )}
    </div>
  );
}

export function NowView({ initial }: { initial?: NowResponse }) {
  const [minimum, setMinimum] = useState<number | null>(null);
  useEffect(() => {
    let live = true;
    api.definitions().then((d) => { if (live) setMinimum(d.usual_range.min_history_days); }).catch(() => {});
    return () => { live = false; };
  }, []);
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
    <div className="space-y-6">
      <header className="pb-2">
        <h1 className="display text-[40px] leading-[1.05] md:text-6xl">Polymarket, right now</h1>
        <p className="mt-4 max-w-3xl text-[15px] leading-relaxed text-ink-soft md:text-base">
          Each hour is compared with the same hour on previous days; when a number leaves its usual range, an investigation opens automatically and explains why.
          {" "}Newest trade {fmtTime(now.freshness.newest_trade)}{now.freshness.record_starts ? ` · collecting since ${fmtTime(now.freshness.record_starts)}` : ""}.
        </p>
      </header>
      <section aria-label="Team KPIs" className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        {team.map((s) => <Tile key={s.series} minimum={minimum} s={s} caseId={caseFor(s)} />)}
      </section>
      {behaviour.length > 0 && (
        <>
          <h2 className="display pt-6 text-[28px] leading-tight md:text-[32px]">How the market behaves</h2>
          <section aria-label="How the market behaves" className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
            {behaviour.map((s) => <Tile key={s.series} minimum={minimum} s={s} caseId={caseFor(s)} />)}
          </section>
        </>
      )}
      <h2 className="display pt-6 text-[28px] leading-tight md:text-[32px]">Volume by category</h2>
      <section aria-label="Volume by category" className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {categories.map((s) => <Tile key={s.series} minimum={minimum} s={s} caseId={caseFor(s)} />)}
      </section>
      <div className="grid gap-4 pt-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <Panel title="Latest investigations" hint="Opened automatically when a KPI leaves its usual range, or by asking.">
          {now.cases.length === 0 ? (
            <p className="text-sm text-ink-muted">None yet. The first can open once three days of history exist.</p>
          ) : (
            <ul className="divide-y divide-dashed divide-rule" data-testid="cases">
              {now.cases.map((c) => (
                <li key={c.case_id}>
                  <button className="group w-full cursor-pointer py-3 text-left" onClick={() => navigate({ view: "case", id: c.case_id })}>
                    <span className="font-medium transition-colors duration-150 group-hover:text-accent">{kpiLabel(c.metric)}</span>{" "}
                    <span className="text-ink-muted">· {segmentName(c.segment)} · {fmtTime(c.as_of)}</span>
                    <span className="mt-0.5 block text-sm text-ink-soft">{c.top_hypothesis ? `Best explanation: ${hypothesisName(c.top_hypothesis).toLowerCase()}` : "investigating…"}</span>
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

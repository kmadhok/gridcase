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
      {bandPath && <path d={bandPath} fill="var(--gc-band)" />}
      {line && <path d={`M ${line}`} fill="none" strokeWidth={1.8} stroke={s.status === "alarm" ? "var(--gc-accent)" : "var(--gc-ink)"} />}
    </svg>
  );
}

function statusText(s: SeriesView): { text: string; cls: string } {
  if (s.status === "alarm") {
    const times = s.ratio ? `${s.ratio.toFixed(1)}×` : "";
    return { text: `Unusual ${s.direction === "down" ? "↓" : "↑"} ${times} usual`, cls: "border-current text-accent-hi" }; // on the blush alarm tile, accent-hi keeps 4.5:1
  }
  if (s.status === "stale") return { text: "Waiting for data", cls: "border-ink/25 text-ink-muted" };
  if (s.status === "learning") return { text: `Learning normal · day ${s.history_days} of 3`, cls: "border-ink/25 text-ink-muted" };
  return { text: "Normal", cls: "border-current text-ok" };
}

function Tile({ s, caseId }: { s: SeriesView; caseId?: string }) {
  const st = statusText(s);
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
        {team.map((s) => <Tile key={s.series} s={s} caseId={caseFor(s)} />)}
      </section>
      {behaviour.length > 0 && (
        <>
          <h2 className="display pt-6 text-[28px] leading-tight md:text-[32px]">How the market behaves</h2>
          <section aria-label="How the market behaves" className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
            {behaviour.map((s) => <Tile key={s.series} s={s} caseId={caseFor(s)} />)}
          </section>
        </>
      )}
      <h2 className="display pt-6 text-[28px] leading-tight md:text-[32px]">Volume by category</h2>
      <section aria-label="Volume by category" className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {categories.map((s) => <Tile key={s.series} s={s} caseId={caseFor(s)} />)}
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

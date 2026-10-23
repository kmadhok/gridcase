import { bandSegments } from "../chart";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, type CaseFull, type CatalogResponse, type SeriesView } from "../api";
import { HypothesisName, Panel, TierBadge } from "../components";
import {
  RANGES, SLICE_DIMS, describeHour, filterList, flaggedHours, judged, parseState, segmentOf, stateToQuery, suggestLine,
  type ExploreState, type Mode,
} from "../explore";
import { CASE_STATES, KPIS, fmtTime, fmtValue, kpiLabel } from "../labels";
import { href } from "../router";
import { ExploreChart } from "./ExploreChart";
import { ExploreChat } from "./ExploreChat";
import { HowCalculatedButton, HowCalculatedPanel } from "./HowCalculated";

const DIM_LABELS: Record<string, string> = { category: "Category", size_band: "Trade size", trader_type: "Trader" };
const ANY: Record<string, string> = { category: "All markets", size_band: "Any size", trader_type: "Anyone" };
const VALUE_LABELS: Record<string, string> = { new: "New to our record", returning: "Returning" };
const RUNNING = ["detected", "planned", "executing"];
const STEPS = [
  { state: "detected", label: "Opened the investigation" },
  { state: "planned", label: "Planned the checks" },
  { state: "executing", label: "Ran the checks and wrote the answer" },
];

interface Investigation {
  hour: string;
  phase: "starting" | "running" | "done" | "refused" | "failed";
  message?: string;
  caseId?: string;
  data?: CaseFull;
  paid?: boolean;
}

function sliceName(filters: Record<string, string>): string {
  const parts = SLICE_DIMS.map((d) => filters[d]).filter(Boolean).map((v) => VALUE_LABELS[v] ?? v);
  return parts.length ? parts.join(" · ") : "All markets";
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const hourAfter = (t: string) => new Date(new Date(`${t}Z`).getTime() + 3_600_000).toISOString().slice(0, 19);

export function ExploreView({ query = "", pollMs = 1500 }: { query?: string; pollMs?: number }) {
  const [state, setState] = useState<ExploreState>(() => parseState(query));
  const [catalog, setCatalog] = useState<CatalogResponse | null>(null);
  const [chart, setChart] = useState<SeriesView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [inv, setInv] = useState<Investigation | null>(null);
  const [how, setHow] = useState(false);
  const invToken = useRef(0);
  const invPanel = useRef<HTMLElement>(null);
  const segment = segmentOf(state.filters);

  useEffect(() => {
    api.catalog().then(setCatalog).catch(() => setCatalog(null));
  }, []);

  useEffect(() => {
    let live = true;
    api.exploreSeries(state.kpi, segment, state.hours)
      .then((v) => {
        if (!live) return;
        setChart(v);
        setError(null);
      })
      .catch((e) => live && setError(e instanceof Error && /429/.test(e.message) ? "Too many chart requests; please wait a few minutes." : "Couldn't load this chart just now."));
    return () => { live = false; };
  }, [state.kpi, segment, state.hours]);

  useEffect(() => {
    window.history.replaceState(null, "", href({ view: "explore", query: stateToQuery(state) }));
  }, [state]);

  useEffect(() => () => { invToken.current += 1; }, []); // stop polling when the page closes

  const update = (patch: Partial<ExploreState>) => setState((s) => ({ ...s, ...patch }));
  const points = useMemo(() => chart?.points ?? [], [chart]);
  const flagged = useMemo(() => flaggedHours(points, state), [points, state]);
  const flaggedSet = useMemo(() => new Set(flagged.map((p) => p.t)), [flagged]);
  const unit = chart?.unit ?? KPIS[state.kpi]?.unit ?? "count";
  const rangeWords = RANGES.find((r) => r.hours === state.hours)?.label ?? "";
  const context = `${kpiLabel(state.kpi)} · ${sliceName(state.filters)} · last ${rangeWords}`;
  const dims = (catalog?.dimensions ?? []).filter((d) => (SLICE_DIMS as readonly string[]).includes(d.id) && d.values);
  const selectedPoint = points.find((p) => p.t === selected) ?? null;
  const peak = points.reduce<(typeof points)[number] | null>((m, p) => (p.value !== null && (m === null || p.value > (m.value ?? 0)) ? p : m), null);
  const suggestions = [
    `Which ${state.filters.category ?? "Polymarket"} markets traded the most in this window?`,
    peak ? `Were $1k+ trades behind the peak on ${fmtTime(peak.t).replace(" UTC", "")}?` : "Were $1k+ trades a big part of this?",
    `How does this compare with ${state.filters.category === "Politics" ? "Sports" : "Politics"}?`,
  ];

  async function investigate(hour: string) {
    const token = ++invToken.current;
    const alive = () => token === invToken.current;
    setSelected(hour);
    setInv({ hour, phase: "starting" });
    invPanel.current?.scrollIntoView?.({ behavior: "smooth", block: "start" });
    try {
      const start = await api.exploreInvestigate({ kpi: state.kpi, filters: filterList(state.filters), hour });
      let caseId = "case_id" in start ? start.case_id : null;
      const paid = "paid" in start ? start.paid : undefined;
      while (!caseId && "task_id" in start && alive()) {
        await sleep(pollMs);
        const t = await api.exploreTask(start.task_id);
        if (t.status === "failed") throw new Error(t.error ?? "The investigation failed; please try again.");
        caseId = t.case_id;
      }
      while (caseId && alive()) {
        const data = await api.case(caseId);
        const done = !RUNNING.includes(data.state);
        if (alive()) setInv({ hour, phase: done ? "done" : "running", caseId, data, paid });
        if (done) return;
        await sleep(pollMs);
      }
    } catch (e) {
      if (!alive()) return;
      const message = e instanceof Error ? e.message : String(e);
      setInv({ hour, phase: /failed/i.test(message) ? "failed" : "refused", message });
    }
  }

  const lineInput = (which: "ceiling" | "floor") => (
    <label className="min-w-0 text-xs text-ink-muted">
      {which === "ceiling" ? "Ceiling" : "Floor"}
      <div className="mt-1.5 flex gap-2">
        <input
          type="number"
          min={0}
          value={state[which] ?? ""}
          onChange={(e) => update({ [which]: e.target.value === "" ? null : Number(e.target.value) })}
          className="field w-full min-w-0 px-3 py-2 text-sm"
        />
        {state[which] === null && points.length > 0 && (
          <button type="button" onClick={() => update({ [which]: suggestLine(points, which), mode: "lines" })} className="btn btn-sm shrink-0 px-3">
            Add
          </button>
        )}
      </div>
    </label>
  );

  return (
    <div className="space-y-4">
      <header className="pb-4">
        <h1 className="display text-[40px] leading-[1.05] md:text-6xl">Explore</h1>
        <p className="mt-4 max-w-3xl text-[15px] leading-relaxed text-ink-soft md:text-base">
          Pick a number and a slice of the market, draw your own lines, and ask why any hour moved. The chart is free and instant; “Why did this happen?” runs a full investigation of that hour.
        </p>
      </header>

      <section className="panel rounded-3xl p-5 md:p-6" aria-label="What to look at">
        <div className="grid gap-6 sm:grid-cols-2 lg:grid-cols-[minmax(0,1.2fr)_minmax(0,2fr)_minmax(0,1.4fr)]">
          <div className="min-w-0">
            <h2 className="label mb-2">Number</h2>
            <select aria-label="Number" value={state.kpi} onChange={(e) => update({ kpi: e.target.value, floor: null, ceiling: null, mode: "usual" })} className="field w-full cursor-pointer px-3 py-2 text-sm">
              {Object.entries(KPIS).map(([id, k]) => <option key={id} value={id}>{k.label}</option>)}
            </select>
            {catalog && <p className="mt-2.5 text-xs leading-snug text-ink-muted">{catalog.kpis.find((k) => k.id === state.kpi)?.description}</p>}
          </div>
          <div className="min-w-0">
            <h2 className="label mb-2">Slice</h2>
            {!catalog && <p className="text-sm text-ink-muted">Loading…</p>}
            <div className="grid grid-cols-3 gap-2">
              {dims.map((d) => (
                <label key={d.id} className="min-w-0 text-xs text-ink-muted">
                  {DIM_LABELS[d.id] ?? d.label}
                  <select
                    value={state.filters[d.id] ?? ""}
                    onChange={(e) => {
                      const filters = { ...state.filters };
                      if (e.target.value) filters[d.id] = e.target.value;
                      else delete filters[d.id];
                      update({ filters, floor: null, ceiling: null, mode: "usual" });
                    }}
                    className="field mt-1.5 w-full cursor-pointer px-3 py-2 text-sm"
                  >
                    <option value="">{ANY[d.id] ?? "Any"}</option>
                    {(d.values ?? []).map((v) => <option key={v} value={v}>{VALUE_LABELS[v] ?? v}</option>)}
                  </select>
                </label>
              ))}
            </div>
          </div>
          <div className="min-w-0">
            <h2 className="label mb-2">Your lines</h2>
            <div className="grid grid-cols-2 gap-2">
              {lineInput("ceiling")}
              {lineInput("floor")}
            </div>
            <p className="mt-2.5 text-xs leading-snug text-ink-muted">Or drag them on the chart. They're kept in this page's link, so you can share it.</p>
          </div>
        </div>
      </section>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1.75fr)_minmax(0,1fr)]">
        <div className="min-w-0 space-y-4">
          <section className="panel rounded-3xl p-5 md:p-6" aria-label="Chart">
            <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-2">
              <h2 className="display min-w-0 text-2xl leading-tight">{kpiLabel(state.kpi)} · {sliceName(state.filters)}</h2>
              <Toggle label="Time range" className="ml-auto" options={RANGES.map((r) => ({ value: String(r.hours), label: r.label }))} value={String(state.hours)} onChange={(v) => update({ hours: Number(v) as ExploreState["hours"] })} />
            </div>
            {error && <p className="accent text-sm">{error}</p>}
            {!chart && !error && <p className="text-sm text-ink-muted">Loading…</p>}
            {chart && (
              <ExploreChart view={chart} state={state} flagged={flaggedSet} selected={selected} onSelect={setSelected} onLine={(which, v) => update({ [which]: v, mode: "lines" })} />
            )}
            <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs text-ink-muted">
              {bandSegments(points).some((run) => run.length > 1) && <span><span className="mr-1.5 inline-block h-2.5 w-4 rounded-sm bg-[var(--gc-band)] align-middle" />usual range for that hour of the day</span>}
              <span><span className="mr-1.5 inline-block w-4 border-t-2 border-dashed border-accent align-middle" />your lines</span>
              <span><span className="mr-1.5 inline-block h-2.5 w-2.5 rounded-full bg-accent align-middle" />flagged hour</span>
              {points.some((p) => p.value === null) && (
                <span><span className="mr-1.5 inline-block h-2.5 w-2 bg-[var(--gc-mark)] align-middle" />no data (before our record began, or a collector gap)</span>
              )}
              <span>click any hour to look closer</span>
              <span className="ml-auto"><HowCalculatedButton open={how} onToggle={() => setHow((o) => !o)} /></span>
            </div>
            {how && chart && points.length > 0 && (
              <HowCalculatedPanel
                kpis={[state.kpi]}
                slice={state.filters}
                periods={[{ label: "Shown", start: points[0].t, end: hourAfter(points[points.length - 1].t) }]}
                grain="hour"
                usualRange
                asOf={chart.as_of}
                queries={chart.sql ? [chart.sql] : []}
                queryNote="It covers the hours shown and the earlier days their usual range is compared with; re-running it on our store reproduces every hour on the chart."
              />
            )}
            {selectedPoint && chart && (
              <div className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-3 rounded-xl border border-ink bg-blush p-3.5 text-sm">
                <div className="min-w-0"><span className="font-medium">{fmtTime(selectedPoint.t)}</span> <span className="text-ink-soft">· {describeHour(selectedPoint, unit)}</span></div>
                {judged(selectedPoint)
                  ? <button type="button" onClick={() => void investigate(selectedPoint.t)} className="btn btn-sm btn-accent ml-auto">Why did this happen?</button>
                  : <span className="ml-auto text-xs text-ink-soft">This hour can't be judged.</span>}
              </div>
            )}
          </section>

          <section className="panel rounded-3xl p-5 md:p-6" aria-label="Flagged hours">
            <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-2">
              <h2 className="label">Flagged hours</h2>
              <Toggle label="Flag hours that" className="ml-auto" options={[{ value: "lines", label: "Cross my lines" }, { value: "usual", label: "Leave the usual range" }]} value={state.mode} onChange={(v) => update({ mode: v as Mode })} />
            </div>
            <ul data-testid="flagged" className="divide-y divide-dashed divide-rule">
              {[...flagged].reverse().slice(0, 8).map((p) => (
                <li key={p.t} className="flex flex-wrap items-center gap-x-3 gap-y-2 py-3">
                  <button type="button" className="group min-w-0 cursor-pointer text-left" onClick={() => setSelected(p.t)}>
                    <span className="text-sm font-medium transition-colors duration-150 group-hover:text-accent">{fmtTime(p.t)}</span>
                    <span className="tabular block text-xs text-ink-muted">{reason(p, state, unit)}</span>
                  </button>
                  <span className="ml-auto">
                    {judged(p)
                      ? <button type="button" onClick={() => void investigate(p.t)} className="btn btn-sm">Why did this happen?</button>
                      : <span className="text-xs text-ink-muted">too early to judge</span>}
                  </span>
                </li>
              ))}
            </ul>
            {flagged.length === 0 && chart && <p className="py-2 text-sm text-ink-muted">{emptyText(state, points.some((p) => p.value !== null && p.low !== null && p.high !== null))}</p>}
            {flagged.length > 8 && <p className="text-xs text-ink-muted">and {flagged.length - 8} more</p>}
            <p className="mt-4 border-t border-dashed border-rule pt-3 text-xs leading-snug text-ink-muted">An investigation takes about 40 seconds. Visitors can start 10 model-written ones a day; after that the standard playbook answers for free.</p>
          </section>
        </div>

        <ExploreChat state={state} context={context} flagged={flagged.map((p) => p.t)} suggestions={suggestions} onInvestigate={(h) => void investigate(h)} />
      </div>

      <section ref={invPanel} aria-live="polite" aria-label="Investigation">
        {inv && <InvestigationPanel inv={inv} label={`${kpiLabel(state.kpi)} · ${sliceName(state.filters)}`} />}
      </section>
    </div>
  );
}

function reason(p: { value: number | null; low: number | null; high: number | null; usual: number | null }, s: ExploreState, unit: string): string {
  const v = p.value as number;
  if (s.mode === "lines") {
    if (s.ceiling !== null && v > s.ceiling) return `${fmtValue(unit, v)} · above your ceiling (${fmtValue(unit, s.ceiling)})`;
    if (s.floor !== null) return `${fmtValue(unit, v)} · below your floor (${fmtValue(unit, s.floor)})`;
  }
  if (p.usual !== null && p.high !== null && v > p.high) return `${fmtValue(unit, v)} · ${(v / p.usual).toFixed(1)}× its usual ${fmtValue(unit, p.usual)}`;
  return `${fmtValue(unit, v)} · below its usual ${fmtValue(unit, p.usual)}`;
}

function emptyText(s: ExploreState, hasRange: boolean): string {
  if (s.mode === "usual") return hasRange ? "No hour in this window left its usual range." : "No hour in this window has a usual range yet.";
  if (s.ceiling === null && s.floor === null) return "Add a ceiling or a floor above to see which hours cross it.";
  return "No hour in this window crossed your lines. Drag them closer to see what would have been flagged.";
}

function Toggle({ label, options, value, onChange, className = "" }: { label: string; options: { value: string; label: string }[]; value: string; onChange: (v: string) => void; className?: string }) {
  return (
    <div role="group" aria-label={label} className={`flex flex-wrap gap-x-4 gap-y-1 text-[13px] ${className}`}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          aria-pressed={o.value === value}
          onClick={() => onChange(o.value)}
          className={`cursor-pointer whitespace-nowrap border-b-2 py-1 transition-colors duration-150 ${o.value === value ? "border-accent font-medium text-ink" : "border-transparent text-ink-muted hover:text-accent"}`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function InvestigationPanel({ inv, label }: { inv: Investigation; label: string }) {
  const heading = <h2 className="display mb-3 text-2xl leading-tight">Why did {label} move at {fmtTime(inv.hour)}?</h2>;
  if (inv.phase === "refused" || inv.phase === "failed") {
    return <Panel>{heading}<p className="text-sm">{inv.message}</p></Panel>;
  }
  if (inv.phase !== "done" || !inv.data) {
    const at = inv.data ? STEPS.findIndex((s) => s.state === inv.data?.state) : -1;
    return (
      <Panel>
        {heading}
        <p className="mb-4 text-xs text-ink-muted">Investigating… this usually takes about 40 seconds.</p>
        <ol className="space-y-1.5 text-sm">
          {STEPS.map((s, i) => (
            <li key={s.state} className={`flex items-center gap-2 ${i > at ? "text-ink-muted" : "text-ink-soft"}`}>
              <span className={`w-4 text-center ${i < at ? "text-ok" : ""}`} aria-hidden="true">{i < at ? "✓" : i === at ? "…" : "○"}</span>
              {s.label}
            </li>
          ))}
        </ol>
      </Panel>
    );
  }
  const c = inv.data;
  const claims = c.conclusion?.claims.slice(0, 4) ?? [];
  return (
    <Panel>
      {heading}
      <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
        <div className="min-w-0">
          <p className="label">Best explanation</p>
          <p className="display mb-2 mt-1 text-2xl leading-tight">{c.top_hypothesis ? <HypothesisName id={c.top_hypothesis} /> : "Nothing stood out"}</p>
          <p className="text-xs text-ink-muted">{CASE_STATES[c.state] ?? c.state}{inv.paid === false ? " · answered by the standard playbook (today's model-written investigations are used up)" : ""}</p>
        </div>
        <ul className="min-w-0 space-y-1">
          {claims.map((cl, i) => (
            <li key={i} className="flex items-start gap-2 text-sm"><TierBadge tier={cl.tier} /><span className="min-w-0 flex-1 leading-relaxed text-ink-soft">{cl.text}</span></li>
          ))}
        </ul>
      </div>
      <a href={href({ view: "case", id: c.case_id })} className="link-ink mt-4 inline-block text-sm font-medium text-ink">
        Open the full case, with its evidence and step-by-step trace →
      </a>
    </Panel>
  );
}

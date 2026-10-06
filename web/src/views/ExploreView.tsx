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

export function ExploreView({ query = "", pollMs = 1500 }: { query?: string; pollMs?: number }) {
  const [state, setState] = useState<ExploreState>(() => parseState(query));
  const [catalog, setCatalog] = useState<CatalogResponse | null>(null);
  const [chart, setChart] = useState<SeriesView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [inv, setInv] = useState<Investigation | null>(null);
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
    <label className="min-w-0 text-xs muted">
      {which === "ceiling" ? "Ceiling" : "Floor"}
      <div className="mt-1 flex gap-1">
        <input
          type="number"
          min={0}
          value={state[which] ?? ""}
          onChange={(e) => update({ [which]: e.target.value === "" ? null : Number(e.target.value) })}
          className="panel w-full min-w-0 p-2 text-sm"
        />
        {state[which] === null && points.length > 0 && (
          <button type="button" onClick={() => update({ [which]: suggestLine(points, which), mode: "lines" })} className="shrink-0 rounded border border-[var(--gc-line)] px-2 text-xs hover:border-amber-600">
            Add
          </button>
        )}
      </div>
    </label>
  );

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-xl font-semibold">Explore</h1>
        <p className="muted text-sm">
          Pick a number and a slice of the market, draw your own lines, and ask why any hour moved. The chart is free and instant; “Why did this happen?” runs a full investigation of that hour.
        </p>
      </header>

      <section className="panel p-4" aria-label="What to look at">
        <div className="grid gap-4 md:grid-cols-[minmax(0,1.2fr)_minmax(0,2fr)_minmax(0,1.4fr)]">
          <div className="min-w-0">
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide muted">Number</h2>
            <select aria-label="Number" value={state.kpi} onChange={(e) => update({ kpi: e.target.value, floor: null, ceiling: null, mode: "usual" })} className="panel w-full p-2 text-sm">
              {Object.entries(KPIS).map(([id, k]) => <option key={id} value={id}>{k.label}</option>)}
            </select>
            {catalog && <p className="muted mt-1 text-xs">{catalog.kpis.find((k) => k.id === state.kpi)?.description}</p>}
          </div>
          <div className="min-w-0">
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide muted">Slice</h2>
            {!catalog && <p className="muted text-sm">Loading…</p>}
            <div className="grid grid-cols-3 gap-2">
              {dims.map((d) => (
                <label key={d.id} className="min-w-0 text-xs muted">
                  {DIM_LABELS[d.id] ?? d.label}
                  <select
                    value={state.filters[d.id] ?? ""}
                    onChange={(e) => {
                      const filters = { ...state.filters };
                      if (e.target.value) filters[d.id] = e.target.value;
                      else delete filters[d.id];
                      update({ filters, floor: null, ceiling: null, mode: "usual" });
                    }}
                    className="panel mt-1 w-full p-2 text-sm"
                  >
                    <option value="">{ANY[d.id] ?? "Any"}</option>
                    {(d.values ?? []).map((v) => <option key={v} value={v}>{VALUE_LABELS[v] ?? v}</option>)}
                  </select>
                </label>
              ))}
            </div>
          </div>
          <div className="min-w-0">
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide muted">Your lines</h2>
            <div className="grid grid-cols-2 gap-2">
              {lineInput("ceiling")}
              {lineInput("floor")}
            </div>
            <p className="muted mt-1 text-xs">Or drag them on the chart. They're kept in this page's link, so you can share it.</p>
          </div>
        </div>
      </section>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1.75fr)_minmax(0,1fr)]">
        <div className="min-w-0 space-y-4">
          <section className="panel p-4" aria-label="Chart">
            <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-2">
              <h2 className="min-w-0 text-base font-semibold">{kpiLabel(state.kpi)} · {sliceName(state.filters)}</h2>
              <Toggle label="Time range" className="ml-auto" options={RANGES.map((r) => ({ value: String(r.hours), label: r.label }))} value={String(state.hours)} onChange={(v) => update({ hours: Number(v) as ExploreState["hours"] })} />
            </div>
            {error && <p className="accent text-sm">{error}</p>}
            {!chart && !error && <p className="muted text-sm">Loading…</p>}
            {chart && (
              <ExploreChart view={chart} state={state} flagged={flaggedSet} selected={selected} onSelect={setSelected} onLine={(which, v) => update({ [which]: v, mode: "lines" })} />
            )}
            <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs muted">
              <span><span className="mr-1 inline-block h-2.5 w-4 rounded-sm bg-zinc-300/70 align-middle dark:bg-zinc-600/50" />usual range for that hour of the day</span>
              <span><span className="mr-1 inline-block w-4 border-t-2 border-dashed border-amber-600 align-middle" />your lines</span>
              <span><span className="mr-1 inline-block h-2.5 w-2.5 rounded-full bg-amber-600 align-middle" />flagged hour</span>
              {points.some((p) => p.value === null) && (
                <span><span className="mr-1 inline-block h-2.5 w-2 bg-zinc-300 align-middle dark:bg-zinc-600" />no data (before our record began, or a collector gap)</span>
              )}
              <span>click any hour to look closer</span>
            </div>
            {selectedPoint && chart && (
              <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2 rounded border border-[var(--gc-line)] p-3 text-sm">
                <div className="min-w-0"><span className="font-medium">{fmtTime(selectedPoint.t)}</span> <span className="muted">· {describeHour(selectedPoint, unit)}</span></div>
                {judged(selectedPoint)
                  ? <button type="button" onClick={() => void investigate(selectedPoint.t)} className="ml-auto rounded bg-amber-700 px-3 py-1.5 text-xs font-medium text-white hover:bg-amber-800">Why did this happen?</button>
                  : <span className="muted ml-auto text-xs">This hour can't be judged.</span>}
              </div>
            )}
          </section>

          <section className="panel p-4" aria-label="Flagged hours">
            <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-2">
              <h2 className="text-sm font-semibold uppercase tracking-wide muted">Flagged hours</h2>
              <Toggle label="Flag hours that" className="ml-auto" options={[{ value: "lines", label: "Cross my lines" }, { value: "usual", label: "Leave the usual range" }]} value={state.mode} onChange={(v) => update({ mode: v as Mode })} />
            </div>
            <ul data-testid="flagged" className="divide-y divide-[var(--gc-line)]">
              {[...flagged].reverse().slice(0, 8).map((p) => (
                <li key={p.t} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2">
                  <button type="button" className="min-w-0 text-left" onClick={() => setSelected(p.t)}>
                    <span className="text-sm font-medium">{fmtTime(p.t)}</span>
                    <span className="muted block text-xs">{reason(p, state, unit)}</span>
                  </button>
                  <span className="ml-auto">
                    {judged(p)
                      ? <button type="button" onClick={() => void investigate(p.t)} className="rounded border border-[var(--gc-line)] px-3 py-1 text-xs font-medium hover:border-amber-600">Why did this happen?</button>
                      : <span className="muted text-xs">too early to judge</span>}
                  </span>
                </li>
              ))}
            </ul>
            {flagged.length === 0 && chart && <p className="muted py-2 text-sm">{emptyText(state)}</p>}
            {flagged.length > 8 && <p className="muted text-xs">and {flagged.length - 8} more</p>}
            <p className="muted mt-3 text-xs">An investigation takes about 40 seconds. Visitors can start 10 model-written ones a day; after that the standard playbook answers for free.</p>
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

function emptyText(s: ExploreState): string {
  if (s.mode === "usual") return "No hour in this window left its usual range.";
  if (s.ceiling === null && s.floor === null) return "Add a ceiling or a floor above to see which hours cross it.";
  return "No hour in this window crossed your lines. Drag them closer to see what would have been flagged.";
}

function Toggle({ label, options, value, onChange, className = "" }: { label: string; options: { value: string; label: string }[]; value: string; onChange: (v: string) => void; className?: string }) {
  return (
    <div role="group" aria-label={label} className={`flex overflow-hidden rounded border border-[var(--gc-line)] text-xs ${className}`}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          aria-pressed={o.value === value}
          onClick={() => onChange(o.value)}
          className={`px-2.5 py-1 ${o.value === value ? "bg-zinc-800 text-white dark:bg-zinc-200 dark:text-zinc-900" : "hover:bg-zinc-100 dark:hover:bg-zinc-800"}`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function InvestigationPanel({ inv, label }: { inv: Investigation; label: string }) {
  const heading = <h2 className="mb-3 text-base font-semibold">Why did {label} move at {fmtTime(inv.hour)}?</h2>;
  if (inv.phase === "refused" || inv.phase === "failed") {
    return <Panel>{heading}<p className="text-sm">{inv.message}</p></Panel>;
  }
  if (inv.phase !== "done" || !inv.data) {
    const at = inv.data ? STEPS.findIndex((s) => s.state === inv.data?.state) : -1;
    return (
      <Panel>
        {heading}
        <p className="muted mb-3 text-xs">Investigating… this usually takes about 40 seconds.</p>
        <ol className="space-y-1.5 text-sm">
          {STEPS.map((s, i) => (
            <li key={s.state} className={`flex items-center gap-2 ${i > at ? "muted" : ""}`}>
              <span className="w-4 text-center" aria-hidden="true">{i < at ? "✓" : i === at ? "…" : "○"}</span>
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
          <p className="muted text-xs uppercase tracking-wide">Best explanation</p>
          <p className="mb-2 text-lg font-semibold">{c.top_hypothesis ? <HypothesisName id={c.top_hypothesis} /> : "Nothing stood out"}</p>
          <p className="muted text-xs">{CASE_STATES[c.state] ?? c.state}{inv.paid === false ? " · answered by the standard playbook (today's model-written investigations are used up)" : ""}</p>
        </div>
        <ul className="min-w-0 space-y-1">
          {claims.map((cl, i) => (
            <li key={i} className="flex items-start gap-2 text-sm"><TierBadge tier={cl.tier} /><span className="min-w-0 flex-1">{cl.text}</span></li>
          ))}
        </ul>
      </div>
      <a href={href({ view: "case", id: c.case_id })} className="mt-3 inline-block text-xs font-medium text-amber-700 underline dark:text-amber-400">
        Open the full case, with its evidence and step-by-step trace →
      </a>
    </Panel>
  );
}

import { useEffect, useState } from "react";
import { api, type CaseFull, type Claim, type Receipt, type Step, type Verdict } from "../api";
import { HypothesisName, Panel, StatusBadge, TierBadge, pct } from "../components";
import { CASE_STATES, HYPOTHESES, KPIS, TIERS, fmtTime, fmtValue, kpiLabel, segmentName, stepQuestion } from "../labels";
import { AskPanel } from "./AskPanel";
import { ReceiptCard } from "./ReceiptCard";
import { TraceView } from "./TraceView";

function ClaimRow({ claim, i, receipts }: { claim: Claim; i: number; receipts: Record<string, Receipt> }) {
  const [open, setOpen] = useState(false);
  const cited = claim.receipt_ids.map((id) => receipts[id]).filter(Boolean);
  return (
    <li className="border-b border-[var(--gc-line)] py-2 last:border-0">
      <button data-testid={`claim-${i}`} onClick={() => setOpen((o) => !o)} className="flex w-full items-start gap-2 text-left" aria-expanded={open}>
        <TierBadge tier={claim.tier} />
        <span className="flex-1 text-sm">{claim.text}</span>
        <span className="muted text-xs">{cited.length ? `${open ? "hide" : "show"} evidence (${cited.length})` : "no evidence link"}</span>
      </button>
      {open && (cited.length ? cited.map((r) => <ReceiptCard key={r.receipt_id} r={r} />) : <p className="muted mt-1 text-xs">An interpretation: nothing in the data backs this sentence directly.</p>)}
    </li>
  );
}

/** Same JSON value regardless of key order: receipts carry no step id, so a step is matched to
 * the receipt that ran the same tool with the same inputs. */
function canonical(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(canonical).join(",")}]`;
  if (v && typeof v === "object") {
    return `{${Object.keys(v).sort().map((k) => `${JSON.stringify(k)}:${canonical((v as Record<string, unknown>)[k])}`).join(",")}}`;
  }
  return JSON.stringify(v);
}

function DriverCard({ v, isTop, c }: { v: Verdict; isTop: boolean; c: CaseFull }) {
  const [open, setOpen] = useState(false);
  const steps = (c.plan?.steps ?? []).filter((s) => s.hypothesis_ids.includes(v.id));
  const ranFor = (s: Step) => c.receipts.find((r) => r.tool === s.tool && canonical(r.inputs) === canonical(s.inputs));
  const cited = new Set(steps.map((s) => ranFor(s)?.receipt_id));
  const claims = (c.conclusion?.claims ?? []).filter((cl) => cl.receipt_ids.some((id) => cited.has(id)));
  return (
    <li className={`rounded border p-2 ${isTop ? "border-amber-600" : "border-[var(--gc-line)]"}`}>
      <button data-testid={`driver-${v.id}`} onClick={() => setOpen((o) => !o)} className="w-full text-left" aria-expanded={open}>
        <div className="flex items-center justify-between gap-2"><HypothesisName id={v.id} /><StatusBadge status={v.status} /></div>
        <div className="muted mt-1 text-xs">{HYPOTHESES[v.id]?.blurb}</div>
        <div className="mt-1 text-xs">
          {pct(v.confidence)} likely{v.coverage !== null && v.status !== "refuted" ? ` · explains ${pct(v.coverage)} of the gap` : ""}
        </div>
        <div className="muted mt-1 text-xs underline">{open ? "hide" : "show"} how it was tested ({steps.length})</div>
      </button>
      {open && (
        <div data-testid={`driver-evidence-${v.id}`}>
          {claims.map((cl, i) => (
            <p key={i} className="mt-2 flex items-start gap-2 text-xs"><TierBadge tier={cl.tier} /><span>{cl.text}</span></p>
          ))}
          {steps.map((s) => {
            const r = ranFor(s);
            if (r) return <ReceiptCard key={s.step_id} r={r} />;
            const why = c.step_errors?.[s.step_id];
            return <p key={s.step_id} className="muted mt-2 text-xs">{stepQuestion(s.tool, s.inputs)}: {why ? `couldn't run (${why})` : "no evidence recorded"}.</p>;
          })}
          {!steps.length && <p className="muted mt-2 text-xs">No check in this investigation tested this driver.</p>}
        </div>
      )}
    </li>
  );
}

function TriggerNote({ r }: { r: Receipt }) {
  const [open, setOpen] = useState(false);
  const o = r.output as Record<string, unknown>;
  const kpi = String(o.kpi ?? "");
  const unit = KPIS[kpi]?.unit ?? "count";
  const filters = (o.filters as Record<string, string>) ?? {};
  const where = Object.values(filters).join(", ");
  const hours = Number(o.run_hours);
  return (
    <div className="panel p-3 text-sm" data-testid="trigger">
      <span className="font-medium">Opened automatically.</span>{" "}
      {kpiLabel(kpi)}{where ? ` in ${where}` : ""} was {Number(o.ratio).toFixed(1)} times its usual level for that hour of the day ({fmtValue(unit, Number(o.value))} against about {fmtValue(unit, Number(o.expected))}), {hours} hour{hours === 1 ? "" : "s"} in a row.{" "}
      <button className="muted text-xs underline" onClick={() => setOpen((x) => !x)} aria-expanded={open}>{open ? "hide" : "show"} evidence</button>
      {open && <ReceiptCard r={r} />}
    </div>
  );
}

export function CaseView({ id, initial }: { id: string; initial?: CaseFull }) {
  const [c, setCase] = useState<CaseFull | null>(initial ?? null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (initial) return;
    api.case(id).then(setCase).catch((e) => setError(String(e)));
  }, [id, initial]);
  if (error) return <p className="accent">{error}</p>;
  if (!c) return <p className="muted">Loading the investigation…</p>;
  const receipts = Object.fromEntries(c.receipts.map((r) => [r.receipt_id, r]));
  const ranked = c.conclusion?.ranked ?? [];
  const top = c.conclusion?.top_hypothesis;
  const failed = Object.keys(c.step_errors ?? {}).length;
  const trigger = c.receipts.find((r) => r.tool === "kpi.alarm" && r.output.status === "alarm");

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-xl font-semibold">
          Why did {kpiLabel(c.metric).toLowerCase()} move? · {segmentName(c.entity)} · {fmtTime(c.as_of)}
        </h1>
        <div className="muted text-sm" title={`case ${c.case_id} · state ${c.state}`}>
          {CASE_STATES[c.state] ?? c.state} · {c.cost_usd > 0 ? `${c.llm_calls} model call${c.llm_calls === 1 ? "" : "s"} · $${c.cost_usd.toFixed(2)}` : "standard playbook, no model"}
        </div>
      </header>
      {trigger && <TriggerNote r={trigger} />}
      {/* minmax(0,1fr): the query <pre> must scroll inside its column, not widen the page */}
      <div className="grid gap-4 lg:grid-cols-[300px_minmax(0,1fr)_320px]">
        <Panel title="Possible drivers" hint="Each one was tested against the numbers." className="min-w-0">
          <ol className="space-y-2" data-testid="hypothesis-tree">
            {ranked.map((v) => <DriverCard key={v.id} v={v} isTop={v.id === top} c={c} />)}
          </ol>
        </Panel>
        <Panel
          title="What happened"
          hint={`Every sentence is tagged: ${TIERS.governed.label} = ${TIERS.governed.blurb}; ${TIERS.derived.label} = ${TIERS.derived.blurb}; ${TIERS.inferred.label} = ${TIERS.inferred.blurb}.`}
          className="min-w-0"
        >
          {top && <p className="mb-2 text-base">Best-supported cause: <HypothesisName id={top} /></p>}
          <ul data-testid="claims">
            {(c.conclusion?.claims ?? []).map((cl, i) => <ClaimRow key={i} claim={cl} i={i} receipts={receipts} />)}
          </ul>
          {(c.conclusion?.caveats ?? []).map((cv, i) => (
            <p key={i} className="accent mt-3 text-sm">Note: {cv}</p>
          ))}
        </Panel>
        <div className="min-w-0 space-y-4">
          <Panel title="How it was investigated" hint={c.plan?.source === "llm" ? `The model chose these checks (accepted on attempt ${c.plan_attempts}).` : "The standard set of checks."}>
            <ol className="space-y-1 text-sm" data-testid="plan-dag">
              {(c.plan?.steps ?? []).map((s, i) => (
                <li key={s.step_id} title={`${s.step_id} ${s.tool} → ${s.hypothesis_ids.join(", ")}${c.step_errors?.[s.step_id] ? ` · ${c.step_errors[s.step_id]}` : ""}`}>
                  <span className="muted">{i + 1}.</span> {stepQuestion(s.tool, s.inputs)}
                  {c.step_errors?.[s.step_id] && <span className="accent"> · couldn't run</span>}
                </li>
              ))}
            </ol>
            {failed > 0 && <p className="muted mt-2 text-xs">{failed} check{failed === 1 ? "" : "s"} couldn't run (usually data not published yet); the conclusion says so.</p>}
          </Panel>
          <Panel title="Follow-up check" hint="Every investigation is re-run a day later: did the change hold, and does the answer still stand?">
            {c.recheck ? (
              <table className="w-full text-xs" data-testid="verify-diff">
                <thead><tr className="muted text-left"><th>driver</th><th>at first</th><th>on {fmtTime(c.recheck.as_of)}</th></tr></thead>
                <tbody>
                  {Object.entries(c.recheck.diffs).map(([h, d]) => (
                    <tr key={h}><td><HypothesisName id={h} /></td><td>{d.before ? <StatusBadge status={d.before} /> : "–"}</td><td><StatusBadge status={d.after} /></td></tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="muted text-sm">Not yet: the follow-up runs automatically a day later.</p>
            )}
          </Panel>
        </div>
      </div>
      <TraceView key={c.case_id} caseId={c.case_id} />
      <AskPanel target={{ caseId: c.case_id }} receipts={receipts} asOf={c.as_of} />
    </div>
  );
}

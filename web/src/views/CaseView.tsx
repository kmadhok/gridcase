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
    <li className="border-b border-dashed border-rule py-3 last:border-0">
      <button data-testid={`claim-${i}`} onClick={() => setOpen((o) => !o)} className="group flex w-full cursor-pointer flex-wrap items-start gap-x-3 gap-y-1 text-left sm:flex-nowrap" aria-expanded={open}>
        <TierBadge tier={claim.tier} />
        <span className="min-w-0 flex-1 basis-full text-[15px] leading-relaxed text-ink-soft sm:basis-auto">{claim.text}</span>
        <span className="whitespace-nowrap text-xs text-ink-muted underline decoration-rule underline-offset-4 transition-colors duration-150 group-hover:text-accent">{cited.length ? `${open ? "hide" : "show"} evidence (${cited.length})` : "no evidence link"}</span>
      </button>
      {open && (cited.length ? cited.map((r) => <ReceiptCard key={r.receipt_id} r={r} />) : <p className="mt-2 text-xs text-ink-muted">An interpretation: nothing in the data backs this sentence directly.</p>)}
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

/** The first driver sits above a non-refuted one with a higher score. The ranking's tiers and
 * causal precedence (rank_hypotheses), not the score, put it there; say so. */
function outranks(ranked: Verdict[], top: string): boolean {
  const first = ranked[0];
  return first?.id === top && ranked.some((v) => v.status !== "refuted" && v.confidence > first.confidence);
}

function DriverCard({ v, isTop, c }: { v: Verdict; isTop: boolean; c: CaseFull }) {
  const [open, setOpen] = useState(false);
  const steps = (c.plan?.steps ?? []).filter((s) => s.hypothesis_ids.includes(v.id));
  const ranFor = (s: Step) => c.receipts.find((r) => r.tool === s.tool && canonical(r.inputs) === canonical(s.inputs));
  const cited = new Set(steps.map((s) => ranFor(s)?.receipt_id));
  const claims = (c.conclusion?.claims ?? []).filter((cl) => cl.receipt_ids.some((id) => cited.has(id)));
  return (
    <li className={`rounded-xl border p-3 ${isTop ? "border-accent" : "border-rule"}`}>
      <button data-testid={`driver-${v.id}`} onClick={() => setOpen((o) => !o)} className="group w-full cursor-pointer text-left" aria-expanded={open}>
        <div className="flex items-start justify-between gap-2"><span className="text-[15px] leading-snug"><HypothesisName id={v.id} /></span><StatusBadge status={v.status} /></div>
        <div className="mt-1 text-xs leading-snug text-ink-muted">{HYPOTHESES[v.id]?.blurb}</div>
        <div className="tabular mt-2 text-xs font-medium text-ink-soft">
          {pct(v.confidence)} likely{v.coverage !== null && v.status !== "refuted" ? ` · explains ${pct(v.coverage)} of the gap` : ""}
        </div>
        <div className="mt-1.5 text-xs text-ink-muted underline decoration-rule underline-offset-4 transition-colors duration-150 group-hover:text-accent">{open ? "hide" : "show"} how it was tested ({steps.length})</div>
      </button>
      {open && (
        <div data-testid={`driver-evidence-${v.id}`}>
          {claims.map((cl, i) => (
            <p key={i} className="mt-3 flex items-start gap-2 text-xs leading-relaxed text-ink-soft"><TierBadge tier={cl.tier} /><span className="min-w-0">{cl.text}</span></p>
          ))}
          {steps.map((s) => {
            const r = ranFor(s);
            if (r) return <ReceiptCard key={s.step_id} r={r} />;
            const why = c.step_errors?.[s.step_id];
            return <p key={s.step_id} className="mt-2 text-xs text-ink-muted">{stepQuestion(s.tool, s.inputs)}: {why ? `couldn't run (${why})` : "no evidence recorded"}.</p>;
          })}
          {!steps.length && <p className="mt-2 text-xs text-ink-muted">No check in this investigation tested this driver.</p>}
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
  const ratio = o.ratio == null ? null : Number(o.ratio);
  const now = fmtValue(unit, Number(o.value));
  const usual = fmtValue(unit, Number(o.expected));
  const size =
    ratio !== null && ratio >= 1 ? `was ${ratio.toFixed(1)} times its usual level for that hour of the day (${now} against about ${usual})`
    : ratio !== null && ratio >= 0.01 ? `was ${(100 * ratio).toFixed(0)}% of its usual level for that hour of the day (${now} against about ${usual})`
    : `was ${now} against a usual level of about ${usual} for that hour of the day`;
  return (
    <div className="panel bg-blush px-5 py-4 text-[15px] leading-relaxed text-ink-soft" data-testid="trigger">
      <span className="font-medium text-ink">Opened automatically.</span>{" "}
      {kpiLabel(kpi)}{where ? ` in ${where}` : ""} {size}, {hours} hour{hours === 1 ? "" : "s"} in a row.{" "}
      <button className="link-ink text-xs text-ink-soft" onClick={() => setOpen((x) => !x)} aria-expanded={open}>{open ? "hide" : "show"} evidence</button>
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
  if (!c) return <p className="text-ink-muted">Loading the investigation…</p>;
  const receipts = Object.fromEntries(c.receipts.map((r) => [r.receipt_id, r]));
  const ranked = c.conclusion?.ranked ?? [];
  const top = c.conclusion?.top_hypothesis;
  const failed = Object.keys(c.step_errors ?? {}).length;
  const trigger = c.receipts.find((r) => r.tool === "kpi.alarm" && r.output.status === "alarm");

  return (
    <div className="space-y-4">
      <header className="flex flex-col gap-3 pb-2">
        <h1 className="display max-w-4xl text-[34px] leading-[1.05] md:text-5xl">
          Why did {kpiLabel(c.metric).toLowerCase()} move? · {segmentName(c.entity)} · {fmtTime(c.as_of)}
        </h1>
        <div className="label" title={`case ${c.case_id} · state ${c.state}`}>
          {CASE_STATES[c.state] ?? c.state} · {c.cost_usd > 0 ? `${c.llm_calls} model call${c.llm_calls === 1 ? "" : "s"} · $${c.cost_usd.toFixed(2)}` : "standard playbook, no model"}
        </div>
      </header>
      {c.retracted && (
        <p className="accent text-sm" data-testid="retracted" title={`retracted ${fmtTime(c.retracted.at)}`}>
          <span className="font-medium">Retracted:</span> {c.retracted.reason}
        </p>
      )}
      {c.judged_without_data && (
        <p data-testid="judged-without-data" className="warn rounded-xl border border-rule p-3 text-sm">
          Opened while our collector was down: the missing hours read as a drop, so this alarm was judged on missing data. Kept as it was judged; the hours have since been filled.
        </p>
      )}
      {trigger && <TriggerNote r={trigger} />}
      {/* minmax(0,1fr): the query <pre> must scroll inside its column, not widen the page */}
      <div className="grid gap-4 lg:grid-cols-[300px_minmax(0,1fr)_320px]">
        <Panel title="Possible drivers" hint="Each one was tested against the numbers." className="min-w-0">
          <ol className="space-y-2" data-testid="hypothesis-tree">
            {ranked.map((v) => <DriverCard key={v.id} v={v} isTop={v.id === top} c={c} />)}
          </ol>
          {top && outranks(ranked, top) && (
            <p data-testid="rank-note" className="mt-3 text-xs leading-snug text-ink-muted">
              Listed by cause, not by score: a data problem comes first, then supported causes that explain most of the gap, then what follows from them.
            </p>
          )}
        </Panel>
        <Panel
          title="What happened"
          hint={`Every sentence is tagged: ${TIERS.governed.label} = ${TIERS.governed.blurb}; ${TIERS.derived.label} = ${TIERS.derived.blurb}; ${TIERS.inferred.label} = ${TIERS.inferred.blurb}.`}
          className="min-w-0"
        >
          {top && <p className="display mb-2 text-2xl leading-tight">Best-supported cause: <HypothesisName id={top} /></p>}
          <ul data-testid="claims">
            {(c.conclusion?.claims ?? []).map((cl, i) => <ClaimRow key={i} claim={cl} i={i} receipts={receipts} />)}
          </ul>
          {(c.conclusion?.caveats ?? []).map((cv, i) => (
            <p key={i} className="warn mt-3 text-sm">Note: {cv}</p>
          ))}
        </Panel>
        <div className="min-w-0 space-y-4">
          <Panel title="How it was investigated" hint={c.plan?.source === "llm" ? `The model chose these checks (accepted on attempt ${c.plan_attempts}).` : "The standard set of checks."}>
            <ol className="space-y-2 text-sm text-ink-soft" data-testid="plan-dag">
              {(c.plan?.steps ?? []).map((s, i) => (
                <li key={s.step_id} title={`${s.step_id} ${s.tool} → ${s.hypothesis_ids.join(", ")}${c.step_errors?.[s.step_id] ? ` · ${c.step_errors[s.step_id]}` : ""}`}>
                  <span className="tabular text-ink-muted">{i + 1}.</span> {stepQuestion(s.tool, s.inputs)}
                  {c.step_errors?.[s.step_id] && <span className="accent"> · couldn't run</span>}
                </li>
              ))}
            </ol>
            {failed > 0 && <p className="mt-3 text-xs text-ink-muted">{failed} check{failed === 1 ? "" : "s"} couldn't run (usually data not published yet); the conclusion says so.</p>}
          </Panel>
          <Panel title="Follow-up check" hint="Every investigation is re-run a day later: did the change hold, and does the answer still stand?">
            {c.recheck ? (
              <div className="overflow-x-auto rounded-xl border border-rule">
              <table className="w-full border-collapse text-xs text-ink-soft" data-testid="verify-diff">
                <thead><tr className="bg-shade text-left [&>th]:px-2.5 [&>th]:py-2 [&>th]:font-medium"><th>driver</th><th>at first</th><th>on {fmtTime(c.recheck.as_of)}</th></tr></thead>
                <tbody>
                  {Object.entries(c.recheck.diffs).map(([h, d]) => (
                    <tr key={h} className="border-t border-rule [&>td]:px-2.5 [&>td]:py-2"><td><HypothesisName id={h} /></td><td>{d.before ? <StatusBadge status={d.before} /> : "–"}</td><td><StatusBadge status={d.after} /></td></tr>
                  ))}
                </tbody>
              </table>
              </div>
            ) : (
              <p className="text-sm text-ink-muted">Not yet: the follow-up runs automatically a day later.</p>
            )}
          </Panel>
        </div>
      </div>
      <TraceView key={c.case_id} caseId={c.case_id} />
      <AskPanel target={{ caseId: c.case_id }} receipts={receipts} asOf={c.as_of} />
    </div>
  );
}

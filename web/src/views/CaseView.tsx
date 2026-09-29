import { useEffect, useState } from "react";
import { api, type CaseFull, type Claim, type Receipt } from "../api";
import { HypothesisName, Panel, StatusBadge, TierBadge, fmt, pct } from "../components";
import { CASE_STATES, HYPOTHESES, REGION_NAMES, TIERS, fmtTime, metricName, stepQuestion } from "../labels";

function ReceiptCard({ r }: { r: Receipt }) {
  const [showSql, setShowSql] = useState(false);
  const summary = Object.entries(r.output)
    .filter(([, v]) => typeof v === "number" || typeof v === "string" || typeof v === "boolean")
    .slice(0, 8);
  return (
    <div className="mt-2 rounded border border-[var(--gc-line)] p-3 text-sm" data-testid="receipt">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="font-medium" title={r.tool}>{stepQuestion(r.tool, r.inputs)}</span>
        <span className="mono muted text-xs" title="evidence id · data known as of · run time">{r.receipt_id.slice(0, 12)} · as of {fmtTime(r.as_of)} · {r.runtime_ms} ms</span>
      </div>
      <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 md:grid-cols-3">
        {summary.map(([k, v]) => (
          <div key={k} className="min-w-0"><dt className="muted text-xs">{k.replace(/_/g, " ")}</dt><dd className="mono break-all text-xs">{typeof v === "number" ? fmt(v, Math.abs(v) < 10 ? 3 : 0) : String(v)}</dd></div>
        ))}
      </dl>
      <button className="mt-2 text-xs underline" onClick={() => setShowSql((s) => !s)} data-testid="toggle-sql">
        {showSql ? "Hide" : "Show"} the exact quer{r.sql.length === 1 ? "y" : "ies"} ({r.sql.length})
      </button>
      {showSql && r.sql.map((q, i) => (
        <pre key={i} data-testid="receipt-sql" className="mono mt-2 max-h-64 overflow-auto rounded bg-zinc-100 p-2 text-[11px] leading-snug dark:bg-zinc-900">{q.sql}</pre>
      ))}
    </div>
  );
}

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

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-xl font-semibold">
          {REGION_NAMES[c.entity] ?? c.entity} · {metricName(c.metric)} · {fmtTime(c.as_of)}
        </h1>
        <div className="muted text-sm" title={`case ${c.case_id} · state ${c.state}`}>
          {CASE_STATES[c.state] ?? c.state} · {c.llm_calls} model call{c.llm_calls === 1 ? "" : "s"} · ${c.cost_usd.toFixed(2)}
        </div>
      </header>
      {/* minmax(0,1fr): the query <pre> must scroll inside its column, not widen the page */}
      <div className="grid gap-4 lg:grid-cols-[300px_minmax(0,1fr)_320px]">
        <Panel title="Possible causes" hint="Each one was tested against the numbers." className="min-w-0">
          <ol className="space-y-2" data-testid="hypothesis-tree">
            {ranked.map((v) => (
              <li key={v.id} className={`rounded border p-2 ${v.id === top ? "border-amber-600" : "border-[var(--gc-line)]"}`}>
                <div className="flex items-center justify-between gap-2"><HypothesisName id={v.id} /><StatusBadge status={v.status} /></div>
                <div className="muted mt-1 text-xs">{HYPOTHESES[v.id]?.blurb}</div>
                <div className="mt-1 text-xs">
                  {pct(v.confidence)} likely{v.coverage !== null && v.status !== "refuted" ? ` · explains ${pct(v.coverage)} of the gap` : ""}
                </div>
              </li>
            ))}
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
          <Panel title="Follow-up check" hint="Some grid data arrives a day or two late, so every case is re-checked once it lands.">
            {c.recheck ? (
              <table className="w-full text-xs" data-testid="verify-diff">
                <thead><tr className="muted text-left"><th>cause</th><th>at first</th><th>on {fmtTime(c.recheck.as_of)}</th></tr></thead>
                <tbody>
                  {Object.entries(c.recheck.diffs).map(([h, d]) => (
                    <tr key={h}><td><HypothesisName id={h} /></td><td>{d.before ? <StatusBadge status={d.before} /> : "–"}</td><td><StatusBadge status={d.after} /></td></tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="muted text-sm">Not yet: the follow-up runs automatically when the delayed data arrives.</p>
            )}
          </Panel>
        </div>
      </div>
    </div>
  );
}

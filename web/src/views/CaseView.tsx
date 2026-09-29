import { useEffect, useState } from "react";
import { api, REGION_NAMES, type CaseFull, type Claim, type Receipt } from "../api";
import { HypothesisName, Panel, StatusBadge, TierBadge, fmt, pct } from "../components";

function ReceiptCard({ r }: { r: Receipt }) {
  const [showSql, setShowSql] = useState(false);
  const summary = Object.entries(r.output)
    .filter(([, v]) => typeof v === "number" || typeof v === "string" || typeof v === "boolean")
    .slice(0, 8);
  return (
    <div className="mt-2 rounded border border-[var(--gc-line)] p-3 text-sm" data-testid="receipt">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="mono font-medium">{r.tool}</span>
        <span className="mono muted text-xs">{r.receipt_id} · as of {r.as_of} · {r.runtime_ms} ms</span>
      </div>
      <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 md:grid-cols-4">
        {summary.map(([k, v]) => (
          <div key={k}><dt className="muted text-xs">{k}</dt><dd className="mono text-xs">{typeof v === "number" ? fmt(v, Math.abs(v) < 10 ? 3 : 0) : String(v)}</dd></div>
        ))}
      </dl>
      <button className="mt-2 text-xs underline" onClick={() => setShowSql((s) => !s)} data-testid="toggle-sql">
        {showSql ? "Hide" : "Show"} SQL ({r.sql.length} quer{r.sql.length === 1 ? "y" : "ies"})
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
        <span className="muted text-xs">{cited.length ? `${cited.length} receipt${cited.length > 1 ? "s" : ""}` : "uncited"}</span>
      </button>
      {open && (cited.length ? cited.map((r) => <ReceiptCard key={r.receipt_id} r={r} />) : <p className="muted mt-1 text-xs">Inferred: no receipt backs this sentence.</p>)}
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
  if (!c) return <p className="muted">Loading case…</p>;
  const receipts = Object.fromEntries(c.receipts.map((r) => [r.receipt_id, r]));
  const ranked = c.conclusion?.ranked ?? [];
  const top = c.conclusion?.top_hypothesis;

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-xl font-semibold">
          {REGION_NAMES[c.entity] ?? c.entity} · {c.metric} · as of {c.as_of.replace("T", " ")}Z
        </h1>
        <div className="muted text-sm">
          case <span className="mono">{c.case_id}</span> · state <StatusBadge status={c.state} /> · {c.llm_calls} LLM calls · ${c.cost_usd.toFixed(4)}
        </div>
      </header>
      {/* minmax(0,1fr): the SQL <pre> must scroll inside its column, not widen the page */}
      <div className="grid gap-4 lg:grid-cols-[300px_minmax(0,1fr)_320px]">
        <Panel title="Hypotheses" className="min-w-0">
          <ol className="space-y-2" data-testid="hypothesis-tree">
            {ranked.map((v) => (
              <li key={v.id} className={`rounded border p-2 ${v.id === top ? "border-amber-600" : "border-[var(--gc-line)]"}`}>
                <div className="flex items-center justify-between gap-2"><HypothesisName id={v.id} /><StatusBadge status={v.status} /></div>
                <div className="muted mt-1 text-xs">confidence {pct(v.confidence)}{v.coverage !== null ? ` · covers ${pct(v.coverage)} of the miss` : ""}{v.margin !== null ? ` · margin ${v.margin.toFixed(2)}` : ""}</div>
              </li>
            ))}
          </ol>
        </Panel>
        <Panel title="Conclusion" className="min-w-0">
          {top && <p className="mb-2 text-base">Best-supported cause: <HypothesisName id={top} /></p>}
          <ul data-testid="claims">
            {(c.conclusion?.claims ?? []).map((cl, i) => <ClaimRow key={i} claim={cl} i={i} receipts={receipts} />)}
          </ul>
          {(c.conclusion?.caveats ?? []).map((cv, i) => (
            <p key={i} className="accent mt-3 text-sm">! {cv}</p>
          ))}
        </Panel>
        <div className="min-w-0 space-y-4">
          <Panel title="Plan">
            <ol className="space-y-1 text-xs" data-testid="plan-dag">
              {(c.plan?.steps ?? []).map((s) => (
                <li key={s.step_id} className="mono">
                  <span className="muted">{s.step_id}</span> {s.tool}
                  <span className="muted"> → {s.hypothesis_ids.map((h) => h.split("_")[0]).join(",")}</span>
                  {s.depends_on.length > 0 && <span className="muted"> ⇐ {s.depends_on.join(",")}</span>}
                </li>
              ))}
            </ol>
            <p className="muted mt-2 text-xs">{c.plan?.source === "llm" ? "Planned by the model" : "Template default plan"} · {c.plan_attempts} attempt{c.plan_attempts === 1 ? "" : "s"}</p>
          </Panel>
          <Panel title="Verify">
            {c.recheck ? (
              <table className="w-full text-xs" data-testid="verify-diff">
                <thead><tr className="muted text-left"><th>hypothesis</th><th>before</th><th>after</th></tr></thead>
                <tbody>
                  {Object.entries(c.recheck.diffs).map(([h, d]) => (
                    <tr key={h}><td className="mono">{h.split("_")[0]}</td><td>{d.before ?? "–"}</td><td><StatusBadge status={d.after} /></td></tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="muted text-sm">Re-check pending: scheduled once lagged data lands.</p>
            )}
          </Panel>
        </div>
      </div>
    </div>
  );
}

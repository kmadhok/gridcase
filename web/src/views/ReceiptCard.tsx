import { useEffect, useState } from "react";
import { api, type Definitions, type Receipt } from "../api";
import { fmt } from "../components";
import { fmtTime, stepQuestion } from "../labels";

export function ReceiptCard({ r }: { r: Receipt }) {
  const [showSql, setShowSql] = useState(false);
  const summary = Object.entries(r.output)
    .filter(([, v]) => typeof v === "number" || typeof v === "string" || typeof v === "boolean")
    .slice(0, 8);
  return (
    <div className="@container mt-2 rounded border border-[var(--gc-line)] p-3 text-sm" data-testid="receipt">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="font-medium" title={r.tool}>{stepQuestion(r.tool, r.inputs)}</span>
        <span className="mono muted text-xs" title="evidence id · data known as of · run time">{r.receipt_id.slice(0, 12)} · as of {fmtTime(r.as_of)} · {r.runtime_ms} ms</span>
      </div>
      <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 @md:grid-cols-3">
        {summary.map(([k, v]) => (
          <div key={k} className="min-w-0"><dt className="muted text-xs">{k.replace(/_/g, " ")}</dt><dd className="mono break-all text-xs">{typeof v === "number" ? fmt(v, Math.abs(v) < 10 ? 3 : 0) : String(v)}</dd></div>
        ))}
      </dl>
      <button className="mt-2 text-xs underline" onClick={() => setShowSql((s) => !s)} data-testid="toggle-sql">
        {showSql ? "Hide" : "Show"} the exact quer{r.sql.length === 1 ? "y" : "ies"} ({r.sql.length})
      </button>
      {showSql && typeof r.inputs.kpi === "string" && <KpiDefinition id={r.inputs.kpi} />}
      {showSql && r.sql.map((q, i) => (
        <pre key={i} data-testid="receipt-sql" className="mono mt-2 max-h-64 overflow-auto rounded bg-zinc-100 p-2 text-[11px] leading-snug dark:bg-zinc-900">{q.sql}</pre>
      ))}
    </div>
  );
}

/** The KPI behind this evidence, as ontology/kpis.yaml defines it. */
function KpiDefinition({ id }: { id: string }) {
  const [kpi, setKpi] = useState<Definitions["kpis"][number] | null>(null);
  useEffect(() => {
    let live = true;
    api.definitions().then((d) => live && setKpi(d.kpis.find((k) => k.id === id) ?? null)).catch(() => undefined);
    return () => { live = false; };
  }, [id]);
  if (!kpi) return null;
  return (
    <p className="mt-2 text-xs" data-testid="receipt-kpi">
      <span className="font-medium">{kpi.label}</span>: {kpi.description} <span className="muted">Formula: {kpi.formula}.</span>
    </p>
  );
}

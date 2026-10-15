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
    <div className="@container inner mt-3 bg-paper p-3.5 text-sm" data-testid="receipt">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="font-medium text-ink" title={r.tool}>{stepQuestion(r.tool, r.inputs)}</span>
        <span className="tabular text-xs text-ink-muted" title="evidence id · data known as of · run time">{r.receipt_id.slice(0, 12)} · as of {fmtTime(r.as_of)} · {r.runtime_ms} ms</span>
      </div>
      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 border-t border-dashed border-rule pt-3 @md:grid-cols-3">
        {summary.map(([k, v]) => (
          <div key={k} className="min-w-0"><dt className="text-xs text-ink-muted">{k.replace(/_/g, " ")}</dt><dd className="mono break-all text-xs text-ink">{typeof v === "number" ? fmt(v, Math.abs(v) < 10 ? 3 : 0) : String(v)}</dd></div>
        ))}
      </dl>
      <button className="link-ink mt-3 text-xs font-medium text-ink-soft" onClick={() => setShowSql((s) => !s)} data-testid="toggle-sql">
        {showSql ? "Hide" : "Show"} the exact quer{r.sql.length === 1 ? "y" : "ies"} ({r.sql.length})
      </button>
      {showSql && typeof r.inputs.kpi === "string" && <KpiDefinition id={r.inputs.kpi} />}
      {showSql && r.sql.map((q, i) => (
        <pre key={i} data-testid="receipt-sql" className="code-block mt-2 max-h-64 overflow-auto px-3 py-2">{q.sql}</pre>
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
    <p className="mt-2 text-xs text-ink-soft" data-testid="receipt-kpi">
      <span className="font-medium">{kpi.label}</span>: {kpi.description} <span className="text-ink-muted">Formula: {kpi.formula}.</span>
    </p>
  );
}

import { useEffect, useState } from "react";
import { api, type CaseTrace, type TraceSpan } from "../api";
import { Panel } from "../components";
import { stepQuestion } from "../labels";

const LABELS: Record<string, string> = {
  compare: "Measured the change",
  plan: "Planned the checks",
  execute: "Ran the checks",
  explain: "Wrote the explanation",
  verify: "Checked every sentence against the evidence",
  publish: "Saved the case",
};

// The free standard playbook runs the same steps without a model (no model name recorded).
const usedModel = (span: TraceSpan) => span.kind === "llm" && Boolean(span.detail.model);

function label(span: TraceSpan): string {
  if (span.kind === "tool") return stepQuestion(span.name, span.detail.inputs);
  const name = LABELS[span.name] ?? "Investigation step";
  if (span.kind === "llm" && !usedModel(span)) {
    return span.name === "plan" ? `${name} (standard playbook, no model)` : `${name} (from templates, no model)`;
  }
  return span.name === "plan" && (span.detail.attempts ?? 1) > 1
    ? `${name} (${span.detail.attempts} tries)` : name;
}

function duration(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
}

export function TraceView({ caseId }: { caseId: string }) {
  const [open, setOpen] = useState(false);
  const [trace, setTrace] = useState<CaseTrace | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setTrace(null);
    setError(false);
    api.trace(caseId).then((data) => {
      if (!cancelled) setTrace(data);
    }).catch(() => {
      if (!cancelled) setError(true);
    });
    return () => { cancelled = true; };
  }, [caseId, open]);
  const totals = trace?.totals;
  // "Measured the change" is one query; its own row would repeat the step.
  const compareSeqs = new Set(trace?.spans.filter((s) => s.name === "compare" && s.kind === "stage").map((s) => s.seq));
  const rows = trace?.spans.filter((s) => !(s.kind === "tool" && s.parent_seq !== null && compareSeqs.has(s.parent_seq))) ?? [];

  return (
    <Panel title="Step by step" hint="What the investigation did, in order, how long each step took and what it cost." className="min-w-0">
      <button className="btn btn-sm" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
        {open ? "Hide the timeline" : "Show the timeline"}
      </button>
      {open && (
        <div className="mt-5 min-w-0">
          {error ? <p className="accent text-sm">The timeline couldn't load. Please try again.</p>
            : !trace ? <p className="text-sm text-ink-muted">Loading the timeline…</p>
            : !trace.recorded ? <p className="text-sm text-ink-muted">Step-by-step timing is recorded for investigations from 3 October 2026 on.</p>
            : totals && <>
              <p className="tabular mb-4 text-sm text-ink-soft">
                Took {(totals.wall_ms / 1000).toFixed(1)} s · {totals.model_calls ? `${totals.model_calls} model call${totals.model_calls === 1 ? "" : "s"} · $${totals.cost_usd.toFixed(2)}` : "standard playbook, no model calls"} · {totals.checks_run} check{totals.checks_run === 1 ? "" : "s"}{totals.checks_failed > 0 ? `, ${totals.checks_failed} couldn't run` : ""}
              </p>
              <ol className="min-w-0 divide-y divide-dashed divide-rule" data-testid="trace-timeline">
                {rows.map((span) => {
                  const left = Math.min(100, Math.max(0, 100 * span.offset_ms / (totals.wall_ms || 1)));
                  const width = Math.min(100 - left, Math.max(0, 100 * span.ms / (totals.wall_ms || 1)));
                  return (
                    <li key={span.seq} className="grid min-w-0 grid-cols-[minmax(0,1fr)_minmax(0,1fr)_4.5rem] items-center gap-3 py-2 text-xs text-ink-soft" title={[span.name, span.detail.step_id, span.detail.receipt_id, span.detail.model, span.detail.error].filter(Boolean).join(" · ")}>
                      <div className={`min-w-0 break-words ${span.parent_seq !== null ? "pl-3" : "font-medium text-ink"}`}>
                        {label(span)}
                        {span.status === "error" && <span className="accent"> · couldn't run</span>}
                        {span.name === "verify" && <span className="block font-normal text-ink-muted">kept {span.detail.kept ?? 0}, removed {span.detail.dropped ?? 0}</span>}
                      </div>
                      <div className="relative h-3 min-w-0 overflow-hidden" aria-hidden="true">
                        <div data-testid="trace-bar" className={`absolute h-3 rounded-full ${usedModel(span) ? "bg-accent" : "bg-[var(--gc-mark)]"}`} style={{ left: `${left}%`, width: `${width}%`, minWidth: "2px" }} />
                      </div>
                      <div className="tabular min-w-0 break-words text-right text-ink-muted">
                        {duration(span.ms)}
                        {usedModel(span) && <span className="accent block">${(span.detail.cost_usd ?? 0).toFixed(2)}</span>}
                      </div>
                    </li>
                  );
                })}
              </ol>
            </>}
        </div>
      )}
    </Panel>
  );
}

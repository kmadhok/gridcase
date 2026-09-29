import { useEffect, useRef, useState } from "react";
import { api, type BenchRun } from "../api";
import { Panel, pct } from "../components";
import { HYPOTHESES, REGION_NAMES, hypothesisName, stepQuestion } from "../labels";

interface TraceEvent { kind: string; data: Record<string, unknown> }

function traceLine(e: TraceEvent): string {
  switch (e.kind) {
    case "started": return `Started: ${REGION_NAMES[String(e.data.entity)] ?? e.data.entity}, using only data published by ${e.data.as_of}`;
    case "plan": return `Planned ${(e.data.steps as unknown[]).length} checks for ${(e.data.hypotheses as string[]).length} possible causes`;
    case "receipt": return stepQuestion(String(e.data.tool), (e.data.inputs as Record<string, unknown>) ?? {}) + " → evidence recorded";
    case "conclusion": return `Conclusion: ${hypothesisName(String(e.data.top_hypothesis))}`;
    case "error": return `Couldn't finish: ${String(e.data.message)}`;
    case "done": return "Finished";
    default: return JSON.stringify(e.data).slice(0, 80);
  }
}

export function BenchView() {
  const [runs, setRuns] = useState<BenchRun[] | null>(null);
  const [trace, setTrace] = useState<TraceEvent[]>([]);
  const [running, setRunning] = useState(false);
  const es = useRef<EventSource | null>(null);

  useEffect(() => {
    api.bench().then((b) => setRuns(b.runs)).catch(() => setRuns([]));
    return () => es.current?.close();
  }, []);

  const replayUri = () => {
    setTrace([]);
    setRunning(true);
    const src = new EventSource("/api/replay/uri_2021/stream");
    es.current = src;
    for (const kind of ["started", "plan", "receipt", "conclusion", "error", "done"]) {
      src.addEventListener(kind, (e) => {
        setTrace((t) => [...t, { kind, data: JSON.parse((e as MessageEvent).data) }]);
        if (kind === "done") { setRunning(false); src.close(); }
      });
    }
    src.onerror = () => { setRunning(false); src.close(); };
  };

  const latest = runs && runs.length ? runs.find((r) => r.config.validator !== false) ?? runs[runs.length - 1] : null;
  const confusion = (latest?.summary.synthetic.confusion ?? {}) as Record<string, Record<string, number>>;
  const hyps = Array.from(new Set([...Object.keys(confusion), ...Object.values(confusion).flatMap((r) => Object.keys(r))])).sort();

  return (
    <div className="space-y-4">
      <Panel title="Scorecard" hint="How often GridCase names the right cause. Real events are the independent test; the test cases are faults we inject into real data, so they partly measure the system agreeing with itself.">
        {!runs ? <p className="muted text-sm">Loading…</p> : runs.length === 0 ? <p className="muted text-sm">No results yet.</p> : (
          <table className="w-full text-sm">
            <thead><tr className="muted text-left"><th>run</th><th>planner</th><th>plan checks</th><th>real events called right</th><th>test cases called right</th><th>…within its top 3</th><th>quiet periods wrongly flagged</th></tr></thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.name} className="border-t border-[var(--gc-line)]" title={`calibration (Brier, lower is better): ${r.summary.synthetic.brier?.toFixed(3) ?? "–"}`}>
                  <td className="mono">{r.name}</td>
                  <td>{String(r.config.model) === "fake" ? "standard playbook" : String(r.config.model)}</td>
                  <td>{r.config.validator === false ? "off" : "on"}</td>
                  <td>{r.summary.replay.n ? `${pct(r.summary.replay.top1)} of ${r.summary.replay.n}` : "–"}</td>
                  <td>{pct(r.summary.synthetic.top1)} of {r.summary.synthetic.n}</td>
                  <td>{pct(r.summary.synthetic.top3)}</td>
                  <td>{r.summary.null.n ? `${pct(r.summary.null.false_alarm_rate)} of ${r.summary.null.n}` : "–"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="What it confused with what" hint="Rows: the cause we injected. Columns: what GridCase said. Green on the diagonal is right.">
          {hyps.length === 0 ? <p className="muted text-sm">–</p> : (
            <table className="text-xs">
              <thead><tr><th className="muted text-left">injected ↓ / said →</th>{hyps.map((h) => <th key={h} className="px-1 font-normal" title={h}>{HYPOTHESES[h]?.short ?? h}</th>)}</tr></thead>
              <tbody>
                {Object.keys(confusion).sort().map((t) => (
                  <tr key={t}><td className="pr-2" title={t}>{hypothesisName(t)}</td>{hyps.map((p) => {
                    const n = confusion[t]?.[p] ?? 0;
                    return <td key={p} className={`px-1 text-center ${n && t === p ? "ok font-semibold" : n ? "accent" : "muted"}`}>{n || "·"}</td>;
                  })}</tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
        <Panel title="Watch it investigate Winter Storm Uri" hint="February 2021, Texas. Runs live, using only data that had been published by 16 Feb 2021 12:00 UTC, and shows each step as it happens (about a minute).">
          <button onClick={replayUri} disabled={running} data-testid="replay-uri" className="rounded bg-amber-700 px-3 py-2 text-sm font-medium text-white hover:bg-amber-800 disabled:opacity-50">
            {running ? "Investigating…" : "Investigate Winter Storm Uri"}
          </button>
          <ol className="mt-3 space-y-1 text-xs" data-testid="trace">
            {trace.map((e, i) => (
              <li key={i} className={e.kind === "error" ? "accent" : ""}>{traceLine(e)}</li>
            ))}
          </ol>
        </Panel>
      </div>
      {latest && latest.replay.length > 0 && (
        <Panel title="Real events" hint="Historical events with a documented cause. The eclipse is a trick question: the forecast anticipated it, so the right answer is to raise no alarm.">
          <table className="w-full text-sm">
            <thead><tr className="muted text-left"><th>event</th><th>region</th><th>actual cause</th><th>GridCase said</th><th>right?</th></tr></thead>
            <tbody>
              {latest.replay.map((c) => (
                <tr key={c.scenario_id} className="border-t border-[var(--gc-line)]">
                  <td className="mono">{c.scenario_id}</td><td>{REGION_NAMES[c.entity] ?? c.entity}</td><td>{hypothesisName(c.ground_truth[0])}</td>
                  <td>{c.predicted[0] ? hypothesisName(c.predicted[0]) : <span className="muted">couldn't run: {(c.error ?? "").slice(0, 40)}</span>}</td>
                  <td>{c.top1 ? <span className="ok">✓</span> : <span className="accent">✗</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}
    </div>
  );
}

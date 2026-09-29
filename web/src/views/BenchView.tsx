import { useEffect, useRef, useState } from "react";
import { api, HYPOTHESIS_NAMES, type BenchRun } from "../api";
import { Panel, pct } from "../components";

interface TraceEvent { kind: string; data: Record<string, unknown> }

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

  const latest = runs && runs.length ? runs[runs.length - 1] : null;
  const confusion = (latest?.summary.synthetic.confusion ?? {}) as Record<string, Record<string, number>>;
  const hyps = Array.from(new Set([...Object.keys(confusion), ...Object.values(confusion).flatMap((r) => Object.keys(r))])).sort();

  return (
    <div className="space-y-4">
      <Panel title="Leaderboard">
        {!runs ? <p className="muted text-sm">Loading…</p> : runs.length === 0 ? <p className="muted text-sm">No benchmark results yet.</p> : (
          <table className="w-full text-sm">
            <thead><tr className="muted text-left"><th>run</th><th>model</th><th>validator</th><th>replay top-1</th><th>synthetic top-1</th><th>top-3</th><th>Brier</th><th>false alarms</th></tr></thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.name} className="border-t border-[var(--gc-line)]">
                  <td className="mono">{r.name}</td><td>{String(r.config.model)}</td><td>{r.config.validator === false ? "off" : "on"}</td>
                  <td>{pct(r.summary.replay.top1)} ({r.summary.replay.n})</td>
                  <td>{pct(r.summary.synthetic.top1)} ({r.summary.synthetic.n})</td>
                  <td>{pct(r.summary.synthetic.top3)}</td>
                  <td>{r.summary.synthetic.brier?.toFixed(3) ?? "–"}</td>
                  <td>{pct(r.summary.null.false_alarm_rate)} ({r.summary.null.n})</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="muted mt-2 text-xs">Synthetic cases come from the same templates the system tests, so they partly measure self-agreement; the replayed real events are the independent signal.</p>
      </Panel>
      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title={`Confusion matrix${latest ? ` · ${latest.name}` : ""}`}>
          {hyps.length === 0 ? <p className="muted text-sm">–</p> : (
            <table className="text-xs">
              <thead><tr><th className="muted text-left">truth ↓ / predicted →</th>{hyps.map((h) => <th key={h} className="mono px-1">{h.split("_")[0]}</th>)}</tr></thead>
              <tbody>
                {Object.keys(confusion).sort().map((t) => (
                  <tr key={t}><td className="pr-2">{HYPOTHESIS_NAMES[t] ?? t}</td>{hyps.map((p) => {
                    const n = confusion[t]?.[p] ?? 0;
                    return <td key={p} className={`px-1 text-center ${n && t === p ? "ok font-semibold" : n ? "accent" : "muted"}`}>{n || "·"}</td>;
                  })}</tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
        <Panel title="Replay Winter Storm Uri">
          <p className="mb-2 text-sm">Runs the case live at 2021-02-16 12:00Z with a leak-free store and streams the trace.</p>
          <button onClick={replayUri} disabled={running} data-testid="replay-uri" className="rounded bg-amber-700 px-3 py-2 text-sm font-medium text-white hover:bg-amber-800 disabled:opacity-50">
            {running ? "Running…" : "Replay Uri"}
          </button>
          <ol className="mono mt-3 space-y-1 text-xs" data-testid="trace">
            {trace.map((e, i) => (
              <li key={i}><span className="muted">{e.kind}</span> {e.kind === "receipt" ? `${e.data.tool} → ${e.data.receipt_id}` : e.kind === "plan" ? `${(e.data.hypotheses as string[]).length} hypotheses, ${(e.data.steps as unknown[]).length} steps` : e.kind === "conclusion" ? `top ${e.data.top_hypothesis}` : e.kind === "error" ? <span className="accent">{String(e.data.message)}</span> : JSON.stringify(e.data).slice(0, 80)}</li>
            ))}
          </ol>
        </Panel>
      </div>
      {latest && latest.replay.length > 0 && (
        <Panel title="Replay cases">
          <table className="w-full text-sm">
            <thead><tr className="muted text-left"><th>case</th><th>entity</th><th>truth</th><th>predicted</th><th>top-1</th></tr></thead>
            <tbody>
              {latest.replay.map((c) => (
                <tr key={c.scenario_id} className="border-t border-[var(--gc-line)]">
                  <td className="mono">{c.scenario_id}</td><td>{c.entity}</td><td>{c.ground_truth[0]}</td>
                  <td>{c.predicted[0] ?? <span className="muted">error: {(c.error ?? "").slice(0, 40)}</span>}</td>
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

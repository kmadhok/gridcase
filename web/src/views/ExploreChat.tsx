import { useEffect, useRef, useState } from "react";
import { api, type ExploreAskBody, type ExploreAskResponse, type Receipt } from "../api";
import { Panel } from "../components";
import { filterList, windowOf, type ExploreState } from "../explore";
import { fmtTime } from "../labels";
import { AnswerSentence } from "./AskPanel";

type Item =
  | { kind: "turn"; question: string; answer?: ExploreAskResponse; error?: string }
  | { kind: "divider"; text: string };

const HISTORY_TURNS = 6;

/** The chat beside the chart: each question is turned into governed semantic-layer queries
 * about what is on screen, and every sentence is checked against their numbers. */
export function ExploreChat({
  state, context, flagged, suggestions, onInvestigate,
}: {
  state: ExploreState;
  context: string; // "Trading volume · Sports · last 3 days", in words
  flagged: string[];
  suggestions: string[];
  onInvestigate: (hour: string) => void;
}) {
  const [items, setItems] = useState<Item[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const shown = useRef(context);

  useEffect(() => {
    if (context === shown.current) return;
    shown.current = context;
    setItems((it) => (it.some((x) => x.kind === "turn") ? [...it, { kind: "divider", text: `Now looking at ${context}` }] : it));
  }, [context]);

  const submit = async () => {
    const q = question.trim();
    if (!q || busy) return;
    setBusy(true);
    setQuestion("");
    const history: ExploreAskBody["history"] = items
      .filter((x): x is Extract<Item, { kind: "turn" }> => x.kind === "turn" && !!x.answer)
      .slice(-HISTORY_TURNS)
      .map((t) => ({ question: t.question, queries: t.answer?.queries ?? [], answer: (t.answer?.sentences ?? []).map((s) => s.text) }));
    const body: ExploreAskBody = {
      question: q,
      page: { kpi: state.kpi, filters: filterList(state.filters), window: windowOf(state.hours), floor: state.floor, ceiling: state.ceiling, flagged_hours: flagged.slice(-24) },
      history,
    };
    try {
      const answer = await api.exploreAsk(body);
      setItems((it) => [...it, { kind: "turn", question: q, answer }]);
    } catch (e) {
      setItems((it) => [...it, { kind: "turn", question: q, error: e instanceof Error ? e.message : String(e) }]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel title="Ask about this chart" hint="Answers use only Polymarket data for what the chart shows, and follow-ups work (try “and Politics?”). Every sentence is checked against the numbers." className="flex min-w-0 flex-col">
      <p className="mb-3 text-xs"><span className="muted">Looking at:</span> <span className="font-medium">{context}</span></p>
      <ol className="max-h-[560px] space-y-3 overflow-y-auto" data-testid="chat">
        {items.map((it, i) =>
          it.kind === "divider" ? (
            <li key={i} className="muted text-center text-xs">— {it.text} —</li>
          ) : (
            <li key={i} className="rounded border border-[var(--gc-line)] p-3">
              <p className="mb-1 text-sm font-medium">{it.question}</p>
              <TurnAnswer item={it} onInvestigate={onInvestigate} />
            </li>
          ),
        )}
      </ol>
      <div className="mt-3 flex flex-wrap gap-2">
        {suggestions.map((s) => (
          <button key={s} type="button" onClick={() => setQuestion(s)} className="rounded-full border border-[var(--gc-line)] px-3 py-1 text-xs hover:border-amber-600">{s}</button>
        ))}
      </div>
      <form className="mt-3 flex gap-2" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
        <textarea
          aria-label="Your question"
          value={question}
          maxLength={500}
          rows={2}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void submit(); } }}
          placeholder="e.g. Which markets traded the most this week?"
          className="panel min-w-0 flex-1 p-2 text-sm"
        />
        <button type="submit" disabled={busy || !question.trim()} className="rounded bg-amber-700 px-4 py-2 text-sm font-medium text-white hover:bg-amber-800 disabled:opacity-50">
          {busy ? "Thinking…" : "Ask"}
        </button>
      </form>
    </Panel>
  );
}

function TurnAnswer({ item, onInvestigate }: { item: Extract<Item, { kind: "turn" }>; onInvestigate: (hour: string) => void }) {
  const a = item.answer;
  if (item.error) return <p className="accent text-sm">{item.error}</p>;
  if (!a) return null;
  if (a.refused) return <p className="muted text-sm">I can only answer questions about Polymarket trading on this page.</p>;
  if (a.investigate_hour) {
    const hour = a.investigate_hour;
    return (
      <div className="text-sm">
        <p className="muted">That's a "why" question, so it needs a full investigation of that hour.</p>
        <button type="button" onClick={() => onInvestigate(hour)} className="mt-2 rounded border border-[var(--gc-line)] px-3 py-1 text-xs font-medium hover:border-amber-600">
          Investigate {fmtTime(hour)}
        </button>
      </div>
    );
  }
  const pool: Record<string, Receipt> = Object.fromEntries([...a.new_receipts, ...(a.page_receipt ? [a.page_receipt] : [])].map((r) => [r.receipt_id, r]));
  return (
    <>
      {a.sentences.length === 0 && <p className="muted text-sm">The numbers here don't answer that.</p>}
      <ul>{a.sentences.map((s, j) => <AnswerSentence key={j} s={s} receipts={pool} />)}</ul>
      {a.dropped > 0 && (
        <p className="muted mt-1 text-xs">{a.dropped} sentence{a.dropped === 1 ? " was" : "s were"} removed because the numbers didn't match the evidence.</p>
      )}
    </>
  );
}

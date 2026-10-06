import { useState } from "react";
import { api, type AskResponse, type AskTarget, type Claim, type Receipt } from "../api";
import { Panel, TierBadge } from "../components";
import { fmtTime } from "../labels";
import { ReceiptCard } from "./ReceiptCard";

const CASE_SUGGESTIONS = [
  "Why was a broad shift ruled out?",
  "What does \"explains X% of the change\" mean?",
  "Were new traders involved?",
  "What should the team check next?",
];
const NOW_SUGGESTIONS = [
  "What's driving trading volume right now?",
  "Which category is busiest compared with usual?",
  "Are big trades unusually active this afternoon?",
];

interface Turn { question: string; answer?: AskResponse; error?: string }

export function AnswerSentence({ s, receipts }: { s: Claim; receipts: Record<string, Receipt> }) {
  const [open, setOpen] = useState(false);
  const cited = s.receipt_ids.map((id) => receipts[id]).filter(Boolean);
  return (
    <li className="py-1">
      <div className="flex items-start gap-2">
        <TierBadge tier={s.tier} />
        <span className="flex-1 text-sm">{s.text}</span>
        {cited.length > 0 && (
          <button className="muted text-xs underline" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
            {open ? "hide" : "show"} evidence ({cited.length})
          </button>
        )}
      </div>
      {open && cited.map((r) => <ReceiptCard key={r.receipt_id} r={r} />)}
    </li>
  );
}

export function AskPanel({ target, receipts, asOf }: { target: AskTarget; receipts: Record<string, Receipt>; asOf?: string }) {
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const isCase = "caseId" in target;
  const suggestions = isCase ? CASE_SUGGESTIONS : NOW_SUGGESTIONS;

  const submit = async () => {
    const q = question.trim();
    if (!q || busy) return;
    setBusy(true);
    setQuestion("");
    try {
      const answer = await api.ask(target, q);
      setTurns((t) => [...t, { question: q, answer }]);
    } catch (e) {
      setTurns((t) => [...t, { question: q, error: e instanceof Error ? e.message : String(e) }]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel
      title={isCase ? "Ask about this investigation" : "Ask about what's happening"}
      hint={isCase
        ? `Answers use only this investigation's evidence, plus the same read-only checks run on the data as it was known on ${fmtTime(asOf)}. Every sentence is checked against the numbers.`
        : "Answers use only live Polymarket data, compared with the same hours on previous days. Every sentence is checked against the numbers."}
    >
      <ol className="space-y-3" data-testid="ask-turns">
        {turns.map((t, i) => {
          const pool = { ...receipts, ...Object.fromEntries((t.answer?.new_receipts ?? []).map((r) => [r.receipt_id, r])) };
          return (
            <li key={i} className="rounded border border-[var(--gc-line)] p-3">
              <p className="mb-1 text-sm font-medium">{t.question}</p>
              {t.error && <p className="accent text-sm">{t.error}</p>}
              {t.answer?.refused && <p className="muted text-sm">I can only answer questions about {isCase ? "this investigation" : "Polymarket trading"}.</p>}
              {t.answer && !t.answer.refused && t.answer.sentences.length === 0 && <p className="muted text-sm">The evidence here doesn't answer that.</p>}
              <ul>{t.answer?.sentences.map((s, j) => <AnswerSentence key={j} s={s} receipts={pool} />)}</ul>
              {t.answer && t.answer.new_receipts.length > 0 && (
                <p className="muted mt-1 text-xs">Ran {t.answer.new_receipts.length} extra check{t.answer.new_receipts.length === 1 ? "" : "s"} to answer this.</p>
              )}
              {t.answer && t.answer.dropped > 0 && (
                <p className="muted mt-1 text-xs">{t.answer.dropped} sentence{t.answer.dropped === 1 ? " was" : "s were"} removed because the numbers didn't match the evidence.</p>
              )}
            </li>
          );
        })}
      </ol>
      <div className="mt-3 flex flex-wrap gap-2">
        {suggestions.map((s) => (
          <button key={s} data-testid="suggestion" onClick={() => setQuestion(s)} className="rounded-full border border-[var(--gc-line)] px-3 py-1 text-xs hover:border-amber-600">{s}</button>
        ))}
      </div>
      <form className="mt-3 flex gap-2" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
        <label className="sr-only" htmlFor="ask-box">Your question</label>
        <textarea
          id="ask-box"
          value={question}
          maxLength={500}
          rows={2}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void submit(); } }}
          placeholder={isCase ? "e.g. Was it one market or many?" : "e.g. Why is sports volume up today?"}
          className="panel min-w-0 flex-1 p-2 text-sm"
        />
        <button type="submit" disabled={busy || !question.trim()} className="rounded bg-amber-700 px-4 py-2 text-sm font-medium text-white hover:bg-amber-800 disabled:opacity-50">
          {busy ? "Thinking…" : "Ask"}
        </button>
      </form>
    </Panel>
  );
}

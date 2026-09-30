import { useEffect, useState } from "react";
import { api, type Digest, type DigestCard } from "../api";
import { fmtValue, kpiLabel, segmentName } from "../labels";
import { href, navigate } from "../router";
import {
  Concentration, DayBars, Heatmap, KindBars, MoverBars, Retention, ShareSlope, Swings, WatchStrip,
  type Cohort, type DayValue, type Mover, type ShareSegment, type Swing,
} from "./DigestCharts";
import { ReceiptCard } from "./ReceiptCard";

const WIDE = new Set(["odds_swings", "rhythm", "alarms"]);

interface AlarmCase { case_id: string; kpi: string; kpi_label: string; segment: string; as_of: string; explanation: string | null }

function Chart({ card, digest }: { card: DigestCard; digest: Digest }) {
  const d = card.data;
  switch (card.id) {
    case "share_shift":
      return <ShareSlope segments={d.segments as ShareSegment[]} gainer={String(d.top_gainer)} />;
    case "movers":
      return (d.markets as Mover[]).length ? <MoverBars markets={d.markets as Mover[]} /> : null;
    case "odds_swings":
      return (d.swings as Swing[]).length ? <Swings swings={d.swings as Swing[]} /> : null;
    case "whales":
      return (d.days as DayValue[]).length ? (
        <DayBars days={d.days as DayValue[]} highlight={d.peak_day as string} mean={d.mean as number} label="Share of each day's volume from trades of $1,000 or more" />
      ) : null;
    case "retention":
      return (d.days as Cohort[]).length ? <Retention days={d.days as Cohort[]} /> : null;
    case "rhythm":
      return <Heatmap grid={d.grid as { weekday: string; hours: (number | null)[] }[]} peak={d.peak as { weekday: string; hour: number } | undefined} />;
    case "concentration":
      return d.markets ? <Concentration markets={d.markets as number} forHalf={d.markets_for_half as number} /> : null;
    case "kind":
      return (d.days as DayValue[]).length ? <KindBars days={d.days as DayValue[]} /> : null;
    case "alarms": {
      const cases = d.cases as AlarmCase[];
      return cases.length ? <WatchStrip from={digest.windows.yesterday[0]} alarmHours={cases.map((c) => new Date(`${c.as_of}Z`).getUTCHours())} /> : null;
    }
    default:
      return null;
  }
}

function Card({ card, digest }: { card: DigestCard; digest: Digest }) {
  const [open, setOpen] = useState(false);
  const receipts = digest.receipts.filter((r) => card.sentence.receipt_ids.includes(r.receipt_id));
  const cases = card.id === "alarms" ? (card.data.cases as AlarmCase[]) : [];
  return (
    <article className={`panel flex min-w-0 flex-col gap-2 px-5 py-4 ${WIDE.has(card.id) ? "md:col-span-2" : ""}`} data-testid={`card-${card.id}`}>
      <p className="muted m-0 text-sm">{card.question}</p>
      <p className="m-0 text-[17px] font-semibold leading-snug text-pretty">{card.sentence.text}</p>
      {!card.insufficient && <Chart card={card} digest={digest} />}
      {cases.length > 0 && (
        <ul className="m-0 flex list-none flex-col gap-2 p-0">
          {cases.map((c) => (
            <li key={c.case_id} className="grid grid-cols-[56px_minmax(0,1fr)] gap-3 text-sm">
              <time className="muted tabular-nums" dateTime={c.as_of}>{c.as_of.slice(11, 16)}</time>
              <a className="underline underline-offset-2" href={href({ view: "case", id: c.case_id })} title={c.kpi}>
                {c.kpi_label}{c.segment !== "all" ? ` in ${segmentName(c.segment)}` : ""}
                {c.explanation ? `: best explained by ${c.explanation}` : ""}
              </a>
            </li>
          ))}
        </ul>
      )}
      <div className="muted mt-auto flex items-center justify-between gap-2 border-t border-[var(--gc-line)] pt-2 text-xs">
        <span>{card.note}</span>
        {receipts.length > 0 && (
          <button type="button" className="shrink-0 underline underline-offset-2" style={{ color: "var(--gc-ink)" }} onClick={() => setOpen((o) => !o)} aria-expanded={open}>
            {open ? "Hide the evidence" : "Show the evidence"}
          </button>
        )}
      </div>
      {open && receipts.map((r) => <ReceiptCard key={r.receipt_id} r={r} />)}
    </article>
  );
}

function Totals({ digest }: { digest: Digest }) {
  const prev = digest.period_words.previous;
  return (
    <section className="grid grid-cols-2 gap-3 md:grid-cols-4" aria-label="The period in four numbers" data-testid="totals">
      {digest.totals.items.map((t) => {
        const value = fmtValue(t.kpi === "volume_usd" ? "usd" : "count", t.now);
        const change = t.pct_change === null
          ? "no earlier period yet"
          : `${t.pct_change >= 0 ? "+" : "−"}${Math.abs(Math.round(t.pct_change))}% on ${prev}`;
        return (
          <div key={t.kpi} className="panel px-4 py-3" title={t.kpi}>
            <div className="muted text-xs">{kpiLabel(t.kpi)}</div>
            <div className="text-2xl font-semibold tabular-nums">{value}</div>
            <div className="muted text-xs">{change}</div>
          </div>
        );
      })}
    </section>
  );
}

function longDay(day: string): string {
  return new Date(`${day}T00:00:00Z`).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}

export function DigestView({ day, initial }: { day?: string; initial?: Digest }) {
  const [digest, setDigest] = useState<Digest | null>(initial ?? null);
  const [days, setDays] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (initial) return;
    api.digest(day).then((d) => { setError(null); setDigest(d); }).catch(() => setError(
      day ? `There is no morning read for ${day}.` : "The first morning read appears after midnight UTC, once a full day has been collected.",
    ));
    api.digests().then((l) => setDays(l.map((x) => x.day))).catch(() => setDays([]));
  }, [day, initial]);

  if (error) return <p className="muted">{error}</p>;
  if (!digest) return <p className="muted">Loading the morning read…</p>;

  const sections = [...new Set(digest.cards.map((c) => c.section))];
  const gaps = digest.coverage && (digest.coverage.gap_minutes > 0 || digest.coverage.empty_hours > 0);
  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-col gap-2">
        <div className="muted flex flex-wrap items-center gap-x-3 gap-y-1 text-xs uppercase tracking-wider">
          <span>Morning read · {longDay(digest.day)} · covers {digest.period_words.current}, to 00:00 UTC</span>
          {days.length > 1 && (
            <label className="normal-case tracking-normal">
              <span className="sr-only">Choose a day</span>
              <select id="digest-day" className="rounded border border-[var(--gc-line)] bg-transparent px-1 py-0.5" value={digest.day} onChange={(e) => navigate({ view: "digest", day: e.target.value })}>
                {days.map((d) => <option key={d} value={d}>{longDay(d)}</option>)}
              </select>
            </label>
          )}
        </div>
        <h1 className="m-0 max-w-[26ch] text-balance text-3xl font-semibold leading-tight md:text-[34px]">
          {digest.headline?.text ?? "The morning read"}
        </h1>
        {digest.totals.sentence && <p className="muted m-0 max-w-prose">{digest.totals.sentence.text}</p>}
        {gaps && (
          <p className="m-0 text-sm" style={{ color: "var(--gc-accent)" }}>
            Our collector missed some trading in this period, so some numbers may be a little low.
          </p>
        )}
      </header>
      <Totals digest={digest} />
      {sections.map((s) => (
        <section key={s} className="flex flex-col gap-3">
          <h2 className="muted m-0 text-[13px] font-semibold uppercase tracking-wider">{s}</h2>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {digest.cards.filter((c) => c.section === s).map((c) => <Card key={c.id} card={c} digest={digest} />)}
          </div>
        </section>
      ))}
    </div>
  );
}

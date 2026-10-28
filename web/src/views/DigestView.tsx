import { useEffect, useState } from "react";
import { api, type Digest, type DigestCard, type Receipt } from "../api";
import { fmtValue, kpiLabel, segmentName } from "../labels";
import { href, navigate } from "../router";
import {
  Concentration, DayBars, Heatmap, KindBars, MoverBars, Retention, ShareSlope, Swings, WatchStrip,
  type Cohort, type DayValue, type Mover, type ShareSegment, type Swing,
} from "./DigestCharts";
import { HowCalculatedButton, HowCalculatedPanel, type Calculation, type Period } from "./HowCalculated";
import { ReceiptCard } from "./ReceiptCard";
import { SunriseField } from "./fields/SunriseField";

const PERIOD_WORDS: Record<string, string> = { window: "Period", current: "This period", previous: "Compared with" };

/** What a card's checks computed, read from their receipts: the KPIs they name, their periods
 * and their exact queries. */
function calculation(receipts: Receipt[], question: string): Calculation {
  const kpis = new Set<string>();
  const periods: Period[] = [];
  for (const r of receipts) {
    if (typeof r.inputs.kpi === "string") kpis.add(r.inputs.kpi);
    if (Array.isArray(r.inputs.kpis)) for (const k of r.inputs.kpis) kpis.add(String(k));
    for (const [key, label] of Object.entries(PERIOD_WORDS)) {
      const w = r.inputs[key];
      if (Array.isArray(w) && w.length === 2 && !periods.some((p) => p.label === label)) periods.push({ label, start: String(w[0]), end: String(w[1]) });
    }
  }
  return {
    kpis: [...kpis],
    periods,
    asOf: receipts[0]?.as_of ?? null,
    queries: receipts.flatMap((r) => r.sql.map((q) => q.sql)),
    direct: `This check reads trades directly to answer "${question}", with the query below, rather than one KPI.`,
  };
}

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
  const [how, setHow] = useState(false);
  const receipts = digest.receipts.filter((r) => card.sentence.receipt_ids.includes(r.receipt_id));
  const cases = card.id === "alarms" ? (card.data.cases as AlarmCase[]) : [];
  return (
    <article className={`panel flex min-w-0 flex-col gap-3 rounded-3xl p-5 md:p-6 ${WIDE.has(card.id) ? "md:col-span-2" : ""}`} data-testid={`card-${card.id}`}>
      <p className="label m-0">{card.question}</p>
      <p className="display m-0 text-xl leading-snug tracking-[-0.02em] text-pretty">{card.sentence.text}</p>
      {!card.insufficient && <Chart card={card} digest={digest} />}
      {cases.length > 0 && (
        <ul className="m-0 flex list-none flex-col gap-2 p-0">
          {cases.map((c) => (
            <li key={c.case_id} className="grid grid-cols-[56px_minmax(0,1fr)] gap-3 text-sm text-ink-soft">
              <time className="tabular text-ink-muted" dateTime={c.as_of}>{c.as_of.slice(11, 16)}</time>
              <a className="link-ink" href={href({ view: "case", id: c.case_id })} title={c.kpi}>
                {c.kpi_label}{c.segment !== "all" ? ` in ${segmentName(c.segment)}` : ""}
                {c.explanation ? `: best explained by ${c.explanation}` : ""}
              </a>
            </li>
          ))}
        </ul>
      )}
      <div className="mt-auto flex flex-wrap items-center justify-between gap-x-3 gap-y-2 border-t border-dashed border-rule pt-3 text-xs text-ink-muted">
        <span>{card.note}</span>
        {receipts.length > 0 && (
          <span className="flex shrink-0 flex-wrap gap-x-4 gap-y-1">
            <HowCalculatedButton open={how} onToggle={() => setHow((o) => !o)} />
            <button type="button" className="link-ink shrink-0 text-ink" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
              {open ? "Hide the evidence" : "Show the evidence"}
            </button>
          </span>
        )}
      </div>
      {how && <HowCalculatedPanel {...calculation(receipts, card.question)} />}
      {open && receipts.map((r) => <ReceiptCard key={r.receipt_id} r={r} />)}
    </article>
  );
}

function Totals({ digest }: { digest: Digest }) {
  const [how, setHow] = useState(false);
  const ids = digest.totals.sentence?.receipt_ids ?? [];
  const receipts = digest.receipts.filter((r) => ids.includes(r.receipt_id));
  return (
    <section data-testid="totals-section" aria-label="The period in four numbers">
      <TotalsGrid digest={digest} />
      {receipts.length > 0 && (
        <div className="mt-3 flex justify-end text-xs"><HowCalculatedButton open={how} onToggle={() => setHow((o) => !o)} /></div>
      )}
      {how && <HowCalculatedPanel {...calculation(receipts, "the period in four numbers")} />}
    </section>
  );
}

function TotalsGrid({ digest }: { digest: Digest }) {
  const prev = digest.period_words.previous;
  return (
    <section className="grid grid-cols-2 gap-3 md:grid-cols-4" data-testid="totals">
      {digest.totals.items.map((t) => {
        const value = fmtValue(t.kpi === "volume_usd" ? "usd" : "count", t.now);
        const change = t.pct_change === null
          ? "no earlier period yet"
          : `${t.pct_change >= 0 ? "+" : "−"}${Math.abs(Math.round(t.pct_change))}% on ${prev}`;
        return (
          <div key={t.kpi} className="panel p-4 md:p-5" title={t.kpi}>
            <div className="label">{kpiLabel(t.kpi)}</div>
            <div className="display tabular mt-3 text-[28px] leading-none md:text-[40px]">{value}</div>
            <div className={`mt-2 text-xs ${t.pct_change === null ? "text-ink-muted" : "text-ink-soft"}`}>{change}</div>
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

  if (error) return <p className="text-ink-muted">{error}</p>;
  if (!digest) return <p className="text-ink-muted">Loading the morning read…</p>;

  const sections = [...new Set(digest.cards.map((c) => c.section))];
  const gaps = digest.coverage && (digest.coverage.gap_minutes > 0 || digest.coverage.empty_hours > 0);
  return (
    <div className="flex flex-col gap-8">
      <header className="flex flex-col gap-4">
        <SunriseField digest={digest} />
        <div className="label flex flex-wrap items-center gap-x-3 gap-y-2">
          <span>Morning read · {longDay(digest.day)} · covers {digest.period_words.current}, to 00:00 UTC</span>
          {days.length > 1 && (
            <label className="normal-case tracking-normal">
              <span className="sr-only">Choose a day</span>
              <select id="digest-day" className="cursor-pointer rounded-full border border-ink/25 bg-sheet px-2.5 py-1 text-xs font-normal text-ink-soft transition-colors duration-150 hover:border-accent" value={digest.day} onChange={(e) => navigate({ view: "digest", day: e.target.value })}>
                {days.map((d) => <option key={d} value={d}>{longDay(d)}</option>)}
              </select>
            </label>
          )}
        </div>
        <h1 className="display m-0 max-w-[24ch] text-balance text-[34px] leading-[1.05] md:text-5xl">
          {digest.headline?.text ?? "The morning read"}
        </h1>
        {digest.totals.sentence && <p className="m-0 max-w-prose text-[15px] leading-relaxed text-ink-soft md:text-base">{digest.totals.sentence.text}</p>}
        {gaps && (
          <p className="warn m-0 text-sm">
            Our collector missed some trading in this period, so some numbers may be a little low.
          </p>
        )}
      </header>
      <Totals digest={digest} />
      {sections.map((s) => (
        <section key={s} className="flex flex-col gap-4">
          <h2 className="display m-0 pt-4 text-[28px] leading-tight md:text-[32px]">{s}</h2>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {digest.cards.filter((c) => c.section === s).map((c) => <Card key={c.id} card={c} digest={digest} />)}
          </div>
        </section>
      ))}
    </div>
  );
}

import type { ReactNode } from "react";
import type { Tier } from "./api";
import { HYPOTHESES, STATUS, TIERS } from "./labels";

const TONE = {
  good: "bg-emerald-100 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-200",
  bad: "bg-rose-100 text-rose-900 dark:bg-rose-900/40 dark:text-rose-200",
  neutral: "bg-zinc-200 text-zinc-800 dark:bg-zinc-700 dark:text-zinc-200",
  info: "bg-sky-100 text-sky-900 dark:bg-sky-900/40 dark:text-sky-200",
};

export function TierBadge({ tier }: { tier: Tier }) {
  const t = TIERS[tier] ?? { label: tier, blurb: "" };
  const cls = tier === "governed" ? TONE.good : tier === "derived" ? TONE.info : TONE.neutral;
  return <span title={`${t.blurb} (${tier})`} className={`rounded px-2 py-0.5 text-xs font-medium ${cls}`}>{t.label}</span>;
}

export function StatusBadge({ status }: { status: string }) {
  const s = STATUS[status] ?? { label: status, tone: "neutral" as const };
  return <span title={status} className={`whitespace-nowrap rounded px-2 py-0.5 text-xs font-medium ${TONE[s.tone]}`}>{s.label}</span>;
}

export function HypothesisName({ id }: { id: string }) {
  const h = HYPOTHESES[id];
  return <span title={id} className="font-medium">{h?.name ?? id}</span>;
}

export function Panel({ title, children, className = "", hint }: { title?: string; children: ReactNode; className?: string; hint?: string }) {
  return (
    <section className={`panel p-4 ${className}`}>
      {title && <h2 className="mb-1 text-sm font-semibold uppercase tracking-wide muted">{title}</h2>}
      {hint && <p className="muted mb-3 text-xs">{hint}</p>}
      {children}
    </section>
  );
}

export function fmt(n: number | null | undefined, digits = 0): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "–";
  return n.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

export function pct(x: number | null | undefined): string {
  return x === null || x === undefined ? "–" : `${Math.round(100 * x)}%`;
}

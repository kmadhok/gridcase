import type { ReactNode } from "react";
import type { Tier } from "./api";
import { HYPOTHESES, STATUS, TIERS } from "./labels";

// Status badges: a currentColor outline on the card's own fill (ok green, accent red, slate, muted).
const TONE = {
  good: "border-current text-ok",
  bad: "border-current text-accent",
  neutral: "border-ink/25 text-ink-muted",
  info: "border-current text-slate",
  warn: "border-current text-warn",
};

export function TierBadge({ tier }: { tier: Tier }) {
  const t = TIERS[tier] ?? { label: tier, blurb: "" };
  const cls = tier === "governed" ? TONE.good : tier === "derived" ? TONE.info : TONE.neutral;
  return <span title={`${t.blurb} (${tier})`} className={`pill ${cls}`}>{t.label}</span>;
}

export function StatusBadge({ status }: { status: string }) {
  const s = STATUS[status] ?? { label: status, tone: "neutral" as const };
  return <span title={status} className={`pill ${TONE[s.tone]}`}>{s.label}</span>;
}

export function HypothesisName({ id }: { id: string }) {
  const h = HYPOTHESES[id];
  return <span title={id} className="font-medium">{h?.name ?? id}</span>;
}

export function Panel({ title, children, className = "", hint }: { title?: string; children: ReactNode; className?: string; hint?: string }) {
  return (
    <section className={`panel rounded-3xl p-5 md:p-6 ${className}`}>
      {title && <h2 className="label mb-1.5">{title}</h2>}
      {hint && <p className="mb-4 text-[13px] leading-snug text-ink-muted">{hint}</p>}
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

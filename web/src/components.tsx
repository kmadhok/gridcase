import type { ReactNode } from "react";
import { HYPOTHESIS_NAMES, type Status, type Tier } from "./api";

export function TierBadge({ tier }: { tier: Tier }) {
  const cls =
    tier === "governed"
      ? "bg-emerald-100 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-200"
      : tier === "derived"
        ? "bg-sky-100 text-sky-900 dark:bg-sky-900/40 dark:text-sky-200"
        : "bg-zinc-200 text-zinc-800 dark:bg-zinc-700 dark:text-zinc-200";
  return <span className={`rounded px-2 py-0.5 text-xs font-medium ${cls}`}>{tier}</span>;
}

export function StatusBadge({ status }: { status: Status | string }) {
  const cls =
    status === "supported" || status === "confirmed"
      ? "bg-emerald-100 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-200"
      : status === "refuted"
        ? "bg-rose-100 text-rose-900 dark:bg-rose-900/40 dark:text-rose-200"
        : "bg-zinc-200 text-zinc-800 dark:bg-zinc-700 dark:text-zinc-200";
  return <span className={`rounded px-2 py-0.5 text-xs font-medium ${cls}`}>{status}</span>;
}

export function HypothesisName({ id }: { id: string }) {
  return (
    <span>
      <span className="font-medium">{HYPOTHESIS_NAMES[id] ?? id}</span>{" "}
      <span className="muted mono text-xs">{id}</span>
    </span>
  );
}

export function Panel({ title, children, className = "" }: { title?: string; children: ReactNode; className?: string }) {
  return (
    <section className={`panel p-4 ${className}`}>
      {title && <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide muted">{title}</h2>}
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

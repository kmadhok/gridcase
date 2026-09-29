// Plain-language labels for everything the engine names with an id. The ids stay available
// in title attributes for engineers; they never appear as visible text (see plain_language test).

export const KPIS: Record<string, { label: string; unit: "usd" | "count" | "share" }> = {
  volume_usd: { label: "Trading volume", unit: "usd" },
  trades: { label: "Trades", unit: "count" },
  active_traders: { label: "Active traders", unit: "count" },
  new_traders: { label: "New traders", unit: "count" },
  avg_trade_usd: { label: "Average trade size", unit: "usd" },
  returning_share: { label: "Returning-trader share", unit: "share" },
};

export const kpiLabel = (id: string): string => KPIS[id]?.label ?? id.replace(/_/g, " ");

export const HYPOTHESES: Record<string, { name: string; short: string; blurb: string }> = {
  D0_DATA_PROBLEM: { name: "Data problem", short: "Data", blurb: "Our collector missed trades or the feed stalled, so the numbers aren't real movement." },
  D1_ONE_MARKET: { name: "One market", short: "Market", blurb: "A single market accounts for most of the change: news, a live match, a resolution." },
  D2_ONE_CATEGORY: { name: "One category", short: "Category", blurb: "One category (sports, politics, crypto…) accounts for most of the change." },
  D3_NEW_TRADERS: { name: "New traders", short: "New", blurb: "Wallets new to Polymarket drive the change: an acquisition wave, or its absence." },
  D4_WHALES: { name: "A few big trades", short: "Whales", blurb: "Trades of $1k and up drive the change, not ordinary traders." },
  D5_LONGSHOTS: { name: "Long-shot bets", short: "Long shots", blurb: "Bets on outcomes priced under 20% drive the change." },
  D6_BROAD_BASED: { name: "A broad shift", short: "Broad", blurb: "The change is spread across many markets: a platform-wide shift." },
};

export const hypothesisName = (id: string): string => HYPOTHESES[id]?.name ?? id;

export const STATUS: Record<string, { label: string; tone: "good" | "bad" | "neutral" }> = {
  supported: { label: "evidence supports", tone: "good" },
  confirmed: { label: "confirmed on follow-up", tone: "good" },
  refuted: { label: "ruled out", tone: "bad" },
  superseded: { label: "overtaken on follow-up", tone: "neutral" },
  open: { label: "couldn't test yet", tone: "neutral" },
};

export const TIERS: Record<string, { label: string; blurb: string }> = {
  governed: { label: "measured", blurb: "read straight from the data, with the exact query attached" },
  derived: { label: "computed", blurb: "calculated from measured numbers" },
  inferred: { label: "interpretation", blurb: "the writer's judgement; no evidence link" },
};

export const CASE_STATES: Record<string, string> = {
  detected: "opened",
  planned: "planned",
  executing: "investigating",
  concluded: "concluded",
  action_taken: "published",
  rechecking: "re-checking",
  closed: "closed",
};

/** 'category=Sports' -> 'Sports'; 'all' -> 'All markets'. */
export function segmentName(segment: string): string {
  if (!segment || segment === "all") return "All markets";
  return segment
    .split(";") // not '&': a category is called "Economy & Finance"
    .map((part) => part.split("=")[1] ?? part)
    .join(", ");
}

/** The question each investigation step answers, in words a non-analyst can read. */
export function stepQuestion(tool: string, inputs: Record<string, unknown> = {}): string {
  const within = Object.values((inputs.filters as Record<string, string>) ?? {}).join(", ");
  const suffix = within ? ` (within ${within})` : "";
  switch (tool) {
    case "kpi.compare": return "How big was the change?" + suffix;
    case "data.coverage": return "Is our data complete?";
    case "by.market": return "Which market drove it?" + suffix;
    case "by.category": return "Which category drove it?" + suffix;
    case "by.size": return "Big trades or small ones?" + suffix;
    case "by.trader": return "New traders or returning ones?" + suffix;
    case "by.odds": return "Long shots or favourites?" + suffix;
    case "kpi.alarm": return "Why was this investigation opened?";
    default: return tool;
  }
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "–";
  const d = new Date(iso.endsWith("Z") ? iso : iso + "Z");
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "UTC" }) + " UTC";
}

export function fmtValue(unit: string, v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  if (unit === "usd") {
    if (Math.abs(v) >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}M`;
    if (Math.abs(v) >= 10_000) return `$${Math.round(v / 1000)}k`;
    return `$${v.toLocaleString(undefined, { maximumFractionDigits: v < 100 ? 2 : 0 })}`;
  }
  if (unit === "share") return `${Math.round(100 * v)}%`;
  return Math.round(v).toLocaleString();
}

// Plain-language labels for everything the engine names with an id. The ids stay available
// in title attributes for engineers; they never appear as visible text (see plain_language test).

export const KPIS: Record<string, { label: string; unit: "usd" | "count" | "share" | "ratio" }> = {
  volume_usd: { label: "Trading volume", unit: "usd" },
  trades: { label: "Trades", unit: "count" },
  active_traders: { label: "Active traders", unit: "count" },
  new_traders: { label: "New traders", unit: "count" },
  avg_trade_usd: { label: "Average trade size", unit: "usd" },
  returning_share: { label: "Returning-trader share", unit: "share" },
  markets_traded: { label: "Markets traded", unit: "count" },
  new_markets: { label: "New markets", unit: "count" },
  trades_per_trader: { label: "Trades per trader", unit: "ratio" },
  whale_share: { label: "Whale share of volume", unit: "share" },
  sure_thing_share: { label: "Sure-thing share", unit: "share" },
  buy_share: { label: "Buy share", unit: "share" },
  longshot_share: { label: "Long-shot share", unit: "share" },
  recurring_share: { label: "Recurring-market share", unit: "share" },
  volume_share: { label: "Share of volume", unit: "share" },
  odds_flips: { label: "Odds flips", unit: "count" },
  whale_count: { label: "Whale wallets", unit: "count" },
  top_market_share: { label: "Top market's share", unit: "share" },
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
  const within = Object.values((inputs.filters as Record<string, string | string[]>) ?? {})
    .map((v) => (Array.isArray(v) ? v.join(" or ") : v))
    .join(", ");
  const suffix = within ? ` (within ${within})` : "";
  switch (tool) {
    case "kpi.compare": return "How big was the change?" + suffix;
    case "data.coverage": return "Is our data complete?";
    case "by.market": return "Which market drove it?" + suffix;
    case "by.category": return "Which category drove it?" + suffix;
    case "by.size": return "Big trades or small ones?" + suffix;
    case "by.trader": return "New traders or returning ones?" + suffix;
    case "by.odds": return "Long shots or favourites?" + suffix;
    case "by.kind": return "Recurring 5-minute markets or real events?" + suffix;
    case "kpi.alarm": return "Why was this investigation opened?";
    case "digest.totals": return "How did the period compare with the one before?";
    case "digest.share_shift": return "Which category gained share of volume?";
    case "digest.movers": return "Which markets came out of nowhere?";
    case "digest.odds_swings": return "Whose odds moved the most?";
    case "digest.by_day": return `${kpiLabel(String(inputs.kpi ?? ""))}, day by day`;
    case "digest.retention": return "Did new traders come back the next day?";
    case "digest.rhythm": return "When is trading busiest?";
    case "digest.concentration": return "How few markets hold half the money?";
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
  if (unit === "ratio") return v.toFixed(2);
  return Math.round(v).toLocaleString();
}

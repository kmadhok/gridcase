// Plain-language labels for everything the engine names with an id. The ids stay available
// in title attributes for engineers; they never appear as visible text (see plain_language test).

export const REGION_NAMES: Record<string, string> = {
  ERCO: "ERCOT (Texas)",
  CISO: "CAISO (California)",
  PJM: "PJM (Mid-Atlantic)",
  US48: "US Lower 48",
};

export const HYPOTHESES: Record<string, { name: string; short: string; blurb: string }> = {
  H0_DATA_ARTIFACT: { name: "Bad data", short: "Bad data", blurb: "The numbers themselves are wrong, missing or later revised." },
  H1_WEATHER_DEMAND: { name: "Weather-driven demand", short: "Weather", blurb: "Temperature moved demand more than the forecast assumed." },
  H2_SUPPLY_SHORTFALL: { name: "Supply shortfall", short: "Supply", blurb: "Power plants delivered far less than planned." },
  H3_IMPORT_CONSTRAINT: { name: "Imports maxed out", short: "Imports", blurb: "The lines to neighbouring grids were at their limit." },
  H4_LOAD_SHED: { name: "Load shed", short: "Load shed", blurb: "Customers were deliberately cut off to protect the grid." },
  H5_SUBREGION_CONCENTRATION: { name: "One area drove it", short: "One area", blurb: "The miss came from a single zone, not the whole grid." },
  H6_RENEWABLE_FORECAST_MISS: { name: "Wind or solar came in far off", short: "Wind/solar", blurb: "Renewable output was far from what was forecast." },
  H7_CALENDAR: { name: "Holiday effect", short: "Holiday", blurb: "The forecast treated a holiday like a normal day." },
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
  open: "opened",
  planned: "planned",
  executing: "investigating",
  concluded: "concluded",
  action_taken: "published",
  rechecking: "re-checking",
  closed: "closed",
};

export const METRICS: Record<string, string> = {
  forecast_error_mw: "demand vs. forecast",
  forecast_error_pct: "demand vs. forecast",
  demand: "demand",
  demand_forecast: "forecast",
  net_generation: "generation",
  supply_gap_mw: "generation vs. forecast",
  net_interchange: "imports and exports",
  balance_residual: "supply/demand balance",
  wind_capacity_factor_proxy: "wind output",
  solar_capacity_factor_proxy: "solar output",
  temp_load_center: "temperature",
};

export const metricName = (id: string): string => METRICS[id] ?? id.replace(/_/g, " ");

/** The question each investigation step answers, in words a non-engineer can read. */
export function stepQuestion(tool: string, inputs: Record<string, unknown> = {}): string {
  const metric = String(inputs.metric ?? "");
  switch (tool) {
    case "identity.balance": return "Do the numbers add up?";
    case "revision.diff": return "Was the data revised later?";
    case "regress.temperature": return "How much does weather explain?";
    case "peer.compare": return "Did neighbouring grids see it too?";
    case "bridge.fuel": return "Which power sources moved?";
    case "price.co_movement": return "Did prices spike?";
    case "variance.plan_vs_actual": return "How big was the miss?";
    case "contribution.subregion": return "Was one area driving it?";
    case "baseline.expected": return "What was normal for this hour?";
    case "kpi.alarm": return "Why was this investigation opened?";
    case "anomaly.detect":
      if (metric === "supply_gap_mw") return "Was generation short of the forecast?";
      if (metric === "net_interchange") return "Were imports maxed out?";
      if (metric === "balance_residual") return "Do the numbers add up?";
      if (metric.startsWith("wind")) return "Did wind under-deliver?";
      if (metric.startsWith("solar")) return "Did solar under-deliver?";
      if (metric.startsWith("forecast_error")) return "Was the miss abnormal?";
      return metric ? `Was ${metricName(metric)} abnormal?` : "Was anything abnormal?";
    default:
      return tool;
  }
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "–";
  const d = new Date(iso.endsWith("Z") ? iso : iso + "Z");
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-GB", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "UTC" }) + " UTC";
}

export function errorPhrase(pct: number | null | undefined): string {
  if (pct === null || pct === undefined) return "no data";
  const a = Math.abs(pct).toFixed(1);
  return pct < 0 ? `${a}% below forecast` : `${a}% above forecast`;
}

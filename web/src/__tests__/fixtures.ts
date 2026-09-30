import type { CaseFull, NowResponse, Receipt, SeriesView, CatalogResponse } from "../api";

export const RECEIPT: Receipt = {
  receipt_id: "r1", tool: "by.market", inputs: { kpi: "volume_usd", filters: {} }, as_of: "2026-10-05T14:20:00",
  data_version: null, output: { top_segment: "England vs Czechia", top_share: 0.72, top_change: 14194 },
  effect_size: 0.72, runtime_ms: 12, sql: [{ sql: "WITH win AS (SELECT 1) SELECT v FROM win", rows_hash: "abc" }],
};

export const CASE: CaseFull = {
  case_id: "all-20261005T14-abc",
  entity: "all",
  metric: "volume_usd",
  as_of: "2026-10-05T14:20:00",
  opened_at: "2026-10-05T14:20:00",
  state: "action_taken",
  season: "fall",
  top_hypothesis: "D1_ONE_MARKET",
  llm_calls: 2,
  cost_usd: 0.31,
  plan: {
    hypotheses: ["D0_DATA_PROBLEM", "D1_ONE_MARKET", "D6_BROAD_BASED"],
    steps: [
      { step_id: "s1", tool: "data.coverage", inputs: {}, hypothesis_ids: ["D0_DATA_PROBLEM"], depends_on: [] },
      { step_id: "s2", tool: "by.market", inputs: { kpi: "volume_usd" }, hypothesis_ids: ["D1_ONE_MARKET", "D6_BROAD_BASED"], depends_on: [] },
    ],
    budget_steps: 12,
    source: "llm",
  },
  plan_attempts: 1,
  conclusion: {
    case_id: "all-20261005T14-abc",
    top_hypothesis: "D1_ONE_MARKET",
    ranked: [
      { id: "D1_ONE_MARKET", status: "supported", confidence: 0.68, margin: 1, coverage: 0.72 },
      { id: "D6_BROAD_BASED", status: "refuted", confidence: 0.12, margin: -1, coverage: 1 },
      { id: "D0_DATA_PROBLEM", status: "refuted", confidence: 0.04, margin: -1, coverage: 1 },
    ],
    claims: [
      { text: "Broken down by market, England vs Czechia accounts for 72% of the change ($14,194).", receipt_ids: ["r1"], tier: "governed" },
      { text: "The best-supported explanation is one market.", receipt_ids: [], tier: "inferred" },
    ],
    caveats: [],
  },
  receipts: [RECEIPT],
  verdicts: {},
  step_errors: {},
  recheck: { as_of: "2026-10-06T14:20:00", verdicts: {}, diffs: { D1_ONE_MARKET: { before: "supported", after: "confirmed" } }, new_top: "D1_ONE_MARKET" },
};

const points = (base: number, spike: number) =>
  Array.from({ length: 48 }, (_, i) => ({
    t: new Date(Date.UTC(2026, 9, 3, 14 + i)).toISOString().slice(0, 19),
    value: i >= 46 ? base * spike : base,
    usual: base, low: base * 0.8, high: base * 1.2,
  }));

export const series = (over: Partial<SeriesView>): SeriesView => ({
  series: "volume_usd", kpi: "volume_usd", label: "Trading volume", unit: "usd", segment: "all", filters: {},
  status: "normal", direction: null, ratio: 1.0, history_days: 5,
  latest: { t: "2026-10-05T13:00:00", value: 1_200_000, usual: 1_150_000 }, points: points(1_150_000, 1), ...over,
});

export const NOW: NowResponse = {
  as_of: "2026-10-05T14:20:00",
  series: [
    series({}),
    series({ series: "active_traders", kpi: "active_traders", label: "Active traders", unit: "count", latest: { t: "2026-10-05T13:00:00", value: 5200, usual: 5000 }, points: points(5000, 1) }),
    series({ series: "new_traders", kpi: "new_traders", label: "New traders", unit: "count", status: "learning", history_days: 1, latest: { t: "2026-10-05T13:00:00", value: 300, usual: null }, points: points(300, 1) }),
    series({ series: "whale_share", kpi: "whale_share", label: "Whale share of volume", unit: "share", group: "behaviour", latest: { t: "2026-10-05T13:00:00", value: 0.54, usual: 0.5 }, points: points(0.5, 1) }),
    series({ series: "trades_per_trader", kpi: "trades_per_trader", label: "Trades per trader", unit: "ratio", group: "behaviour", latest: { t: "2026-10-05T13:00:00", value: 7.95, usual: 8.1 }, points: points(8.1, 1) }),
    series({ series: "volume_usd · category=Sports", segment: "category=Sports", filters: { category: "Sports" }, status: "alarm", direction: "up", ratio: 3.1, latest: { t: "2026-10-05T13:00:00", value: 2_400_000, usual: 780_000 }, points: points(780_000, 3.1) }),
  ],
  freshness: { newest_trade: "2026-10-05T14:18:00", record_starts: "2026-09-29T19:37:00", minutes_behind: 2 },
  cases: [{ ...CASE, entity: "category=Sports", segment: "category=Sports", case_id: "sports-1" }],
};

export const CATALOG: CatalogResponse = {
  as_of: "2026-10-05T14:20:00",
  hour: ["2026-10-05T13:00:00", "2026-10-05T14:00:00"],
  kpis: [
    { id: "volume_usd", label: "Trading volume", description: "Dollars traded (shares x price), summed over trades.", unit: "usd", formula: "Dollars traded (shares x price), summed over trades.", definition: "sum(usd)", watched: true, status: "normal", value: 3_830_000 },
    { id: "whale_share", label: "Whale share of volume", description: "Share of dollars traded that came from trades of $1,000 or more.", unit: "share", formula: "Whale volume ÷ Trading volume", definition: "whale_volume / volume_usd", watched: true, status: "normal", value: 0.54 },
    { id: "buy_share", label: "Buy share", description: "Share of dollars traded that bought shares rather than sold them.", unit: "share", formula: "Buy volume ÷ Trading volume", definition: "buy_volume / volume_usd", watched: false, status: null, value: 0.7 },
    { id: "odds_flips", label: "Odds flips", description: "Outcomes whose price crossed 50 cents within the period.", unit: "count", formula: "Worked out for each outcome, then combined", definition: "per asset: min(price) AS lo; then count(*)", watched: true, status: "learning", value: 125 },
  ],
  dimensions: [
    { id: "category", label: "Category", values: ["Sports", "Politics", "Crypto"] },
    { id: "size_band", label: "Trade size", values: ["under $10", "$10-100", "$100-1k", "$1k+"] },
    { id: "market", label: "Market", values: null },
  ],
};

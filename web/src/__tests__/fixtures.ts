import type { CaseTrace, CaseFull, NowResponse, Receipt, SeriesView, CatalogResponse } from "../api";

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

export const TRACE: CaseTrace = {
  recorded: true,
  totals: { wall_ms: 14200, model_calls: 3, input_tokens: 500, output_tokens: 80, cost_usd: 0.15, checks_run: 2, checks_failed: 1 },
  spans: [
    { seq: 0, parent_seq: null, kind: "stage", name: "compare", offset_ms: 0, ms: 200, status: "ok", detail: {} },
    { seq: 1, parent_seq: 0, kind: "tool", name: "kpi.compare", offset_ms: 10, ms: 180, status: "ok", detail: { inputs: { kpi: "volume_usd" }, receipt_id: "r0" } },
    { seq: 2, parent_seq: null, kind: "llm", name: "plan", offset_ms: 200, ms: 4000, status: "ok", detail: { attempts: 2, cost_usd: 0.05, model: "test-model" } },
    { seq: 3, parent_seq: null, kind: "stage", name: "execute", offset_ms: 4200, ms: 1900, status: "ok", detail: {} },
    { seq: 4, parent_seq: 3, kind: "tool", name: "data.coverage", offset_ms: 4200, ms: 500, status: "ok", detail: { step_id: "s1", receipt_id: "r1", inputs: {} } },
    { seq: 5, parent_seq: 3, kind: "tool", name: "by.market", offset_ms: 4700, ms: 1400, status: "error", detail: { step_id: "s2", inputs: { kpi: "volume_usd" }, error: "by.market failed" } },
    { seq: 6, parent_seq: null, kind: "llm", name: "explain", offset_ms: 6100, ms: 8000, status: "ok", detail: { cost_usd: 0.10, model: "test-model" } },
    { seq: 7, parent_seq: null, kind: "stage", name: "verify", offset_ms: 14100, ms: 20, status: "ok", detail: { kept: 6, dropped: 1, interpretation: 1 } },
    { seq: 8, parent_seq: null, kind: "stage", name: "publish", offset_ms: 14120, ms: 80, status: "ok", detail: {} },
  ].map((s) => ({ ...s, started_at: "2026-10-03T12:00:00" })) as CaseTrace["spans"],
};

// GET /api/definitions, trimmed to what the tests read
export const DEFINITIONS = {
  kpis: [
    { id: "volume_usd", label: "Trading volume", description: "Dollars traded (shares x price), summed over trades.", unit: "usd", formula: "Added up over trades", definition: "sum(usd)" },
    { id: "active_traders", label: "Active traders", description: "Distinct wallets that traded.", unit: "count", formula: "Counted once per distinct item", definition: "count(DISTINCT wallet)" },
    { id: "whale_share", label: "Whale share of volume", description: "Share of dollars traded that came from trades of $1,000 or more.", unit: "share", formula: "Whale volume ÷ Trading volume", definition: "whale_volume / volume_usd" },
    { id: "recurring_share", label: "Recurring-market share", description: "Share of dollars traded in recurring short markets.", unit: "share", formula: "Recurring-market volume ÷ Trading volume", definition: "recurring_volume / volume_usd" },
  ],
  dimensions: [
    { id: "category", label: "Category", values: ["Sports", "Politics", "Crypto"], sql: "coalesce(m_category, 'Other')" },
    { id: "size_band", label: "Trade size", values: ["under $10", "$10-100", "$100-1k", "$1k+"], sql: "CASE WHEN usd < 10 THEN 'under $10' ELSE '$1k+' END" },
  ],
  usual_range: { lookback_days: 28, min_history_days: 3, k_sigma: 3, min_run: 2, single_hour_sigma: 5, settle_minutes: 15, log_scale: ["volume_usd"], warmup_days: { new_traders: 7 } },
  sources: [
    { name: "Trades", url: "https://data-api.polymarket.com/trades", how: "Polymarket's public trades feed. Our collector saves the newest 10,000 trades every 4 minutes." },
    { name: "Markets and categories", url: "https://gamma-api.polymarket.com", how: "Polymarket's public market data gives each market's tags; the tags decide its category." },
    { name: "As of", how: "Every number reads only the trades our collector had saved by its time." },
  ],
  record_start: "2026-09-29T19:36:41",
};

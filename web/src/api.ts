import { BASE_PATH } from "./basePath";

const API = `${BASE_PATH}api`;

export type Tier = "governed" | "derived" | "inferred";
export type Status = "open" | "supported" | "refuted" | "confirmed" | "superseded";

export interface Claim {
  text: string;
  receipt_ids: string[];
  tier: Tier;
}

export interface Verdict {
  id: string;
  status: Status;
  confidence: number;
  margin: number | null;
  coverage: number | null;
}

export interface Conclusion {
  case_id: string;
  top_hypothesis: string | null;
  ranked: Verdict[];
  claims: Claim[];
  caveats: string[];
}

export interface Receipt {
  receipt_id: string;
  tool: string;
  inputs: Record<string, unknown>;
  as_of: string;
  data_version: string | null;
  output: Record<string, unknown>;
  effect_size: number | null;
  runtime_ms: number;
  sql: { sql: string; rows_hash: string }[];
}

export interface Step {
  step_id: string;
  tool: string;
  inputs: Record<string, unknown>;
  hypothesis_ids: string[];
  depends_on: string[];
}

export interface CaseSummary {
  case_id: string;
  entity: string;
  metric: string;
  as_of: string;
  opened_at: string;
  state: string;
  season: string;
  top_hypothesis: string | null;
  llm_calls: number;
  cost_usd: number;
}

export interface CaseFull extends CaseSummary {
  plan: { hypotheses: string[]; steps: Step[]; budget_steps: number; source: string } | null;
  plan_attempts: number;
  conclusion: Conclusion | null;
  receipts: Receipt[];
  verdicts: Record<string, string>;
  step_errors: Record<string, string>;
  recheck: {
    as_of: string;
    verdicts: Record<string, string>;
    diffs: Record<string, { before: string | null; after: string }>;
    new_top: string | null;
  } | null;
}

export interface SeriesPoint {
  t: string;
  value: number | null;
  usual: number | null;
  low: number | null;
  high: number | null;
}

export interface SeriesView {
  series: string;
  kpi: string;
  label: string;
  unit: "usd" | "count" | "share" | string;
  segment: string;
  group?: "team" | "behaviour" | "category";
  filters: Record<string, string | string[]>;
  status: "alarm" | "normal" | "learning" | "stale";
  direction: "up" | "down" | null;
  ratio: number | null;
  history_days: number;
  latest: { t: string; value: number | null; usual: number | null } | null;
  points: SeriesPoint[];
  as_of?: string;
  sql?: string; // the query that reproduces every hour shown (Explore)
}

export interface Definitions {
  kpis: { id: string; label: string; description: string; unit: string; formula: string; definition: string }[];
  dimensions: { id: string; label: string; values: string[] | null; sql: string }[];
  usual_range: {
    lookback_days: number;
    min_history_days: number;
    k_sigma: number;
    min_run: number;
    single_hour_sigma: number;
    settle_minutes: number;
    log_scale: string[];
    warmup_days: Record<string, number>;
  };
  sources: { name: string; url?: string; how: string }[];
  record_start: string | null;
}

let definitionsOnce: Promise<Definitions> | null = null;

export interface NowResponse {
  as_of: string;
  series: SeriesView[];
  freshness: { newest_trade: string | null; record_starts: string | null; minutes_behind: number | null };
  cases: (CaseSummary & { segment: string })[];
}

export interface AskResponse {
  sentences: Claim[];
  refused: boolean;
  new_receipts: Receipt[];
  dropped: number;
  cost_usd: number;
}

export type AskTarget = { caseId: string } | { page: "now" };

async function ask(target: AskTarget, question: string): Promise<AskResponse> {
  const body = "caseId" in target ? { case_id: target.caseId, question } : { page: target.page, question };
  const r = await fetch(`${API}/ask`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  const data = await r.json();
  if (!r.ok) throw new Error(typeof data?.detail === "string" ? data.detail : `The question box failed (${r.status}).`);
  return data as AskResponse;
}

export interface CatalogKpi {
  id: string;
  label: string;
  description: string;
  unit: string;
  formula: string;
  definition: string;
  watched: boolean;
  status: "alarm" | "normal" | "learning" | "stale" | null;
  value: number | null;
}

export interface CatalogResponse {
  as_of: string;
  hour: [string, string];
  kpis: CatalogKpi[];
  dimensions: { id: string; label: string; values: string[] | null }[];
}

export interface DigestSentence {
  text: string;
  receipt_ids: string[];
  tier: Tier;
}

export interface DigestCard {
  id: string;
  section: string;
  question: string;
  note: string;
  insufficient: boolean;
  sentence: DigestSentence;
  // the check's output (chart data); its shape depends on the card
  data: Record<string, unknown>;
}

export interface Digest {
  day: string;
  as_of: string;
  period_days: number;
  period_words: { current: string; previous: string };
  windows: { current: [string, string]; previous: [string, string] | null; yesterday: [string, string] };
  headline: (DigestSentence & { card: string }) | null;
  totals: {
    items: { kpi: string; now: number; before: number | null; pct_change: number | null }[];
    sentence: DigestSentence | null;
  };
  cards: DigestCard[];
  coverage: { gap_minutes: number; empty_hours: number } | null;
  receipts: Receipt[];
}

export interface ExploreFilterIn {
  dimension: string;
  values: string[];
}

export interface ExploreAskResponse {
  sentences: Claim[];
  refused: boolean;
  investigate_hour: string | null;
  queries: Record<string, unknown>[];
  new_receipts: Receipt[];
  dropped: number;
  fallback: boolean;
  page_receipt: Receipt | null;
  cost_usd: number;
}

export interface ExploreAskBody {
  question: string;
  page: { kpi: string; filters: ExploreFilterIn[]; window: string; floor: number | null; ceiling: number | null; flagged_hours: string[] };
  history: { question: string; queries: Record<string, unknown>[]; answer: string[] }[];
}

export type ExploreStart = { task_id: string; paid: boolean } | { case_id: string; existing: true };

export interface ExploreTask {
  status: "queued" | "running" | "done" | "failed";
  case_id: string | null;
  paid: boolean;
  error: string | null;
}

async function post<T>(url: string, body: unknown, what: string): Promise<T> {
  const r = await fetch(url, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  const data = await r.json();
  if (!r.ok) throw new Error(typeof data?.detail === "string" ? data.detail : `${what} failed (${r.status}).`);
  return data as T;
}

async function get<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return (await r.json()) as T;
}

export interface TraceSpan {
  seq: number;
  parent_seq: number | null;
  kind: "stage" | "tool" | "llm";
  name: string;
  started_at: string;
  offset_ms: number;
  ms: number;
  status: "ok" | "error";
  detail: {
    inputs?: Record<string, unknown>;
    step_id?: string;
    receipt_id?: string;
    error?: string;
    attempts?: number;
    input_tokens?: number;
    output_tokens?: number;
    cost_usd?: number;
    model?: string | null;
    kept?: number;
    dropped?: number;
    interpretation?: number;
    model_top_hypothesis?: string;
    top_hypothesis?: string | null;
  };
}

export interface CaseTrace {
  recorded: boolean;
  spans: TraceSpan[];
  totals: {
    wall_ms: number;
    model_calls: number;
    input_tokens: number;
    output_tokens: number;
    cost_usd: number;
    checks_run: number;
    checks_failed: number;
  };
}

export const api = {
  now: () => get<NowResponse>(`${API}/now`),
  catalog: () => get<CatalogResponse>(`${API}/catalog`),
  digest: (day?: string) => get<Digest>(day ? `${API}/digest/${encodeURIComponent(day)}` : `${API}/digest`),
  digests: () => get<{ day: string }[]>(`${API}/digests`),
  cases: () => get<CaseSummary[]>(`${API}/cases`),
  case: (id: string) => get<CaseFull>(`${API}/cases/${encodeURIComponent(id)}`),
  trace: (id: string) => get<CaseTrace>(`${API}/cases/${encodeURIComponent(id)}/trace`),
  ask,
  exploreSeries: (kpi: string, segment: string, hours: number) =>
    get<SeriesView>(`${API}/explore/series?${new URLSearchParams({ kpi, segment, hours: String(hours) })}`),
  exploreAsk: (body: ExploreAskBody) => post<ExploreAskResponse>(`${API}/explore/ask`, body, "The question"),
  exploreInvestigate: (body: { kpi: string; filters: ExploreFilterIn[]; hour: string }) =>
    post<ExploreStart>(`${API}/explore/investigate`, body, "Starting the investigation"),
  exploreTask: (id: string) => get<ExploreTask>(`${API}/explore/investigations/${encodeURIComponent(id)}`),
  /** Static for the life of the page, so fetched once (and again only after a failure). */
  definitions: (): Promise<Definitions> => {
    definitionsOnce ??= get<Definitions>(`${API}/definitions`).catch((e) => {
      definitionsOnce = null;
      throw e;
    });
    return definitionsOnce;
  },
};


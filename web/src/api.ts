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
  status: "alarm" | "normal" | "learning";
  direction: "up" | "down" | null;
  ratio: number | null;
  history_days: number;
  latest: { t: string; value: number | null; usual: number | null } | null;
  points: SeriesPoint[];
}

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
  status: "alarm" | "normal" | "learning" | null;
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
};


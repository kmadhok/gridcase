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

export interface Region {
  entity: string;
  latest_hour: string | null;
  demand_mw: number | null;
  forecast_mw: number | null;
  forecast_error_pct: number | null;
  typical_error_pct: number | null;
  case_id: string | null;
  case_state: string | null;
  top_hypothesis: string | null;
}

export interface BenchRun {
  name: string;
  config: Record<string, unknown>;
  summary: {
    synthetic: Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
    replay: Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
    null: { n: number; false_alarm_rate: number | null };
  };
  replay: { scenario_id: string; entity: string; ground_truth: string[]; predicted: string[]; top1: boolean; error: string | null }[];
}

async function get<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return (await r.json()) as T;
}

export const api = {
  map: () => get<{ regions: Region[]; generated_at: string }>("/api/map"),
  cases: () => get<CaseSummary[]>("/api/cases"),
  case: (id: string) => get<CaseFull>(`/api/cases/${encodeURIComponent(id)}`),
  bench: () => get<{ runs: BenchRun[] }>("/api/bench"),
};

export { HYPOTHESES, REGION_NAMES } from "./labels";

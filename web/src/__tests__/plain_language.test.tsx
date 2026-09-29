import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CaseView } from "../views/CaseView";
import type { CaseFull } from "../api";

// The spec's bar: readable by a recruiter in 30 seconds. Engineering ids stay available in
// tooltips/title attributes, never as visible text.
const CASE: CaseFull = {
  case_id: "erco-20210216T12-abc",
  entity: "ERCO",
  metric: "forecast_error_mw",
  as_of: "2021-02-16T12:00:00",
  opened_at: "2026-09-28T00:00:00",
  state: "action_taken",
  season: "winter",
  top_hypothesis: "H2_SUPPLY_SHORTFALL",
  llm_calls: 2,
  cost_usd: 0.31,
  plan: {
    hypotheses: ["H0_DATA_ARTIFACT", "H2_SUPPLY_SHORTFALL"],
    steps: [
      { step_id: "s1", tool: "identity.balance", inputs: {}, hypothesis_ids: ["H0_DATA_ARTIFACT"], depends_on: [] },
      { step_id: "s2", tool: "anomaly.detect", inputs: { metric: "supply_gap_mw" }, hypothesis_ids: ["H2_SUPPLY_SHORTFALL"], depends_on: [] },
    ],
    budget_steps: 12,
    source: "llm",
  },
  plan_attempts: 1,
  conclusion: {
    case_id: "erco-20210216T12-abc",
    top_hypothesis: "H2_SUPPLY_SHORTFALL",
    ranked: [
      { id: "H2_SUPPLY_SHORTFALL", status: "supported", confidence: 0.44, margin: 1, coverage: 0.54 },
      { id: "H0_DATA_ARTIFACT", status: "refuted", confidence: 0.06, margin: -1, coverage: 1 },
      { id: "H5_SUBREGION_CONCENTRATION", status: "open", confidence: 0.15, margin: null, coverage: null },
    ],
    claims: [
      { text: "Generation fell 14,667 MW short of the forecast at the worst hour.", receipt_ids: ["r1"], tier: "governed" },
      { text: "The best-supported cause is a supply shortfall.", receipt_ids: [], tier: "inferred" },
    ],
    caveats: [],
  },
  receipts: [
    { receipt_id: "r1", tool: "anomaly.detect", inputs: { metric: "supply_gap_mw" }, as_of: "2021-02-16T12:00:00", data_version: null, output: { max_abs_dev: 14667 }, effect_size: 19.1, runtime_ms: 12, sql: [{ sql: "SELECT 1", rows_hash: "abc" }] },
  ],
  verdicts: {},
  step_errors: {},
  recheck: { as_of: "2021-02-18T12:00:00", verdicts: {}, diffs: { H2_SUPPLY_SHORTFALL: { before: "supported", after: "confirmed" } }, new_top: "H2_SUPPLY_SHORTFALL" },
};

describe("plain language", () => {
  it("shows no engineering ids, tool names or jargon as visible text on a case", () => {
    render(<CaseView id={CASE.case_id} initial={CASE} />);
    const text = document.body.textContent ?? "";
    for (const raw of ["H2_SUPPLY_SHORTFALL", "H0_DATA_ARTIFACT", "identity.balance", "anomaly.detect", "forecast_error_mw", "supply_gap_mw", "governed", "inferred", "refuted", "margin", "receipt"]) {
      expect(text).not.toContain(raw);
    }
    expect(screen.getAllByText(/Supply shortfall/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/ruled out/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/explains 54% of the gap/i)).toBeInTheDocument();
    expect(screen.getByText(/Do the numbers add up\?/)).toBeInTheDocument();
    expect(screen.getByText(/Was generation short of the forecast\?/)).toBeInTheDocument();
    expect(screen.getAllByText(/measured/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/confirmed/i).length).toBeGreaterThan(0);
  });
});

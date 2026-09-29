import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { CaseView } from "../views/CaseView";
import type { CaseFull } from "../api";

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
  cost_usd: 0.0123,
  plan: { hypotheses: ["H0_DATA_ARTIFACT", "H2_SUPPLY_SHORTFALL"], steps: [{ step_id: "s1", tool: "identity.balance", inputs: {}, hypothesis_ids: ["H0_DATA_ARTIFACT"], depends_on: [] }], budget_steps: 12, source: "template_default" },
  plan_attempts: 1,
  conclusion: {
    case_id: "erco-20210216T12-abc",
    top_hypothesis: "H2_SUPPLY_SHORTFALL",
    ranked: [
      { id: "H2_SUPPLY_SHORTFALL", status: "supported", confidence: 0.74, margin: 2, coverage: 0.41 },
      { id: "H0_DATA_ARTIFACT", status: "refuted", confidence: 0.05, margin: -1, coverage: 1 },
    ],
    claims: [
      { text: "supply_gap_mw for ERCO reached |z| = 15.6 against its baseline.", receipt_ids: ["r1"], tier: "governed" },
      { text: "The best-supported explanation is H2_SUPPLY_SHORTFALL.", receipt_ids: [], tier: "inferred" },
    ],
    caveats: ["EIA demand during a load-shed event is served load, not true demand."],
  },
  receipts: [
    { receipt_id: "r1", tool: "anomaly.detect", inputs: { metric: "supply_gap_mw" }, as_of: "2021-02-16T12:00:00", data_version: null, output: { max_abs_z: 15.6, flagged: true }, effect_size: 15.6, runtime_ms: 12, sql: [{ sql: "WITH m_demand AS (SELECT 1) SELECT t, v FROM m_demand", rows_hash: "abc" }] },
  ],
  verdicts: {},
  step_errors: {},
  recheck: null,
};

describe("CaseView", () => {
  it("renders the hypothesis tree, claims by tier, and expands a claim to its receipt and SQL", async () => {
    render(<CaseView id={CASE.case_id} initial={CASE} />);
    expect(screen.getByTestId("hypothesis-tree")).toHaveTextContent("Supply shortfall");
    expect(screen.getAllByText("measured").length).toBeGreaterThan(0);
    expect(screen.getAllByText("interpretation").length).toBeGreaterThan(0);
    expect(screen.queryByTestId("receipt-sql")).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId("claim-0"));
    expect(screen.getByTestId("receipt")).toHaveTextContent("Was generation short of the forecast?");
    await userEvent.click(screen.getByTestId("toggle-sql"));
    expect(screen.getByTestId("receipt-sql")).toHaveTextContent("WITH m_demand AS");
    expect(screen.getByText(/served load/)).toBeInTheDocument();
  });

  it("marks uncited claims as inferred with no receipt", async () => {
    render(<CaseView id={CASE.case_id} initial={CASE} />);
    await userEvent.click(screen.getByTestId("claim-1"));
    expect(screen.getByText(/nothing in the data backs this sentence/)).toBeInTheDocument();
  });
});

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { CaseView } from "../views/CaseView";
import { CASE } from "./fixtures";

describe("CaseView", () => {
  it("shows the drivers, the answer by tier, and expands a sentence to its evidence and query", async () => {
    render(<CaseView id={CASE.case_id} initial={CASE} />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Why did trading volume move?");
    expect(screen.getByTestId("hypothesis-tree")).toHaveTextContent("One market");
    expect(screen.getAllByText("measured").length).toBeGreaterThan(0);
    expect(screen.queryByTestId("receipt-sql")).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId("claim-0"));
    expect(screen.getByTestId("receipt")).toHaveTextContent("Which market drove it?");
    await userEvent.click(screen.getByTestId("toggle-sql"));
    expect(screen.getByTestId("receipt-sql")).toHaveTextContent("WITH win AS");
  });

  it("marks uncited sentences as interpretation with no evidence", async () => {
    render(<CaseView id={CASE.case_id} initial={CASE} />);
    await userEvent.click(screen.getByTestId("claim-1"));
    expect(screen.getByText(/nothing in the data backs this sentence/)).toBeInTheDocument();
  });

  it("says why an automatic case was opened, with its evidence", async () => {
    const trigger = {
      receipt_id: "t1", tool: "kpi.alarm", inputs: {}, as_of: "2026-10-05T14:20:00", data_version: null,
      output: { status: "alarm", direction: "up", run_hours: 2, value: 2400000, expected: 780000, ratio: 3.1, k_sigma: 3, kpi: "volume_usd", filters: { category: "Sports" } },
      effect_size: 22, runtime_ms: 1, sql: [{ sql: "SELECT 1", rows_hash: "x" }],
    };
    render(<CaseView id={CASE.case_id} initial={{ ...CASE, receipts: [...CASE.receipts, trigger] }} />);
    const note = screen.getByTestId("trigger");
    expect(note).toHaveTextContent("Opened automatically.");
    expect(note).toHaveTextContent("3.1 times its usual level");
    await userEvent.click(within(note).getByRole("button", { name: /show evidence/i }));
    expect(within(note).getByTestId("receipt")).toHaveTextContent("Why was this investigation opened?");
  });

  it("writes a drop as a share of usual, and a drop to nothing as the two values", () => {
    const alarm = (value: number, ratio: number) => ({
      receipt_id: `t-${ratio}`, tool: "kpi.alarm", inputs: {}, as_of: "2026-10-05T14:20:00", data_version: null,
      output: { status: "alarm", direction: "down", run_hours: 2, value, expected: 52, ratio, k_sigma: 3, kpi: "odds_flips", filters: {} },
      effect_size: -6, runtime_ms: 1, sql: [{ sql: "SELECT 1", rows_hash: "x" }],
    });
    const { unmount } = render(<CaseView id={CASE.case_id} initial={{ ...CASE, receipts: [...CASE.receipts, alarm(21, 0.4)] }} />);
    expect(screen.getByTestId("trigger")).toHaveTextContent("40% of its usual level");
    unmount();
    render(<CaseView id={CASE.case_id} initial={{ ...CASE, receipts: [...CASE.receipts, alarm(0, 0)] }} />);
    expect(screen.getByTestId("trigger")).toHaveTextContent("was 0 against a usual level of about 52");
    expect(screen.getByTestId("trigger")).not.toHaveTextContent("times its usual");
  });

  describe("driver cards", () => {
    // s2's inputs match receipt r1 in a different key order, as the API may return them.
    const steps = [
      { step_id: "s1", tool: "data.coverage", inputs: {}, hypothesis_ids: ["D0_DATA_PROBLEM"], depends_on: [] },
      { step_id: "s2", tool: "by.market", inputs: { filters: {}, kpi: "volume_usd" }, hypothesis_ids: ["D1_ONE_MARKET", "D6_BROAD_BASED"], depends_on: [] },
    ];
    const ranked = [...CASE.conclusion!.ranked, { id: "D5_LONGSHOTS", status: "refuted" as const, confidence: 0.02, margin: -1, coverage: null }];
    const tested = { ...CASE, plan: { ...CASE.plan!, steps }, conclusion: { ...CASE.conclusion!, ranked }, step_errors: { s1: "data not published yet" } };

    it("opens any driver, not just the top one, to the sentences and evidence that tested it", async () => {
      render(<CaseView id={CASE.case_id} initial={tested} />);
      const card = screen.getByTestId("driver-D6_BROAD_BASED");
      expect(card).toHaveAttribute("aria-expanded", "false");
      await userEvent.click(card);
      expect(card).toHaveAttribute("aria-expanded", "true");
      const evidence = screen.getByTestId("driver-evidence-D6_BROAD_BASED");
      expect(evidence).toHaveTextContent("England vs Czechia accounts for 72%");
      expect(within(evidence).getByTestId("receipt")).toHaveTextContent("Which market drove it?");
      expect(screen.queryByTestId("driver-evidence-D1_ONE_MARKET")).not.toBeInTheDocument();
      await userEvent.click(card);
      expect(screen.queryByTestId("driver-evidence-D6_BROAD_BASED")).not.toBeInTheDocument();
    });

    it("says when the check for a driver couldn't run", async () => {
      render(<CaseView id={CASE.case_id} initial={tested} />);
      await userEvent.click(screen.getByTestId("driver-D0_DATA_PROBLEM"));
      const evidence = screen.getByTestId("driver-evidence-D0_DATA_PROBLEM");
      expect(evidence).toHaveTextContent("couldn't run (data not published yet)");
      expect(within(evidence).queryByTestId("receipt")).not.toBeInTheDocument();
    });

    it("says when no check tested a driver", async () => {
      render(<CaseView id={CASE.case_id} initial={tested} />);
      expect(screen.getByTestId("driver-D5_LONGSHOTS")).toHaveTextContent("show how it was tested (0)");
      await userEvent.click(screen.getByTestId("driver-D5_LONGSHOTS"));
      expect(screen.getByTestId("driver-evidence-D5_LONGSHOTS")).toHaveTextContent("No check in this investigation tested this driver.");
    });
  });
  it("flags a case opened while the collector was down, and only that one", () => {
    const { unmount } = render(<CaseView id={CASE.case_id} initial={{ ...CASE, judged_without_data: true }} />);
    expect(screen.getByTestId("judged-without-data")).toHaveTextContent("Opened while our collector was down");
    unmount();
    render(<CaseView id={CASE.case_id} initial={CASE} />);
    expect(screen.queryByTestId("judged-without-data")).not.toBeInTheDocument();
  });

  it("explains the order when the first driver scores lower than one below it", () => {
    const conclusion = CASE.conclusion!;
    const ranked = [
      { id: "D2_ONE_CATEGORY", status: "supported" as const, confidence: 0.38, margin: 1, coverage: 0.89 },
      { id: "D6_BROAD_BASED", status: "supported" as const, confidence: 0.52, margin: 1, coverage: 1 },
    ];
    const { unmount } = render(<CaseView id={CASE.case_id} initial={{ ...CASE, conclusion: { ...conclusion, top_hypothesis: "D2_ONE_CATEGORY", ranked } }} />);
    expect(screen.getByTestId("rank-note")).toHaveTextContent("Listed by cause, not by score");
    unmount();
    render(<CaseView id={CASE.case_id} initial={CASE} />);
    expect(screen.queryByTestId("rank-note")).not.toBeInTheDocument();
  });
});


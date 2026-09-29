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
});

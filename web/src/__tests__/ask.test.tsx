import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AskPanel } from "../views/AskPanel";

const RECEIPT = {
  receipt_id: "new1", tool: "by.category", inputs: { kpi: "volume_usd" }, as_of: "2026-10-05T14:20:00",
  data_version: null, output: { top_segment: "Sports", top_share: 0.8, top_change: 1620000 }, effect_size: 0.8, runtime_ms: 5, sql: [{ sql: "SELECT 2", rows_hash: "h" }],
};

function mockFetch(status: number, body: unknown) {
  const fn = vi.fn().mockResolvedValue({ ok: status < 400, status, json: async () => body });
  vi.stubGlobal("fetch", fn);
  return fn;
}

afterEach(() => vi.unstubAllGlobals());

describe("AskPanel", () => {
  it("sends the question for this case and shows a verified, tagged answer with its evidence", async () => {
    const fetch = mockFetch(200, {
      sentences: [
        { text: "Sports accounts for 80% of the change ($1,620,000).", receipt_ids: ["new1"], tier: "governed" },
        { text: "That points to a single sporting event.", receipt_ids: [], tier: "inferred" },
      ],
      refused: false, new_receipts: [RECEIPT], dropped: 0, cost_usd: 0.02,
    });
    render(<AskPanel target={{ caseId: "c1" }} receipts={{}} asOf="2026-10-05T14:20:00" />);
    await userEvent.type(screen.getByRole("textbox"), "Which category drove it?");
    await userEvent.click(screen.getByRole("button", { name: /^ask$/i }));
    await screen.findByText(/Sports accounts for 80% of the change/);
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ case_id: "c1", question: "Which category drove it?" });
    expect(screen.getAllByText("measured").length).toBeGreaterThan(0);
    expect(screen.getAllByText("interpretation").length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole("button", { name: /show evidence/i }));
    expect(screen.getByTestId("receipt")).toHaveTextContent("Which category drove it?");
  });

  it("fills the box from a suggested question", async () => {
    mockFetch(200, { sentences: [], refused: false, new_receipts: [], dropped: 0, cost_usd: 0 });
    render(<AskPanel target={{ page: "now" }} receipts={{}} />);
    await userEvent.click(screen.getAllByTestId("suggestion")[0]);
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value.length).toBeGreaterThan(0);
  });

  it("says plainly when a question is off-topic or the limit is hit", async () => {
    mockFetch(200, { sentences: [], refused: true, new_receipts: [], dropped: 0, cost_usd: 0 });
    const { unmount } = render(<AskPanel target={{ caseId: "c1" }} receipts={{}} />);
    await userEvent.type(screen.getByRole("textbox"), "Who won the game?");
    await userEvent.click(screen.getByRole("button", { name: /^ask$/i }));
    await screen.findByText(/only answer questions about this/i);
    unmount();
    mockFetch(429, { detail: "Too many questions; please wait a few minutes." });
    render(<AskPanel target={{ caseId: "c1" }} receipts={{}} />);
    await userEvent.type(screen.getByRole("textbox"), "Again?");
    await userEvent.click(screen.getByRole("button", { name: /^ask$/i }));
    await waitFor(() => expect(screen.getByText(/Too many questions/)).toBeInTheDocument());
  });
});

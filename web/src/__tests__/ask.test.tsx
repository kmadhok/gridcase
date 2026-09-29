import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AskPanel } from "../views/AskPanel";

const RECEIPT = {
  receipt_id: "new1", tool: "anomaly.detect", inputs: { metric: "net_interchange" }, as_of: "2021-02-16T12:00:00",
  data_version: null, output: { max_abs_z: 8.7 }, effect_size: 8.7, runtime_ms: 5, sql: [{ sql: "SELECT 2", rows_hash: "h" }],
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
        { text: "Power over the ties reached 8.7 times its normal variation.", receipt_ids: ["new1"], tier: "governed" },
        { text: "That means imports were near their limit.", receipt_ids: [], tier: "inferred" },
      ],
      refused: false, new_receipts: [RECEIPT], dropped: 0, cost_usd: 0.02,
    });
    render(<AskPanel target={{ caseId: "c1" }} receipts={{}} asOf="2021-02-16T12:00:00" />);
    await userEvent.type(screen.getByRole("textbox"), "Were imports maxed out?");
    await userEvent.click(screen.getByRole("button", { name: /^ask$/i }));
    await screen.findByText(/8.7 times its normal variation/);
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ case_id: "c1", question: "Were imports maxed out?" });
    expect(screen.getAllByText("measured").length).toBeGreaterThan(0);
    expect(screen.getAllByText("interpretation").length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole("button", { name: /show evidence/i }));
    expect(screen.getByTestId("receipt")).toHaveTextContent("Were imports maxed out?");
  });

  it("fills the box from a suggested question", async () => {
    mockFetch(200, { sentences: [], refused: false, new_receipts: [], dropped: 0, cost_usd: 0 });
    render(<AskPanel target={{ page: "bench" }} receipts={{}} />);
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

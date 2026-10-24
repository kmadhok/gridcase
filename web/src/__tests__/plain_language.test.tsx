import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { api, type CaseTrace } from "../api";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CaseView } from "../views/CaseView";
import { NowView } from "../views/NowView";
import { CASE, NOW, TRACE } from "./fixtures";

// The bar: readable by a product manager or a recruiter in 30 seconds. Engine ids stay in
// tooltips/title attributes, never as visible text.
const RAW = [
  "D0_DATA_PROBLEM", "D1_ONE_MARKET", "D6_BROAD_BASED", "data.coverage", "by.market", "kpi.compare",
  "volume_usd", "active_traders", "new_traders", "category=", "governed", "inferred", "refuted", "margin", "receipt",
];

afterEach(() => vi.restoreAllMocks());

describe("plain language", () => {
  it("a case shows no engine ids, tool names or jargon", async () => {
    vi.spyOn(api, "trace").mockResolvedValue({
      ...(TRACE as CaseTrace),
      spans: (TRACE as CaseTrace).spans.map((span) => span.name === "explain"
        ? { ...span, detail: { ...span.detail, model_top_hypothesis: "D1_ONE_MARKET", top_hypothesis: "D6_BROAD_BASED" } }
        : span),
    });
    render(<CaseView id={CASE.case_id} initial={CASE} />);
    await userEvent.click(screen.getByRole("button", { name: "Show the timeline" }));
    await screen.findByTestId("trace-timeline");
    const text = document.body.textContent ?? "";
    for (const raw of RAW) expect(text).not.toContain(raw);
    expect(screen.getAllByText(/One market/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/ruled out/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/explains 72% of the gap/i)).toBeInTheDocument();
    expect(screen.getAllByText(/Is our data complete\?/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Which market drove it\?/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/confirmed/i).length).toBeGreaterThan(0);
  });

  it("the Now page shows no engine ids either", () => {
    render(<NowView initial={NOW} />);
    const text = document.body.textContent ?? "";
    for (const raw of RAW) expect(text).not.toContain(raw);
  });
});

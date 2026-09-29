import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CaseView } from "../views/CaseView";
import { NowView } from "../views/NowView";
import { CASE, NOW } from "./fixtures";

// The bar: readable by a product manager or a recruiter in 30 seconds. Engine ids stay in
// tooltips/title attributes, never as visible text.
const RAW = [
  "D0_DATA_PROBLEM", "D1_ONE_MARKET", "D6_BROAD_BASED", "data.coverage", "by.market", "kpi.compare",
  "volume_usd", "active_traders", "new_traders", "category=", "governed", "inferred", "refuted", "margin", "receipt",
];

describe("plain language", () => {
  it("a case shows no engine ids, tool names or jargon", () => {
    render(<CaseView id={CASE.case_id} initial={CASE} />);
    const text = document.body.textContent ?? "";
    for (const raw of RAW) expect(text).not.toContain(raw);
    expect(screen.getAllByText(/One market/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/ruled out/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/explains 72% of the gap/i)).toBeInTheDocument();
    expect(screen.getByText(/Is our data complete\?/)).toBeInTheDocument();
    expect(screen.getByText(/Which market drove it\?/)).toBeInTheDocument();
    expect(screen.getAllByText(/confirmed/i).length).toBeGreaterThan(0);
  });

  it("the Now page shows no engine ids either", () => {
    render(<NowView initial={NOW} />);
    const text = document.body.textContent ?? "";
    for (const raw of RAW) expect(text).not.toContain(raw);
  });
});

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, type CaseTrace } from "../api";
import { CaseView } from "../views/CaseView";
import { TraceView } from "../views/TraceView";
import { CASE, TRACE } from "./fixtures";

afterEach(() => vi.restoreAllMocks());
const trace = TRACE as CaseTrace;

describe("Step by step", () => {
  it("starts collapsed and opens a plain-language timeline in order", async () => {
    const fetch = vi.spyOn(api, "trace").mockResolvedValue(trace);
    render(<CaseView id={CASE.case_id} initial={CASE} />);
    const toggle = screen.getByRole("button", { name: "Show the timeline" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("trace-timeline")).not.toBeInTheDocument();
    await userEvent.click(toggle);
    const timeline = await screen.findByTestId("trace-timeline");
    expect(fetch).toHaveBeenCalledWith(CASE.case_id);
    expect(screen.getByText("Took 14.2 s · 3 model calls · $0.15 · 2 checks, 1 couldn't run")).toBeInTheDocument();
    const rows = within(timeline).getAllByRole("listitem");
    const labels = ["Measured the change", "Planned the checks (2 tries)", "Ran the checks", "Is our data complete?", "Which market drove it?", "Wrote the explanation", "Checked every sentence against the evidence", "Saved the case"];
    expect(rows).toHaveLength(labels.length); // the change's own query is not a second row
    rows.forEach((row, i) => expect(row).toHaveTextContent(labels[i]));
    expect(rows[4]).toHaveTextContent("couldn't run");
    expect(rows[6]).toHaveTextContent("kept 6, removed 1");
    for (const id of ["kpi.compare", "data.coverage", "by.market", "test-model", "volume_usd", "s1", "r1"]) {
      expect(timeline.textContent).not.toMatch(new RegExp(`\\b${id.replaceAll(".", "\\.")}\\b`));
    }
    expect(rows[1]).toHaveTextContent("$0.05");
    expect(rows[5]).toHaveTextContent("$0.10");
    const bars = within(timeline).getAllByTestId("trace-bar");
    const shown = TRACE.spans.filter((s) => s.name !== "kpi.compare");
    bars.forEach((bar, i) => {
      expect(parseFloat(bar.style.width)).toBeCloseTo(100 * shown[i].ms / 14200);
      expect(parseFloat(bar.style.left)).toBeCloseTo(100 * shown[i].offset_ms / 14200);
    });
    await userEvent.click(screen.getByRole("button", { name: "Hide the timeline" }));
    expect(screen.queryByTestId("trace-timeline")).not.toBeInTheDocument();
  });

  it("explains when timing was not recorded", async () => {
    vi.spyOn(api, "trace").mockResolvedValue({ ...trace, recorded: false, spans: [] });
    render(<TraceView caseId={CASE.case_id} />);
    await userEvent.click(screen.getByRole("button", { name: "Show the timeline" }));
    expect(await screen.findByText("Step-by-step timing is recorded for investigations from 3 October 2026 on.")).toBeInTheDocument();
  });

  it("keeps load failures in plain language", async () => {
    vi.spyOn(api, "trace").mockRejectedValue(new Error("/api/cases/engine-id/trace: 500"));
    render(<TraceView caseId={CASE.case_id} />);
    await userEvent.click(screen.getByRole("button", { name: "Show the timeline" }));
    expect(await screen.findByText("The timeline couldn't load. Please try again.")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("engine-id");
  });
  it("labels the free standard playbook honestly: no model calls, no cost", async () => {
    const free: CaseTrace = {
      recorded: true,
      totals: { wall_ms: 1000, model_calls: 0, input_tokens: 0, output_tokens: 0, cost_usd: 0, checks_run: 1, checks_failed: 0 },
      spans: [
        { seq: 0, parent_seq: null, kind: "llm", name: "plan", offset_ms: 0, ms: 1, status: "ok", detail: { attempts: 1, cost_usd: 0, model: null } },
        { seq: 1, parent_seq: null, kind: "llm", name: "explain", offset_ms: 900, ms: 1, status: "ok", detail: { cost_usd: 0, model: null } },
      ],
    } as CaseTrace;
    vi.spyOn(api, "trace").mockResolvedValue(free);
    render(<TraceView caseId="c" />);
    await userEvent.click(screen.getByRole("button", { name: "Show the timeline" }));
    const timeline = await screen.findByTestId("trace-timeline");
    expect(screen.getByText(/standard playbook, no model calls/)).toBeInTheDocument();
    expect(timeline).toHaveTextContent("Planned the checks (standard playbook, no model)");
    expect(timeline).toHaveTextContent("Wrote the explanation (from templates, no model)");
    expect(timeline.textContent).not.toContain("$");
  });
});

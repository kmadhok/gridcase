import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { chartSegments, clipBand, formatChartTicks } from "../chart";
import { statusText } from "../nowStatus";
import { Sparkline } from "../views/NowView";
import { ExploreChart } from "../views/ExploreChart";
import { parseState } from "../explore";
import { fmtTime } from "../labels";
import { series } from "./fixtures";

const points = [null, 0.001, null, 0.002, 0.003, null].map((value, i) => ({
  t: `2026-10-05T0${i}:00:00`, value, low: null, high: null, usual: null,
}));
const view = series({ unit: "share", points });

describe("chart frames", () => {
  it("splits missing values without losing isolated hours", () => {
    expect(chartSegments(points, (p) => p.value !== null)).toEqual([[1], [3, 4]]);
    expect(chartSegments([], () => true)).toEqual([]);
  });
  it("renders min/max, actual endpoint times and a focus/hover readout", () => {
    render(<Sparkline s={view} />);
    expect(screen.getByText("Min 0.1%")).toBeInTheDocument();
    expect(screen.getByText("Max 0.3%")).toBeInTheDocument();
    expect(screen.getByText(fmtTime(points[1].t))).toBeInTheDocument();
    expect(screen.getByText(fmtTime(points[5].t))).toBeInTheDocument();
    const hour = screen.getAllByRole("button")[0];
    fireEvent.focus(hour);
    expect(screen.getByRole("status")).toHaveTextContent("No usual range yet");
    fireEvent.blur(hour);
    fireEvent.pointerEnter(hour);
    expect(screen.getByRole("status")).toHaveTextContent(fmtTime(points[1].t));
    expect(screen.getByTestId("isolated-point")).toHaveAttribute("cx", "4");
    expect(screen.getAllByTestId("value-segment")).toHaveLength(1);
  });
  it("includes the usual range in the hour readout", () => {
    render(<Sparkline s={series({ points: [{ ...points[1], value: 10, low: 8, high: 12 }] })} />);
    fireEvent.focus(screen.getByRole("button"));
    expect(screen.getByRole("status")).toHaveTextContent("$10 · usual range $8–$12");
    expect(screen.getByTestId("isolated-point")).toHaveAttribute("cx", "120");
  });
  it("draws isolated Explore hours and distinguishes small percentage ticks", () => {
    const { container } = render(<ExploreChart view={view} state={parseState("")} flagged={new Set()} selected={null} onSelect={() => {}} onLine={() => {}} />);
    expect(screen.getByTestId("isolated-point")).toBeInTheDocument();
    expect(screen.getAllByTestId("value-segment")).toHaveLength(1);
    const ticks = [...container.querySelectorAll("text")].map((el) => el.textContent).filter((v) => v?.endsWith("%"));
    expect(ticks).toHaveLength(5);
    expect(new Set(ticks).size).toBe(5);
  });
  it("keeps adjacent ticks distinct across units", () => {
    for (const unit of ["share", "usd", "count", "ratio"]) {
      expect(new Set(formatChartTicks(unit, [0, 0.0001, 0.0002])).size).toBe(3);
    }
  });
  it("shows days only below the configured minimum", () => {
    expect(statusText(series({ status: "learning", history_days: 2 }), 5).text).toBe("Learning normal · day 2 of 5");
    for (const history_days of [5, 18]) {
      expect(statusText(series({ status: "learning", history_days }), 5).text).toBe("Learning normal · warming up");
    }
    expect(statusText(series({ status: "learning" }), null).text).toBe("Learning normal · warming up");
  });
});

describe("usual band", () => {
  const band = (unit: string, low: number, high: number) =>
    series({ unit, points: [0.8, 0.9].map((value, i) => ({ t: `2026-10-05T0${i}:00:00`, value, low, high, usual: value })) });

  it("never labels a share above 100% or any KPI below zero", () => {
    render(<Sparkline s={band("share", -0.2, 1.49)} />);
    expect(screen.getByText("Min 0%")).toBeInTheDocument();
    expect(screen.getByText("Max 100%")).toBeInTheDocument();
  });

  it("clips only what the unit cannot take", () => {
    const [p] = clipBand("count", band("count", -5, 40).points);
    expect([p.low, p.high]).toEqual([0, 40]);
    const [s] = clipBand("share", band("share", 0.2, 0.7).points);
    expect([s.low, s.high]).toEqual([0.2, 0.7]);
  });
});

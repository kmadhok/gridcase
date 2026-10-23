import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NowView } from "../views/NowView";
import { api } from "../api";
import { DEFINITIONS, NOW } from "./fixtures";

afterEach(() => vi.restoreAllMocks());

describe("NowView", () => {
  it("shows every watched KPI with its status against the usual level", async () => {
    vi.spyOn(api, "definitions").mockResolvedValue(DEFINITIONS);
    render(<NowView initial={NOW} />);
    const volume = screen.getByTestId("tile-volume_usd");
    expect(volume).toHaveTextContent("Trading volume");
    expect(volume).toHaveTextContent("Normal");
    expect(volume).toHaveTextContent("$1.2M");
    expect(await screen.findByText("Learning normal · day 1 of 3")).toBeInTheDocument();
    const sports = screen.getByTestId("tile-volume_usd · category=Sports");
    expect(sports).toHaveTextContent("Sports volume");
    expect(sports).toHaveTextContent("Unusual ↑ 3.1× usual");
    expect(within(sports).getByRole("img")).toBeInTheDocument(); // the sparkline
  });

  it("shows how the market behaves in its own row", () => {
    render(<NowView initial={NOW} />);
    const row = screen.getByRole("region", { name: "How the market behaves" });
    expect(within(row).getByTestId("tile-whale_share")).toHaveTextContent("Whale share of volume");
    expect(within(row).getByTestId("tile-whale_share")).toHaveTextContent("54%");
    expect(within(row).getByTestId("tile-trades_per_trader")).toHaveTextContent("7.95");
    const team = screen.getByRole("region", { name: "Team KPIs" });
    expect(within(team).queryByTestId("tile-whale_share")).toBeNull();
  });

  it("links an unusual KPI to its investigation in one click", async () => {
    render(<NowView initial={NOW} />);
    const sports = screen.getByTestId("tile-volume_usd · category=Sports");
    await userEvent.click(within(sports).getByTestId("open-case"));
    expect(window.location.hash).toBe("#/case/sports-1");
    expect(screen.getByTestId("cases")).toHaveTextContent("Best explanation: one market");
  });

  it("offers the question box about what's happening now", () => {
    render(<NowView initial={NOW} />);
    expect(screen.getByText(/Ask about what's happening/)).toBeInTheDocument();
  });
});

describe("segment names", () => {
  it("keep an ampersand inside a category name", async () => {
    const { segmentName } = await import("../labels");
    expect(segmentName("category=Economy & Finance")).toBe("Economy & Finance");
    expect(segmentName("category=Sports;size_band=$1k+")).toBe("Sports, $1k+");
  });
});

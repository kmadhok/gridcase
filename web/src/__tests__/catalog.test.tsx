import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { parseHash, href } from "../router";
import { CatalogView } from "../views/CatalogView";
import { CATALOG } from "./fixtures";

const RAW = ["volume_usd", "whale_share", "whale_volume", "sum(usd)", "d_size_band", "odds_flips"];

describe("KPI catalog", () => {
  it("has its own address", () => {
    expect(parseHash("#/kpis")).toEqual({ view: "kpis" });
    expect(href({ view: "kpis" })).toBe("#/kpis");
  });

  it("explains each KPI in plain words with its last hour and watch status", () => {
    render(<CatalogView initial={CATALOG} />);
    const whale = screen.getByTestId("kpi-whale_share");
    expect(whale).toHaveTextContent("Whale share of volume");
    expect(whale).toHaveTextContent("trades of $1,000 or more");
    expect(whale).toHaveTextContent("Whale volume ÷ Trading volume");
    expect(whale).toHaveTextContent("54%");
    expect(whale).toHaveTextContent("Watched every hour");
    expect(within(screen.getByTestId("kpi-buy_share")).queryByText("Watched every hour")).toBeNull();
    expect(screen.getByText(/Category/)).toBeInTheDocument();
  });

  it("keeps the exact definition out of sight until asked", async () => {
    render(<CatalogView initial={CATALOG} />);
    const code = within(screen.getByTestId("kpi-whale_share")).getByTestId("definition");
    expect(code).not.toBeVisible();
    await userEvent.click(within(screen.getByTestId("kpi-whale_share")).getByText("Show the exact definition"));
    expect(code).toBeVisible();
    expect(code).toHaveTextContent("whale_volume / volume_usd");
  });

  it("shows no engine ids outside the definitions", () => {
    render(<CatalogView initial={CATALOG} />);
    const clone = document.body.cloneNode(true) as HTMLElement;
    clone.querySelectorAll("[data-testid=definition]").forEach((n) => n.remove());
    const text = clone.textContent ?? "";
    for (const raw of RAW) expect(text).not.toContain(raw);
  });
});

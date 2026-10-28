import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Digest } from "../api";
import { CaseView } from "../views/CaseView";
import { CatalogView } from "../views/CatalogView";
import { DigestView } from "../views/DigestView";
import { ExploreView } from "../views/ExploreView";
import fixture from "./digest.fixture.json";
import { CASE, CATALOG, series } from "./fixtures";

// jsdom has no 2D canvas context, so each scene's start returns null; these check the canvas is mounted where it
// belongs, fixed behind the page and hidden from assistive tech.
function expectBackdrop(testId: string) {
  const field = screen.getByTestId(testId);
  expect(field.tagName).toBe("CANVAS");
  expect(field).toHaveAttribute("aria-hidden", "true");
  expect(field).toHaveClass("fixed", "pointer-events-none");
  expect(field.parentElement).toBe(document.body);
}

const createRange = document.createRange;

afterEach(() => {
  document.createRange = createRange;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("page backdrops", () => {
  it("puts a sunrise behind the morning read", () => {
    render(<DigestView initial={fixture as unknown as Digest} />);
    expectBackdrop("sunrise-field");
  });

  it("puts the formula compiler behind the KPI catalog", () => {
    render(<CatalogView initial={CATALOG} />);
    expectBackdrop("compiler-field");
  });

  it("puts the semantic-layer graph behind Explore once the catalog loads", async () => {
    const chart = { ...series({}), as_of: "2026-10-05T14:20:00", sql: "SELECT 1" };
    vi.stubGlobal("fetch", vi.fn(async (url: string) => ({
      ok: true, status: 200, json: async () => (url.includes("/api/catalog") ? CATALOG : chart),
    })));
    render(<ExploreView />);
    expect(await screen.findByTestId("graph-field")).toBeInTheDocument();
    expectBackdrop("graph-field");
  });

  it("puts the investigation tree behind a case", () => {
    render(<CaseView id={CASE.case_id} initial={CASE} />);
    expectBackdrop("tree-field");
  });
});

// A stand-in 2D context, so the scenes actually draw: under reduced motion each draws one still frame on mount.
// The glyphs are put back together into rows of text to check what the scene wrote.
function fakeCanvas() {
  const glyphs: { ch: string; x: number; y: number }[] = [];
  const ctx = {
    setTransform() {}, clearRect() {}, fillText(ch: string, x: number, y: number) { glyphs.push({ ch, x, y }); },
    font: "", textAlign: "", textBaseline: "", fillStyle: "",
  };
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(ctx as unknown as CanvasRenderingContext2D);
  vi.spyOn(HTMLCanvasElement.prototype, "getBoundingClientRect").mockReturnValue({ width: 1600, height: 960, top: 0, left: 0, right: 1600, bottom: 960, x: 0, y: 0, toJSON() {} } as DOMRect);
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  vi.stubGlobal("matchMedia", (q: string) => ({ matches: q.includes("reduce"), addEventListener() {}, removeEventListener() {} }));
  document.createRange = () => ({ selectNodeContents() {}, getClientRects: () => [] }) as unknown as Range;
  return () => {
    const rows = new Map<number, Map<number, string>>();
    for (const g of glyphs) {
      const row = rows.get(g.y) ?? new Map<number, string>();
      row.set(g.x, g.ch);
      rows.set(g.y, row);
    }
    return [...rows.values()].map((row) => {
      const xs = [...row.keys()].sort((a, b) => a - b);
      let line = "";
      xs.forEach((x, i) => { line += (i && x - xs[i - 1] > 10.5 ? " " : "") + row.get(x); });
      return line;
    }).join("\n");
  };
}

describe("page backdrops draw from the page's own data", () => {
  it("the case tree names the drivers the case tested, with their real status", () => {
    const text = fakeCanvas();
    render(<CaseView id={CASE.case_id} initial={CASE} />);
    const drawn = text();
    expect(drawn).toContain("One market · evidence supports · 68% likely");
    expect(drawn).toContain("Data problem · ruled out");
    expect(drawn).toContain("Is our data complete?");
  });

  it("the catalog compiles a real KPI's label, formula and definition", () => {
    const text = fakeCanvas();
    render(<CatalogView initial={CATALOG} />);
    const drawn = text();
    expect(drawn).toContain("Trading volume");
    expect(drawn).toContain("= Dollars traded (shares x price), summed over trades.");
    expect(drawn).toContain("→ sum(usd)");
  });

  it("the Explore graph labels its nodes and the lit path with catalog names", async () => {
    const text = fakeCanvas();
    const chart = { ...series({}), as_of: "2026-10-05T14:20:00", sql: "SELECT 1" };
    vi.stubGlobal("fetch", vi.fn(async (url: string) => ({
      ok: true, status: 200, json: async () => (url.includes("/api/catalog") ? CATALOG : chart),
    })));
    render(<ExploreView />);
    await screen.findByTestId("graph-field");
    const drawn = text();
    for (const name of ["Trading volume", "Odds flips", "Category", "Trade size"]) expect(drawn).toContain(name);
    expect(drawn).toMatch(/ by (category|trade size|market) /);
  });

  it("the sunrise names its horizon and draws the sun in the sky", () => {
    const text = fakeCanvas();
    render(<DigestView initial={fixture as unknown as Digest} />);
    const drawn = text();
    expect(drawn).toContain("horizon: volume by hour, Monday to Sunday");
    expect(drawn).toContain("O");
  });
});

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { flaggedHours, parseState, stateToQuery } from "../explore";
import { href, parseHash } from "../router";
import { ExploreView } from "../views/ExploreView";
import { CASE, CATALOG, DEFINITIONS, series } from "./fixtures";

// The 48-hour Sports volume fixture: usual 1.15M every hour, the last two hours at 3.1x.
const SERIES = series({ segment: "category=Sports", filters: { category: "Sports" } });
const SPIKE = SERIES.points.map((p, i) => (i >= 46 ? { ...p, value: 3_565_000 } : p));
const CHART = { ...SERIES, points: SPIKE, as_of: "2026-10-05T14:20:00", sql: "SELECT date_trunc('hour', ts) AS t, sum(usd) AS v FROM trades_as_of('2026-10-05 14:20') WHERE d_category = 'Sports'" };

type Handler = (body: unknown) => { status?: number; body: unknown };

function mockApi(routes: Record<string, Handler>) {
  const calls: { url: string; body: unknown }[] = [];
  const fn = vi.fn(async (url: string, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url, body });
    const key = Object.keys(routes).find((k) => url.includes(k));
    if (!key) return { ok: false, status: 404, json: async () => ({ detail: "not mocked" }) };
    const res = routes[key](body);
    const status = res.status ?? 200;
    return { ok: status < 400, status, json: async () => res.body };
  });
  vi.stubGlobal("fetch", fn);
  return calls;
}

const BASE = { "/api/catalog": () => ({ body: CATALOG }), "/api/explore/series": () => ({ body: CHART }) };

afterEach(() => {
  vi.unstubAllGlobals();
  window.location.hash = "";
});

describe("Explore state", () => {
  it("round-trips through the page link", () => {
    const q = "kpi=trades&seg=category%3DSports%3Bsize_band%3D%241k%2B&h=168&ceiling=5000&mode=lines";
    const state = parseState(q);
    expect(state).toEqual({ kpi: "trades", filters: { category: "Sports", size_band: "$1k+" }, hours: 168, floor: null, ceiling: 5000, mode: "lines" });
    expect(parseState(stateToQuery(state))).toEqual(state);
    expect(parseHash("#/explore?kpi=trades")).toEqual({ view: "explore", query: "kpi=trades" });
    expect(href({ view: "explore", query: "kpi=trades" })).toBe("#/explore?kpi=trades");
    expect(parseState("kpi=volume_usd&h=999&mode=nonsense")).toMatchObject({ hours: 72, mode: "usual" });
  });

  it("flags the hours that leave the usual range or cross the visitor's lines", () => {
    const base = parseState("");
    expect(flaggedHours(SPIKE, base).map((p) => p.t)).toEqual([SPIKE[46].t, SPIKE[47].t]);
    expect(flaggedHours(SPIKE, { ...base, mode: "lines", ceiling: 2_000_000 })).toHaveLength(2);
    expect(flaggedHours(SPIKE, { ...base, mode: "lines", ceiling: 4_000_000 })).toHaveLength(0);
    expect(flaggedHours(SPIKE, { ...base, mode: "lines", floor: 1_200_000 })).toHaveLength(46);
  });
});

describe("ExploreView", () => {
  it("draws the chosen slice and lists its unusual hours; the visitor's lines change the list and the link", async () => {
    const calls = mockApi(BASE);
    render(<ExploreView query="kpi=volume_usd&seg=category%3DSports" />);
    expect(await screen.findByRole("heading", { name: "Trading volume · Sports" })).toBeInTheDocument();
    expect(calls.some((c) => c.url.includes("/api/explore/series?kpi=volume_usd&segment=category%3DSports&hours=72"))).toBe(true);
    const list = await screen.findByTestId("flagged");
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    await userEvent.type(screen.getByLabelText("Ceiling"), "4000000");
    await userEvent.click(screen.getByRole("button", { name: "Cross my lines" }));
    expect(screen.getByText(/No hour in this window crossed your lines/)).toBeInTheDocument();
    expect(window.location.hash).toContain("ceiling=4000000");
    expect(window.location.hash).toContain("mode=lines");
  });

  it("investigates a flagged hour and shows the answer with a link to the case", async () => {
    const calls = mockApi({
      ...BASE,
      "/api/explore/investigate": () => ({ status: 202, body: { task_id: "t1", paid: true } }),
      "/api/explore/investigations/t1": () => ({ body: { status: "done", case_id: CASE.case_id, paid: true, error: null } }),
      [`/api/cases/${CASE.case_id}`]: () => ({ body: CASE }),
    });
    render(<ExploreView query="kpi=volume_usd&seg=category%3DSports" pollMs={5} />);
    const list = await screen.findByTestId("flagged");
    await userEvent.click(within(list).getAllByRole("button", { name: "Why did this happen?" })[0]);
    expect(await screen.findByText("One market")).toBeInTheDocument();
    expect(screen.getByText(/England vs Czechia accounts for 72% of the change/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Open the full case/ })).toHaveAttribute("href", `#/case/${CASE.case_id}`);
    const post = calls.find((c) => c.url.endsWith("/api/explore/investigate"));
    expect(post?.body).toEqual({ kpi: "volume_usd", filters: [{ dimension: "category", values: ["Sports"] }], hour: SPIKE[47].t });
  });

  it("says plainly why an hour cannot be investigated", async () => {
    mockApi({ ...BASE, "/api/explore/investigate": () => ({ status: 409, body: { detail: "It's too early to judge that hour: there isn't enough history for it yet." } }) });
    render(<ExploreView query="kpi=volume_usd&seg=category%3DSports" pollMs={5} />);
    const list = await screen.findByTestId("flagged");
    await userEvent.click(within(list).getAllByRole("button", { name: "Why did this happen?" })[0]);
    expect(await screen.findByText(/too early to judge that hour/)).toBeInTheDocument();
  });

  it("chats about what is on screen, sends the history, and marks when the view changes", async () => {
    let turn = 0;
    const calls = mockApi({
      ...BASE,
      "/api/explore/ask": () => {
        turn += 1;
        return turn === 1
          ? { body: { sentences: [{ text: "In Sports over the last 3 days, volume peaked at $3.6M.", receipt_ids: ["p1"], tier: "governed" }], refused: false, investigate_hour: null, queries: [], new_receipts: [], dropped: 0, fallback: false, page_receipt: null, cost_usd: 0.002 } }
          : { body: { sentences: [], refused: false, investigate_hour: SPIKE[47].t, queries: [], new_receipts: [], dropped: 0, fallback: false, page_receipt: null, cost_usd: 0.001 } };
      },
    });
    render(<ExploreView query="kpi=volume_usd&seg=category%3DSports" />);
    await screen.findByTestId("flagged");
    const box = screen.getByLabelText("Your question");
    await userEvent.type(box, "When was the peak?{Enter}");
    expect(await screen.findByText(/volume peaked at \$3.6M/)).toBeInTheDocument();
    const first = calls.find((c) => c.url.includes("/api/explore/ask"))?.body as { page: Record<string, unknown>; history: unknown[] };
    expect(first.page).toMatchObject({ kpi: "volume_usd", filters: [{ dimension: "category", values: ["Sports"] }], window: "last_3d" });
    expect(first.history).toEqual([]);

    await userEvent.selectOptions(screen.getByLabelText("Category"), "Politics");
    expect(await screen.findByText(/now looking at/i)).toBeInTheDocument();
    await userEvent.type(box, "Why did it jump?{Enter}");
    expect(await screen.findByRole("button", { name: /Investigate/ })).toBeInTheDocument();
    const second = calls.filter((c) => c.url.includes("/api/explore/ask"))[1].body as { history: { question: string; answer: string[] }[] };
    expect(second.history).toEqual([{ question: "When was the peak?", queries: [], answer: ["In Sports over the last 3 days, volume peaked at $3.6M."] }]);
  });

  it("answers a question it cannot handle with the KPIs it tracks", async () => {
    mockApi({
      ...BASE,
      "/api/explore/ask": () => ({ body: { sentences: [], refused: true, investigate_hour: null, queries: [], new_receipts: [], dropped: 0, fallback: false, page_receipt: null, cost_usd: 0.001 } }),
    });
    render(<ExploreView query="kpi=volume_usd" />);
    await screen.findByTestId("flagged");
    await userEvent.type(screen.getByLabelText("Your question"), "What were the biggest whale trades in that game?{Enter}");
    expect(await screen.findByText("I can't answer that. Here are the KPIs I currently track:")).toBeInTheDocument();
    const tracked = within(screen.getByTestId("chat")).getByRole("list", { name: "KPIs I track" });
    expect(within(tracked).getByText("Trading volume")).toBeInTheDocument();
    expect(within(tracked).getByText("Whale wallets")).toBeInTheDocument();
    expect(within(tracked).getAllByRole("listitem")).toHaveLength(18);
    expect(screen.queryByText(/only answer questions/)).not.toBeInTheDocument();
  });

  it("explains how the chart is calculated: the KPI, the slice, the usual range, the sources and the query", async () => {
    mockApi({ ...BASE, "/api/definitions": () => ({ body: DEFINITIONS }) });
    render(<ExploreView query="kpi=volume_usd&seg=category%3DSports" />);
    await screen.findByTestId("flagged");
    await userEvent.click(screen.getByRole("button", { name: "How is this calculated?" }));
    const panel = await screen.findByTestId("how-calculated");
    expect(within(panel).getByText(/Dollars traded \(shares x price\), summed over trades/)).toBeInTheDocument();
    expect(within(panel).getByText("Category: Sports")).toBeInTheDocument();
    expect(within(panel).getByText(/same hour on up to 28 earlier days/)).toBeInTheDocument();
    expect(within(panel).getByRole("link", { name: /data-api.polymarket.com\/v2\/trades/ })).toHaveAttribute("href", "https://data-api.polymarket.com/v2/trades");
    expect(within(panel).getByTestId("how-sql")).toHaveTextContent(CHART.sql);
    expect(within(panel).getByRole("link", { name: /What each number means/ })).toHaveAttribute("href", "#/kpis");
  });

  it("shows no engine ids", async () => {
    mockApi(BASE);
    render(<ExploreView query="kpi=volume_usd&seg=category%3DSports" />);
    await screen.findByTestId("flagged");
    await waitFor(() => expect(screen.getAllByRole("option").length).toBeGreaterThan(3));
    const text = document.body.textContent ?? "";
    for (const raw of ["volume_usd", "category=", "kpi.series", "last_3d", "size_band", "governed", "inferred"]) expect(text).not.toContain(raw);
  });
});

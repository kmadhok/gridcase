import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Digest } from "../api";
import { href, parseHash } from "../router";
import { DigestView } from "../views/DigestView";
import fixture from "./digest.fixture.json";
import { DEFINITIONS } from "./fixtures";

// the real output of the backend's M6 exit-test store (gridcase digest for 2026-10-15)
const DIGEST = fixture as unknown as Digest;

const RAW = [
  "digest.", "volume_share", "volume_usd", "whale_share", "recurring_share", "share_shift",
  "odds_swings", "receipt", "governed", "inferred", "insufficient",
];

afterEach(() => vi.unstubAllGlobals());

describe("morning read", () => {
  it("has its own address, for the latest day and for any day", () => {
    expect(parseHash("#/digest")).toEqual({ view: "digest" });
    expect(parseHash("#/digest/2026-10-15")).toEqual({ view: "digest", day: "2026-10-15" });
    expect(href({ view: "digest" })).toBe("#/digest");
    expect(href({ view: "digest", day: "2026-10-15" })).toBe("#/digest/2026-10-15");
  });

  it("leads with the headline and the period in four numbers", () => {
    render(<DigestView initial={DIGEST} />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Sports took 8 more points of the money this week.");
    const stats = screen.getByTestId("totals");
    expect(stats).toHaveTextContent("Trading volume");
    expect(stats).toHaveTextContent("$127k");
    expect(stats).toHaveTextContent("+26% on last week");
  });

  it("answers every standing question in one sentence with one chart", () => {
    render(<DigestView initial={DIGEST} />);
    for (const card of DIGEST.cards) {
      const el = screen.getByTestId(`card-${card.id}`);
      expect(el).toHaveTextContent(card.question);
      expect(el).toHaveTextContent(card.sentence.text);
      if (card.id !== "alarms") expect(within(el).getByRole("img")).toBeInTheDocument();
    }
    expect(within(screen.getByTestId("card-movers")).getByRole("img")).toHaveTextContent("Chiefs vs Bills");
    expect(within(screen.getByTestId("card-odds_swings")).getByRole("img")).toHaveTextContent("30¢ → 80¢");
  });

  it("explains how a card's numbers were calculated, from the KPI's definition to the exact query", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200, json: async () => DEFINITIONS })));
    render(<DigestView initial={DIGEST} />);
    const card = screen.getByTestId("card-whales");
    await userEvent.click(within(card).getByRole("button", { name: "How is this calculated?" }));
    const panel = await within(card).findByTestId("how-calculated");
    expect(within(panel).getByText(/Share of one-sided \(taker\) cash-flow volume .* from trades of \$1,000 or more/)).toBeInTheDocument();
    expect(within(panel).getByText(/Whale volume ÷ Trading volume/)).toBeInTheDocument();
    const ids = DIGEST.cards.find((c) => c.id === "whales")?.sentence.receipt_ids ?? [];
    const sql = DIGEST.receipts.find((r) => ids.includes(r.receipt_id))?.sql[0].sql ?? "missing";
    expect(within(panel).getByTestId("how-sql")).toHaveTextContent(sql.replace(/\s+/g, " ").trim());
    expect(within(panel).getByRole("link", { name: /data-api.polymarket.com/ })).toBeInTheDocument();
  });

  it("says when a card reads trades directly rather than one KPI", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200, json: async () => DEFINITIONS })));
    render(<DigestView initial={DIGEST} />);
    const card = screen.getByTestId("card-movers");
    await userEvent.click(within(card).getByRole("button", { name: "How is this calculated?" }));
    expect(await within(card).findByText(/reads trades directly/)).toBeInTheDocument();
  });

  it("explains the period's four totals too", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200, json: async () => DEFINITIONS })));
    render(<DigestView initial={DIGEST} />);
    const totals = screen.getByTestId("totals-section");
    await userEvent.click(within(totals).getByRole("button", { name: "How is this calculated?" }));
    expect(await within(totals).findByText(/Distinct wallets that traded/)).toBeInTheDocument();
  });

  it("shows the evidence behind a sentence on request", async () => {
    render(<DigestView initial={DIGEST} />);
    const card = screen.getByTestId("card-movers");
    expect(within(card).queryByTestId("receipt")).toBeNull();
    await userEvent.click(within(card).getByRole("button", { name: /show the evidence/i }));
    expect(within(card).getByTestId("receipt")).toHaveTextContent("Which markets came out of nowhere?");
  });

  it("says plainly when there is not enough history, instead of an empty chart", () => {
    const short: Digest = {
      ...DIGEST,
      cards: DIGEST.cards.map((c) =>
        c.id === "share_shift"
          ? { ...c, insufficient: true, data: {}, sentence: { text: "There is not enough history yet to compare category shares between periods.", receipt_ids: [], tier: "inferred" } }
          : c,
      ),
    };
    render(<DigestView initial={short} />);
    const card = screen.getByTestId("card-share_shift");
    expect(card).toHaveTextContent("not enough history");
    expect(within(card).queryByRole("img")).toBeNull();
  });

  it("shows no engine ids or jargon", () => {
    render(<DigestView initial={DIGEST} />);
    const text = document.body.textContent ?? "";
    for (const raw of RAW) expect(text).not.toContain(raw);
  });
});

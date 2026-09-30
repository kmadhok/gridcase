import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { Digest } from "../api";
import { href, parseHash } from "../router";
import { DigestView } from "../views/DigestView";
import fixture from "./digest.fixture.json";

// the real output of the backend's M6 exit-test store (gridcase digest for 2026-10-15)
const DIGEST = fixture as unknown as Digest;

const RAW = [
  "digest.", "volume_share", "volume_usd", "whale_share", "recurring_share", "share_shift",
  "odds_swings", "receipt", "governed", "inferred", "insufficient",
];

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

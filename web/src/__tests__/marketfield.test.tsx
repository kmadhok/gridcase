import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NowView } from "../views/NowView";
import { api } from "../api";
import { DEFINITIONS, NOW } from "./fixtures";

afterEach(() => vi.restoreAllMocks());

describe("MarketField", () => {
  it("puts the whole-page ridgeline behind the Now page, hidden from assistive tech", () => {
    vi.spyOn(api, "definitions").mockResolvedValue(DEFINITIONS);
    render(<NowView initial={NOW} />);
    const field = screen.getByTestId("market-field");
    expect(field.tagName).toBe("CANVAS");
    expect(field).toHaveAttribute("aria-hidden", "true");
    expect(field).toHaveClass("fixed", "pointer-events-none");
  });
});

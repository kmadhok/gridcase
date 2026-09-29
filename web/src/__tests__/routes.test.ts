import { describe, expect, it } from "vitest";
import routes from "../routes.json";
import { href, parseHash } from "../router";

describe("routes", () => {
  it("parses and renders hash routes", () => {
    expect(parseHash("")).toEqual({ view: "now" });
    expect(parseHash("#/")).toEqual({ view: "now" });
    expect(parseHash("#/bench")).toEqual({ view: "now" });
    expect(parseHash("#/case/all-1")).toEqual({ view: "case", id: "all-1" });
    expect(href({ view: "case", id: "a b" })).toBe("#/case/a%20b");
  });

  it("reaches an investigation from the landing page in one or two clicks", () => {
    expect(routes.clicks_to_conclusion).toBeLessThanOrEqual(2);
    expect(routes.landing).toBe("/");
  });
});

import { describe, expect, it } from "vitest";
import routes from "../routes.json";
import { href, parseHash } from "../router";

describe("routes", () => {
  it("parses and renders hash routes", () => {
    expect(parseHash("")).toEqual({ view: "map" });
    expect(parseHash("#/")).toEqual({ view: "map" });
    expect(parseHash("#/bench")).toEqual({ view: "bench" });
    expect(parseHash("#/case/erco-1")).toEqual({ view: "case", id: "erco-1" });
    expect(href({ view: "case", id: "a b" })).toBe("#/case/a%20b");
  });

  it("reaches a conclusion from the landing page in at most three clicks", () => {
    expect(routes.clicks_to_conclusion).toBeLessThanOrEqual(3);
    expect(routes.landing).toBe("/");
  });
});

import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";

// The same build is served at kanumadhok.com/marketcase/ and, over Tailscale, at the VPS: the
// server answers under /marketcase too, so every request goes there.
describe("public path", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("asks the server under /marketcase/", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal("fetch", fetch);
    await api.now();
    await api.digest("2026-10-15");
    await api.ask({ page: "now" }, "q");
    expect(fetch.mock.calls.map((c) => c[0])).toEqual([
      "/marketcase/api/now",
      "/marketcase/api/digest/2026-10-15",
      "/marketcase/api/ask",
    ]);
  });
});

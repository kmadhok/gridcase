import { useEffect, useState } from "react";

export type Route =
  | { view: "now" }
  | { view: "kpis" }
  | { view: "digest"; day?: string }
  | { view: "case"; id: string }
  | { view: "explore"; query: string };

export function parseHash(hash: string): Route {
  const full = hash.replace(/^#/, "") || "/";
  const [path, query = ""] = full.split(/\?(.*)/s);
  if (path === "/explore") return { view: "explore", query };
  const m = path.match(/^\/case\/(.+)$/);
  if (m) return { view: "case", id: decodeURIComponent(m[1]) };
  if (path === "/kpis") return { view: "kpis" };
  const d = path.match(/^\/digest(?:\/(\d{4}-\d{2}-\d{2}))?$/);
  if (d) return d[1] ? { view: "digest", day: d[1] } : { view: "digest" };
  return { view: "now" };
}

export function href(route: Route): string {
  if (route.view === "case") return `#/case/${encodeURIComponent(route.id)}`;
  if (route.view === "kpis") return "#/kpis";
  if (route.view === "explore") return route.query ? `#/explore?${route.query}` : "#/explore";
  if (route.view === "digest") return route.day ? `#/digest/${route.day}` : "#/digest";
  return "#/";
}

export function navigate(route: Route): void {
  window.location.hash = href(route);
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));
  useEffect(() => {
    const on = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}

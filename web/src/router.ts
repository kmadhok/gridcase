import { useEffect, useState } from "react";

export type Route =
  | { view: "map" }
  | { view: "case"; id: string }
  | { view: "bench" };

export function parseHash(hash: string): Route {
  const path = hash.replace(/^#/, "") || "/";
  const m = path.match(/^\/case\/(.+)$/);
  if (m) return { view: "case", id: decodeURIComponent(m[1]) };
  if (path === "/bench") return { view: "bench" };
  return { view: "map" };
}

export function href(route: Route): string {
  if (route.view === "case") return `#/case/${encodeURIComponent(route.id)}`;
  if (route.view === "bench") return "#/bench";
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

// The Explore page's state lives in its link (#/explore?kpi=…&seg=…&h=…&floor=…&ceiling=…&mode=…),
// so a refresh or a shared link shows the same chart and lines. Nothing is stored on the server.
import type { ExploreFilterIn, SeriesPoint } from "./api";
import { fmtValue } from "./labels";

export type Hours = 24 | 72 | 168;
export type Mode = "lines" | "usual";

export interface ExploreState {
  kpi: string;
  filters: Record<string, string>; // one segment per dimension, e.g. { category: "Sports" }
  hours: Hours;
  floor: number | null;
  ceiling: number | null;
  mode: Mode;
}

export const RANGES: { hours: Hours; label: string; window: string }[] = [
  { hours: 24, label: "24 h", window: "last_24h" },
  { hours: 72, label: "3 days", window: "last_3d" },
  { hours: 168, label: "7 days", window: "last_7d" },
];

/** The slices a visitor can pick, in the order the controls show them. */
export const SLICE_DIMS = ["category", "size_band", "trader_type"] as const;

export function segmentOf(filters: Record<string, string>): string {
  const parts = Object.entries(filters)
    .filter(([, v]) => v)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([k, v]) => `${k}=${v}`);
  return parts.length ? parts.join(";") : "all";
}

function parseSegment(seg: string): Record<string, string> {
  if (!seg || seg === "all") return {};
  const out: Record<string, string> = {};
  for (const part of seg.split(";")) {
    const i = part.indexOf("=");
    if (i > 0 && i < part.length - 1) out[part.slice(0, i)] = part.slice(i + 1);
  }
  return out;
}

export function parseState(query: string): ExploreState {
  const q = new URLSearchParams(query);
  const num = (k: string): number | null => {
    const v = q.get(k);
    if (v === null || v.trim() === "") return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  };
  const h = Number(q.get("h"));
  return {
    kpi: q.get("kpi") || "volume_usd",
    filters: parseSegment(q.get("seg") ?? ""),
    hours: RANGES.some((r) => r.hours === h) ? (h as Hours) : 72,
    floor: num("floor"),
    ceiling: num("ceiling"),
    mode: q.get("mode") === "lines" ? "lines" : "usual",
  };
}

export function stateToQuery(s: ExploreState): string {
  const q = new URLSearchParams({ kpi: s.kpi });
  const seg = segmentOf(s.filters);
  if (seg !== "all") q.set("seg", seg);
  if (s.hours !== 72) q.set("h", String(s.hours));
  if (s.floor !== null) q.set("floor", String(s.floor));
  if (s.ceiling !== null) q.set("ceiling", String(s.ceiling));
  if (s.mode !== "usual") q.set("mode", s.mode);
  return q.toString();
}

export function filterList(filters: Record<string, string>): ExploreFilterIn[] {
  return Object.entries(filters)
    .filter(([, v]) => v)
    .map(([dimension, v]) => ({ dimension, values: [v] }));
}

export function windowOf(hours: Hours): string {
  return RANGES.find((r) => r.hours === hours)?.window ?? "last_3d";
}

/** An hour the watch can judge: it has a value and a usual range. */
export function judged(p: SeriesPoint): boolean {
  return p.value !== null && p.usual !== null && p.low !== null && p.high !== null;
}

/** Hours that left their usual range, or crossed the visitor's ceiling or floor. */
export function flaggedHours(points: SeriesPoint[], s: ExploreState): SeriesPoint[] {
  return points.filter((p) => {
    if (p.value === null) return false;
    if (s.mode === "usual") return judged(p) && (p.value > (p.high as number) || p.value < (p.low as number));
    return (s.ceiling !== null && p.value > s.ceiling) || (s.floor !== null && p.value < s.floor);
  });
}

/** Two significant figures, for lines a person would draw ("$4.0M", not "$3,987,412"). */
export function roundNice(v: number): number {
  if (v <= 0) return 0;
  const step = 10 ** (Math.floor(Math.log10(v)) - 1);
  return Math.round(v / step) * step;
}

/** Where a new line starts: just outside everything the chart shows. */
export function suggestLine(points: SeriesPoint[], which: "floor" | "ceiling"): number {
  const vals = points.flatMap((p) => [p.value, which === "ceiling" ? p.high : p.low]).filter((v): v is number => v !== null);
  if (!vals.length) return 0;
  return which === "ceiling" ? roundNice(Math.max(...vals) * 1.1) : roundNice(Math.min(...vals) * 0.9);
}

/** One hour in words: its value against its usual range. */
export function describeHour(p: SeriesPoint, unit: string): string {
  if (p.value === null) return "no data for this hour (our collector missed trades, or the record had not begun)";
  if (p.usual === null || p.low === null || p.high === null) return `${fmtValue(unit, p.value)} · still learning what's usual for this hour`;
  const off = p.value > p.high ? ` · ${(p.value / p.usual).toFixed(1)}× usual` : p.value < p.low ? " · below its usual range" : "";
  return `${fmtValue(unit, p.value)} · usually ${fmtValue(unit, p.low)}–${fmtValue(unit, p.high)}${off}`;
}

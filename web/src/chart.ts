import type { SeriesView } from "./api";
import { fmtValue } from "./labels";

/** Contiguous runs retain their original indices, including isolated observations. */
export function chartSegments<T>(points: T[], present: (point: T) => boolean): number[][] {
  const runs: number[][] = [];
  points.forEach((p, i) => {
    if (!present(p)) return;
    if (i === 0 || !present(points[i - 1])) runs.push([]);
    runs[runs.length - 1].push(i);
  });
  return runs;
}

export const bandSegments = (points: SeriesView["points"]) =>
  chartSegments(points, (p) => p.low !== null && p.high !== null);

/** Keep the familiar compact units unless rounding would hide the scale. */
export function formatChartTicks(unit: string, values: number[]): string[] {
  const compact = values.map((v) => fmtValue(unit, v));
  if (new Set(compact).size === values.length) return compact;
  for (let digits = 0; digits <= 12; digits++) {
    const labels = values.map((v) => `${unit === "usd" ? "$" : ""}${(unit === "share" ? v * 100 : v).toLocaleString("en-GB", { maximumFractionDigits: digits })}${unit === "share" ? "%" : ""}`);
    if (new Set(labels).size === values.length) return labels;
  }
  return values.map((v) => `${unit === "usd" ? "$" : ""}${unit === "share" ? v * 100 : v}${unit === "share" ? "%" : ""}`);
}

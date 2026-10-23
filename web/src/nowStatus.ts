import type { SeriesView } from "./api";

export function statusText(s: SeriesView, minimum: number | null): { text: string; cls: string } {
  if (s.status === "alarm") {
    const times = s.ratio ? `${s.ratio.toFixed(1)}×` : "";
    return { text: `Unusual ${s.direction === "down" ? "↓" : "↑"} ${times} usual`, cls: "border-current text-accent-hi" }; // on the blush alarm tile, accent-hi keeps 4.5:1
  }
  if (s.status === "stale") return { text: "Waiting for data", cls: "border-ink/25 text-ink-muted" };
  if (s.status === "learning") return { text: minimum !== null && s.history_days < minimum ? `Learning normal · day ${s.history_days} of ${minimum}` : "Learning normal · warming up", cls: "border-ink/25 text-ink-muted" };
  return { text: "Normal", cls: "border-current text-ok" };
}

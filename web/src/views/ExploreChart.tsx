import { useEffect, useRef, useState, type PointerEvent } from "react";
import type { SeriesView } from "../api";
import { describeHour, roundNice, type ExploreState } from "../explore";
import { fmtTime, fmtValue } from "../labels";

const PT = 24;
const PB = 28;

type Line = "floor" | "ceiling";

/** The KPI hour by hour (line) over its usual range for each hour of the day (band), with the
 * visitor's draggable ceiling and floor, flagged hours as dots and the selected hour marked. */
export function ExploreChart({
  view, state, flagged, selected, onSelect, onLine,
}: {
  view: SeriesView;
  state: ExploreState;
  flagged: Set<string>;
  selected: string | null;
  onSelect: (t: string) => void;
  onLine: (which: Line, value: number) => void;
}) {
  const svg = useRef<SVGSVGElement>(null);
  const [drag, setDrag] = useState<Line | null>(null);
  const [hover, setHover] = useState<number | null>(null);
  // draw at the real width, so labels stay readable on a phone instead of shrinking with the chart
  const wrap = useRef<HTMLDivElement>(null);
  const [W, setW] = useState(760);
  useEffect(() => {
    const el = wrap.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(300, Math.round(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const narrow = W < 520;
  const H = narrow ? 240 : 300;
  const PL = narrow ? 48 : 60;
  const PR = narrow ? 88 : 104;
  const pts = view.points;
  if (!pts.length) return <p className="text-sm text-ink-muted">No complete hours in this window yet.</p>;

  const vals = pts.flatMap((p) => [p.value, p.high]).filter((v): v is number => v !== null);
  const ymax = Math.max(...vals, state.ceiling ?? 0, state.floor ?? 0, 1e-9) * 1.1;
  const x = (i: number) => PL + (pts.length > 1 ? i / (pts.length - 1) : 0.5) * (W - PL - PR);
  const y = (v: number) => PT + (1 - v / ymax) * (H - PT - PB);
  const step = pts.length > 1 ? (W - PL - PR) / (pts.length - 1) : W - PL - PR;

  const toSvg = (e: PointerEvent) => {
    const r = svg.current?.getBoundingClientRect();
    if (!r || !r.width) return { sx: 0, sy: 0 };
    return { sx: ((e.clientX - r.left) / r.width) * W, sy: ((e.clientY - r.top) / r.height) * H };
  };
  const idxAt = (sx: number) => Math.max(0, Math.min(pts.length - 1, Math.round((sx - PL) / step)));

  // the usual band, in runs of hours that have one
  const bands: string[] = [];
  let run: number[] = [];
  const flush = () => {
    if (run.length > 1) {
      const top = run.map((i) => `${x(i)},${y(pts[i].high as number)}`).join(" L ");
      const bottom = [...run].reverse().map((i) => `${x(i)},${y(pts[i].low as number)}`).join(" L ");
      bands.push(`M ${top} L ${bottom} Z`);
    }
    run = [];
  };
  pts.forEach((p, i) => (p.low !== null && p.high !== null ? run.push(i) : flush()));
  flush();

  let line = "";
  pts.forEach((p, i) => {
    if (p.value === null) return;
    line += `${line && pts[i - 1]?.value !== null ? " L" : " M"} ${x(i)},${y(p.value)}`;
  });

  const ticks = pts
    .map((p, i) => ({ i, d: new Date(p.t + "Z") }))
    .filter(({ d }) => (pts.length <= 24 ? d.getUTCHours() % 6 === 0 : d.getUTCHours() === 0));
  const tickLabel = (d: Date) =>
    pts.length <= 24
      ? `${String(d.getUTCHours()).padStart(2, "0")}:00`
      : d.toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
  // the record's first days: from the first hour with data until the first usual range
  const firstData = pts.findIndex((p) => p.value !== null);
  const firstBand = pts.findIndex((p) => p.usual !== null);
  const learnFrom = Math.max(firstData, 0);
  const learnTo = firstBand < 0 ? pts.length - 1 : firstBand;
  const learning = firstData >= 0 && learnTo - learnFrom >= 6;
  const hovered = hover !== null ? pts[hover] : null;

  return (
    <div ref={wrap} className="relative">
      <svg
        ref={svg}
        viewBox={`0 0 ${W} ${H}`}
        className="h-auto w-full touch-none select-none"
        role="img"
        aria-label={`${view.label}, hour by hour, against its usual range and your lines`}
        onPointerMove={(e) => {
          const { sx, sy } = toSvg(e);
          if (drag) {
            const v = roundNice(Math.max(0, ymax * (1 - (sy - PT) / (H - PT - PB))));
            onLine(drag, v);
            return;
          }
          setHover(sx >= PL && sx <= W - PR ? idxAt(sx) : null);
        }}
        onPointerUp={() => setDrag(null)}
        onPointerLeave={() => {
          setHover(null);
          setDrag(null);
        }}
      >
        {[0, 1, 2, 3, 4].map((k) => {
          const v = (ymax / 1.1) * (k / 4);
          return (
            <g key={k}>
              <line x1={PL} x2={W - PR} y1={y(v)} y2={y(v)} stroke="var(--gc-line)" />
              <text x={PL - 8} y={y(v) + 4} textAnchor="end" fontSize="11" fill="var(--gc-muted)">{fmtValue(view.unit, v)}</text>
            </g>
          );
        })}
        {learning && (
          <g>
            <rect x={x(learnFrom)} y={PT} width={Math.max(0, x(learnTo) - x(learnFrom))} height={H - PT - PB} fill="var(--gc-mark)" opacity="0.18" />
            <text x={x(learnFrom)} y={PT - 8} fontSize="11" fill="var(--gc-muted)">Not enough history yet for a usual range</text>
          </g>
        )}
        {bands.map((d, k) => <path key={k} d={d} fill="var(--gc-band)" />)}
        {pts.map((p, i) => (p.value === null ? <rect key={p.t} x={x(i) - Math.max(step / 2, 2)} y={PT} width={Math.max(step, 4)} height={H - PT - PB} fill="var(--gc-mark)" opacity="0.5" /> : null))}
        {ticks.map(({ i, d }) => (
          <g key={i}>
            <line x1={x(i)} x2={x(i)} y1={H - PB} y2={H - PB + 4} stroke="var(--gc-muted)" />
            <text x={x(i)} y={H - 8} textAnchor="middle" fontSize="11" fill="var(--gc-muted)">{tickLabel(d)}</text>
          </g>
        ))}
        <path d={line} fill="none" strokeWidth={1.8} stroke="var(--gc-ink)" strokeLinejoin="round" />
        {(["ceiling", "floor"] as const).map((which) => {
          const v = state[which];
          if (v === null) return null;
          return (
            <g key={which}>
              <line x1={PL} x2={W - PR + 4} y1={y(v)} y2={y(v)} stroke="var(--gc-accent)" strokeWidth={1.5} strokeDasharray="6 4" />
              <g
                className="cursor-ns-resize"
                onPointerDown={(e) => {
                  setDrag(which);
                  svg.current?.setPointerCapture?.(e.pointerId);
                  e.preventDefault();
                }}
              >
                <rect x={W - PR + 6} y={y(v) - 11} width={PR - 8} height={22} rx={11} fill="var(--gc-accent)" />
                <text x={W - PR + 6 + (PR - 8) / 2} y={y(v) + 4} textAnchor="middle" fontSize="11" fontWeight="600" fill="white">
                  {which === "ceiling" ? "Ceiling" : "Floor"} {fmtValue(view.unit, v)}
                </text>
              </g>
            </g>
          );
        })}
        {pts.map((p, i) => (flagged.has(p.t) && p.value !== null ? <circle key={p.t} cx={x(i)} cy={y(p.value)} r={4.5} fill="var(--gc-accent)" stroke="var(--gc-panel)" strokeWidth={1.5} /> : null))}
        {pts.map((p, i) =>
          p.t === selected ? (
            <g key={`sel-${p.t}`}>
              <line x1={x(i)} x2={x(i)} y1={PT} y2={H - PB} stroke="var(--gc-ink)" strokeDasharray="2 3" />
              {p.value !== null && <circle cx={x(i)} cy={y(p.value)} r={5.5} fill="none" stroke="var(--gc-ink)" strokeWidth={2} />}
            </g>
          ) : null,
        )}
        <rect
          x={PL}
          y={PT}
          width={W - PL - PR}
          height={H - PT - PB}
          fill="transparent"
          className="cursor-crosshair"
          onClick={(e) => onSelect(pts[idxAt(toSvg(e as unknown as PointerEvent).sx)].t)}
        />
      </svg>
      {hovered && hover !== null && (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute max-w-[260px] rounded-xl border border-ink bg-sheet px-3 py-2 text-xs"
          style={{ left: `${Math.min((x(hover) / W) * 100, 62)}%`, top: 8 }}
        >
          <div className="font-medium">{fmtTime(hovered.t)}</div>
          <div className="text-ink-muted">{describeHour(hovered, view.unit)}</div>
        </div>
      )}
    </div>
  );
}


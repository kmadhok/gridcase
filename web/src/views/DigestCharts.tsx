// The morning read's charts: hand-drawn SVG, one per standing question. Everything is grey
// except the thing the card's sentence names, which is amber, so the chart points at the
// answer rather than at a legend. Text takes its colour from the theme tokens.
import type { ReactNode } from "react";

const ACC = "var(--gc-accent)";
const MARK = "var(--gc-mark)";
const INK = "var(--gc-ink)";
const MUTED = "var(--gc-muted)";
const LINE = "var(--gc-line)";

type Anchor = "start" | "middle" | "end";

function T({ x, y, children, fill = MUTED, anchor = "start", bold = false }: {
  x: number; y: number; children: ReactNode; fill?: string; anchor?: Anchor; bold?: boolean;
}) {
  return (
    <text x={x} y={y} fill={fill} fontSize={11} textAnchor={anchor} fontWeight={bold ? 650 : 400} style={{ fontVariantNumeric: "tabular-nums" }}>
      {children}
    </text>
  );
}

// wide charts keep a readable size on a phone and scroll sideways inside their own box
function Svg({ w, h, label, children }: { w: number; h: number; label: string; children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <svg viewBox={`0 0 ${w} ${h}`} role="img" aria-label={label} className="block h-auto w-full" style={{ minWidth: w > 440 ? w * 0.8 : Math.min(w, 320) }}>
        {children}
      </svg>
    </div>
  );
}

const pct = (v: number): string => `${Math.round(100 * v)}%`;
const cents = (v: number): string => `${Math.round(100 * v)}¢`;
const clip = (s: string, n: number): string => (s.length > n ? `${s.slice(0, n - 1)}…` : s);
const shortDay = (day: string): string =>
  new Date(`${day}T00:00:00Z`).toLocaleDateString("en-GB", { weekday: "short", timeZone: "UTC" });

/** Push label positions apart so no two sit closer than `gap`, keeping their order. */
function spread(ys: number[], gap: number): number[] {
  const order = ys.map((y, i) => ({ y, i })).sort((a, b) => a.y - b.y);
  for (let k = 1; k < order.length; k++) order[k].y = Math.max(order[k].y, order[k - 1].y + gap);
  const out = [...ys];
  for (const o of order) out[o.i] = o.y;
  return out;
}

// ------------------------------------------------------------------ what changed

export interface ShareSegment { segment: string; now: number; before: number }

export function ShareSlope({ segments, gainer }: { segments: ShareSegment[]; gainer: string }) {
  const keep = segments.slice(0, 3).map((s) => s.segment);
  if (!keep.includes(gainer)) keep[2] = gainer;
  const rest = segments.filter((s) => !keep.includes(s.segment));
  const rows: ShareSegment[] = [
    ...segments.filter((s) => keep.includes(s.segment)),
    ...(rest.length
      ? [{ segment: "Everything else", now: rest.reduce((a, s) => a + s.now, 0), before: rest.reduce((a, s) => a + s.before, 0) }]
      : []),
  ];
  const vals = rows.flatMap((r) => [r.now, r.before]);
  const lo = Math.min(...vals);
  const hi = Math.max(...vals);
  const y = (v: number) => 186 - ((v - lo) / (hi - lo || 1)) * 156;
  const x0 = 160;
  const x1 = 320;
  const left = spread(rows.map((r) => y(r.before)), 15);
  const right = spread(rows.map((r) => y(r.now)), 15);
  return (
    <Svg w={420} h={200 + Math.max(0, Math.max(...left, ...right) - 190)} label="Each category's share of volume, the period before against this one">
      <T x={x0} y={14} anchor="middle">before</T>
      <T x={x1} y={14} anchor="middle">now</T>
      {rows.map((r, i) => {
        const hot = r.segment === gainer;
        const c = hot ? ACC : MARK;
        return (
          <g key={r.segment}>
            <line x1={x0} y1={y(r.before)} x2={x1} y2={y(r.now)} stroke={c} strokeWidth={hot ? 3 : 2} />
            <circle cx={x0} cy={y(r.before)} r={4} fill={c} />
            <circle cx={x1} cy={y(r.now)} r={4} fill={c} />
            <T x={x0 - 10} y={left[i] + 4} anchor="end" fill={hot ? ACC : INK} bold={hot}>{`${clip(r.segment, 18)} ${pct(r.before)}`}</T>
            <T x={x1 + 10} y={right[i] + 4} fill={hot ? ACC : INK} bold={hot}>{pct(r.now)}</T>
          </g>
        );
      })}
    </Svg>
  );
}

export interface Mover { market: string; now: number; usual: number; change: number; ratio: number | null }

const money = (v: number): string =>
  Math.abs(v) >= 1e6 ? `$${(v / 1e6).toFixed(1)}M` : Math.abs(v) >= 1e4 ? `$${Math.round(v / 1e3)}k` : `$${Math.round(v).toLocaleString("en-US")}`;

/** Ranked by how many more dollars each market traded than on a usual day. */
export function MoverBars({ markets }: { markets: Mover[] }) {
  const rows = markets.slice(0, 5).filter((m) => m.change > 0);
  const max = Math.max(...rows.map((m) => m.change), 1);
  const bx = 180;
  const bw = 130;
  return (
    <Svg w={440} h={rows.length * 34 + 22} label="Markets that traded the most above their usual daily volume yesterday">
      {rows.map((m, i) => {
        const yy = 16 + i * 34;
        const hot = i === 0;
        return (
          <g key={m.market}>
            <T x={0} y={yy + 4} fill={INK}>{clip(m.market, 28)}</T>
            <rect x={bx} y={yy - 8} width={Math.max(2, (m.change / max) * bw)} height={16} rx={2} fill={hot ? ACC : MARK} />
            <T x={bx + bw + 8} y={yy + 4} fill={hot ? ACC : MUTED} bold={hot}>
              {`+${money(m.change)} · ${m.ratio ? `${m.ratio.toFixed(1)}× usual` : "new"}`}
            </T>
          </g>
        );
      })}
      <T x={bx} y={rows.length * 34 + 16}>dollars above a usual day</T>
    </Svg>
  );
}

// ------------------------------------------------------------------ what traders believe

export interface Swing { market: string; outcome: string; open: number; close: number }

export function Swings({ swings }: { swings: Swing[] }) {
  const rows = swings.slice(0, 5);
  const x0 = 290;
  const x = (v: number) => x0 + v * 480;
  return (
    <Svg w={900} h={rows.length * 36 + 52} label="Biggest odds moves yesterday, from the first price of the day to the last">
      {[0, 0.25, 0.5, 0.75, 1].map((v) => (
        <g key={v}>
          <line x1={x(v)} y1={20} x2={x(v)} y2={rows.length * 36 + 22} stroke={LINE} strokeDasharray={v === 0.5 ? undefined : "2 4"} />
          <T x={x(v)} y={12} anchor="middle">{cents(v)}</T>
        </g>
      ))}
      {rows.map((s, i) => {
        const yy = 38 + i * 36;
        const hot = i === 0;
        const c = hot ? ACC : INK;
        return (
          <g key={s.market}>
            <T x={0} y={yy + 4} fill={INK}>{clip(`${s.market} (${s.outcome})`, 44)}</T>
            <line x1={x(s.open)} y1={yy} x2={x(s.close)} y2={yy} stroke={hot ? ACC : MARK} strokeWidth={4} strokeLinecap="round" />
            <circle cx={x(s.open)} cy={yy} r={5} fill="var(--gc-panel)" stroke={c} strokeWidth={2} />
            <circle cx={x(s.close)} cy={yy} r={6} fill={c} />
            <T x={Math.max(x(s.open), x(s.close)) + 14} y={yy + 4} fill={hot ? ACC : MUTED} bold={hot}>{`${cents(s.open)} → ${cents(s.close)}`}</T>
          </g>
        );
      })}
      <T x={x0} y={rows.length * 36 + 44}>○ first price of the day · ● last · the price is the chance traders give it</T>
    </Svg>
  );
}

// ------------------------------------------------------------------ who is trading

export interface DayValue { day: string; value: number }

export function DayBars({ days, highlight, mean, label }: { days: DayValue[]; highlight?: string; mean?: number; label: string }) {
  const max = Math.max(...days.map((d) => d.value), mean ?? 0, 0.01);
  const w = 430;
  const step = (w - 40) / Math.max(days.length, 1);
  const bw = Math.min(40, step * 0.7);
  const y = (v: number) => 150 - (v / max) * 120;
  return (
    <Svg w={w} h={176} label={label}>
      {mean !== undefined && (
        <g>
          <line x1={30} y1={y(mean)} x2={w} y2={y(mean)} stroke={MUTED} strokeDasharray="3 3" />
          <T x={26} y={y(mean) + 4} anchor="end">avg</T>
        </g>
      )}
      {days.map((d, i) => {
        const cx = 40 + i * step + step / 2;
        const hot = d.day === highlight;
        return (
          <g key={d.day}>
            <rect x={cx - bw / 2} y={y(d.value)} width={bw} height={150 - y(d.value)} rx={2} fill={hot ? ACC : MARK} />
            <T x={cx} y={y(d.value) - 6} anchor="middle" fill={hot ? ACC : MUTED} bold={hot}>{pct(d.value)}</T>
            <T x={cx} y={168} anchor="middle">{shortDay(d.day)}</T>
          </g>
        );
      })}
    </Svg>
  );
}

export interface Cohort { day: string; new: number; came_back: number; rate: number }

export function Retention({ days }: { days: Cohort[] }) {
  return (
    <DayBars
      days={days.map((d) => ({ day: d.day, value: d.rate }))}
      highlight={days[days.length - 1]?.day}
      label="Share of each day's new traders who traded again the next day"
    />
  );
}

// ------------------------------------------------------------------ how the market behaves

export function Heatmap({ grid, peak }: { grid: { weekday: string; hours: (number | null)[] }[]; peak?: { weekday: string; hour: number } }) {
  const cw = 32;
  const ch = 20;
  const x0 = 40;
  const y0 = 18;
  const max = Math.max(...grid.flatMap((r) => r.hours.filter((v): v is number => v !== null)), 1);
  return (
    <Svg w={x0 + 24 * cw + 4} h={y0 + 7 * ch + 26} label="Average volume by weekday and hour, UTC">
      {Array.from({ length: 8 }, (_, k) => k * 3).map((h) => (
        <T key={h} x={x0 + h * cw + cw / 2} y={12} anchor="middle">{`${String(h).padStart(2, "0")}:00`}</T>
      ))}
      {grid.map((r, d) => (
        <g key={r.weekday}>
          <T x={x0 - 6} y={y0 + d * ch + 14} anchor="end">{r.weekday.slice(0, 3)}</T>
          {r.hours.map((v, h) => {
            const isPeak = peak?.weekday === r.weekday && peak.hour === h;
            return (
              <rect
                key={h}
                x={x0 + h * cw + 1}
                y={y0 + d * ch + 1}
                width={cw - 2}
                height={ch - 2}
                rx={2}
                fill={v === null ? LINE : ACC}
                fillOpacity={v === null ? 0.5 : 0.06 + 0.9 * (v / max)}
                stroke={isPeak ? INK : undefined}
                strokeWidth={isPeak ? 2 : undefined}
              />
            );
          })}
        </g>
      ))}
      <T x={x0} y={y0 + 7 * ch + 18}>lighter: less money · darker: more · outlined: the busiest hour · grey: no data yet</T>
    </Svg>
  );
}

export function Concentration({ markets, forHalf }: { markets: number; forHalf: number }) {
  const L = 80;
  const W = 330;
  const share = markets ? forHalf / markets : 0;
  const mw = Math.max(3, W * share);
  return (
    <Svg w={420} h={144} label={`Half of all volume came from ${forHalf} of ${markets} markets`}>
      <T x={0} y={31} fill={INK}>Volume</T>
      <rect x={L} y={16} width={W / 2} height={22} fill={ACC} />
      <rect x={L + W / 2} y={16} width={W / 2} height={22} fill={MARK} />
      <T x={L + 8} y={31} fill="var(--gc-panel)" bold>half</T>
      <T x={L + W / 2 + 8} y={31} fill={INK}>the other half</T>
      <path d={`M${L} 38 L${L} 96 L${L + mw} 96 L${L + W / 2} 38 Z`} fill={ACC} fillOpacity={0.15} />
      <T x={0} y={111} fill={INK}>Markets</T>
      <rect x={L} y={96} width={mw} height={22} fill={ACC} />
      <rect x={L + mw} y={96} width={W - mw} height={22} fill={MARK} />
      <T x={L} y={136} fill={ACC} bold>{`${forHalf.toLocaleString("en-US")} market${forHalf === 1 ? "" : "s"}`}</T>
      <T x={L + W} y={136} anchor="end">{`${(markets - forHalf).toLocaleString("en-US")} others`}</T>
    </Svg>
  );
}

export function KindBars({ days }: { days: DayValue[] }) {
  const L = 40;
  const W = 370;
  return (
    <Svg w={420} h={days.length * 20 + 24} label="Share of volume in recurring Up or Down crypto markets, by day">
      <T x={L} y={12} fill={ACC} bold>recurring crypto rounds</T>
      <T x={L + W} y={12} anchor="end">real events</T>
      {days.map((d, i) => {
        const yy = 20 + i * 20;
        const w = W * d.value;
        return (
          <g key={d.day}>
            <T x={L - 6} y={yy + 12} anchor="end">{shortDay(d.day)}</T>
            <rect x={L} y={yy} width={w} height={14} fill={ACC} />
            <rect x={L + w} y={yy} width={W - w} height={14} fill={MARK} />
            <T x={L + w + 6} y={yy + 11} fill={INK}>{pct(d.value)}</T>
          </g>
        );
      })}
    </Svg>
  );
}

// ------------------------------------------------------------------ what the watch caught

export function WatchStrip({ from, alarmHours }: { from: string; alarmHours: number[] }) {
  const W = 900;
  const x = (h: number) => 20 + h * 35.8;
  const hot = new Set(alarmHours);
  return (
    <Svg w={W} h={60} label={`Hourly checks over the day from ${from}, with the hours that alarmed`}>
      <line x1={x(0)} y1={30} x2={x(24)} y2={30} stroke={LINE} strokeWidth={8} strokeLinecap="round" />
      {Array.from({ length: 24 }, (_, h) =>
        hot.has(h) ? (
          <g key={h}>
            <circle cx={x(h) + 18} cy={30} r={8} fill={ACC} />
            <T x={x(h) + 18} y={14} anchor="middle" fill={ACC} bold>{`${String(h).padStart(2, "0")}:00`}</T>
          </g>
        ) : (
          <circle key={h} cx={x(h) + 18} cy={30} r={3} fill="var(--gc-ok)" />
        ),
      )}
      {[0, 6, 12, 18, 24].map((h) => (
        <T key={h} x={x(h)} y={54} anchor={h === 24 ? "end" : "start"}>{`${String(h % 24).padStart(2, "0")}:00`}</T>
      ))}
    </Svg>
  );
}

import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import type { SeriesView } from "../api";

// The Now page's backdrop: every watched series as one ridge of an ASCII ridgeline that fills the whole screen
// behind the page, nearer ridges hiding the ones behind them (the Unknown Pleasures plot, drawn in type). Each
// ridge is that series' real last 48 hours, scaled to its own peak. A sweep crosses the page and raises the ridges
// as it passes; where an hour sits outside its usual range the ridge turns accent and a flare goes out, labelled with
// the series, the hour and how far off it was. Hours with no trades are static, labelled as a collector gap. Hover a
// tile and its ridge lights up across the page.
//
// Every glyph comes from /api/now, so this is decoration made of data, never decoration pretending to be data.
// The tiles carry the same numbers accessibly, so the canvas is aria-hidden. The page's text is measured so no glyph
// is drawn under it. Under prefers-reduced-motion it draws once, fully raised, with no flares or static.

const SWEEP_SECONDS = 9;
const HOLD_SECONDS = 4;
const FALL_SECONDS = 1.6;
const LEVELS = 6;

type Rgb = string; // "r,g,b"
type Point = SeriesView["points"][number];

/** A theme colour as "r,g,b": the first of `names` that resolves to a hex value, else the fallback. */
function cssRgb(names: string[], fallback: Rgb): Rgb {
  const style = getComputedStyle(document.documentElement);
  for (const name of names) {
    const hex = style.getPropertyValue(name).trim().replace("#", "");
    if (/^[0-9a-f]{6}$/i.test(hex)) return [0, 2, 4].map((k) => parseInt(hex.slice(k, k + 2), 16)).join(",");
  }
  return fallback;
}

function hourLabel(t: string | undefined) {
  if (!t) return "";
  const d = new Date(t.endsWith("Z") ? t : `${t}Z`);
  return `${d.toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" })} ${String(d.getUTCHours()).padStart(2, "0")}:00 UTC`;
}

const outside = (p: Point) => p.value !== null && p.low !== null && p.high !== null && (p.value > p.high || p.value < p.low);

interface Lane {
  s: SeriesView;
  /** Values scaled 0..1 to this series' own peak; null where no trades were collected. */
  norm: (number | null)[];
}

interface Flare {
  x: number;
  y: number;
  t0: number;
  text: string;
}

function startField(canvas: HTMLCanvasElement, series: SeriesView[]) {
  const ctx = canvas.getContext("2d");
  if (!ctx || typeof ResizeObserver === "undefined") return null;
  const lanes: Lane[] = series
    .filter((s) => s.points.length > 1)
    .map((s) => {
      // Scaled to the series' own low and high, with a floor, so every ridge shows its shape.
      const vals = s.points.map((p) => p.value).filter((v): v is number => v !== null);
      const lo = vals.length ? Math.min(...vals) : 0;
      const hi = vals.length ? Math.max(...vals) : 1;
      return { s, norm: s.points.map((p) => (p.value === null ? null : 0.12 + 0.88 * ((p.value - lo) / (hi - lo || 1)))) };
    });
  if (!lanes.length) return null;
  const hours = Math.max(...lanes.map((l) => l.norm.length));
  // The collector gap: the trailing run of hours where no series has a value.
  let gapFrom = hours;
  while (gapFrom > 0 && lanes.every((l) => l.norm[gapFrom - 1] === null || l.norm[gapFrom - 1] === undefined)) gapFrom--;
  // Hours with data get the width; a trailing collector gap is squeezed into a labelled band at the right edge,
  // so an outage reads as an outage without flattening the poster.
  const hasGap = gapFrom < hours && gapFrom > 1;
  const dataShare = hasGap ? 0.84 : 1;
  /** The fractional hour index drawn at screen fraction x. */
  const hourAt = (x: number) =>
    x <= dataShare ? (x / dataShare) * ((hasGap ? gapFrom : hours) - 1) : gapFrom + ((x - dataShare) / (1 - dataShare)) * (hours - 1 - gapFrom);
  /** The screen fraction where hour index i is drawn. */
  const xOfHour = (i: number) =>
    !hasGap || i < gapFrom ? (i / Math.max(1, (hasGap ? gapFrom : hours) - 1)) * dataShare : dataShare + ((i - gapFrom) / Math.max(1, hours - 1 - gapFrom)) * (1 - dataShare);
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");

  let ink = "", muted = "", accent = "";
  const colours = () => {
    ink = cssRgb(["--color-ink", "--gc-ink"], "23,20,15");
    muted = cssRgb(["--color-ink-muted", "--gc-muted"], "110,102,90");
    accent = cssRgb(["--color-accent", "--gc-accent"], "193,58,20");
  };
  colours();

  let W = 0, H = 0, dpr = 1, cw = 8, ch = 13, cols = 0, rows = 0;
  let stamp = new Uint32Array(0);
  let veil = new Uint8Array(0);
  let frameId = 0;
  const cells: number[][] = [];
  const chars: string[][] = [];
  const styles: string[] = [];
  for (let k = 0; k < 3 * LEVELS; k++) {
    cells.push([]);
    chars.push([]);
    styles.push("");
  }
  const restyle = () => {
    [ink, muted, accent].forEach((rgb, k) => {
      for (let l = 0; l < LEVELS; l++) styles[k * LEVELS + l] = `rgba(${rgb},${(0.1 + (0.88 * l) / (LEVELS - 1)).toFixed(3)})`;
    });
  };
  restyle();

  let time = 0;
  let phase: "sweep" | "hold" | "fall" = "sweep";
  let phaseTime = 0;
  let flares: Flare[] = [];
  let fired = new Set<string>();
  let hovered: string | null = null;
  let raf = 0, last = 0, stopped = false;

  function measure() {
    // The canvas's own box, not window.innerWidth: the two differ when a scrollbar or zoom is involved, and the
    // text map below is in the same CSS pixels as this box.
    const box = canvas.getBoundingClientRect();
    W = box.width;
    H = box.height;
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    cw = Math.max(7, Math.min(10, Math.round(W / 160)));
    ch = Math.round(cw * 1.6);
    cols = Math.ceil(W / cw);
    rows = Math.ceil(H / ch);
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    stamp = new Uint32Array(cols * rows);
    measureText();
    if (reduced.matches) draw();
  }

  /** Cells under the page's text stay empty, so the ridges never run through a sentence. */
  function measureText() {
    veil = new Uint8Array(cols * rows);
    const mark = (r: DOMRect, pad: number) => {
      if (r.bottom < 0 || r.top > H) return;
      const c0 = Math.max(0, Math.floor((r.left - pad) / cw));
      const c1 = Math.min(cols - 1, Math.floor((r.right + pad) / cw));
      const r0 = Math.max(0, Math.floor((r.top - pad) / ch));
      const r1 = Math.min(rows - 1, Math.floor((r.bottom + pad) / ch));
      for (let y = r0; y <= r1; y++) veil.fill(1, y * cols + c0, y * cols + c1 + 1);
    };
    document.querySelectorAll("h1, h2, h3, p").forEach((el) => {
      const range = document.createRange();
      range.selectNodeContents(el);
      for (const r of Array.from(range.getClientRects())) mark(r, 6);
    });
    // Panels and the nav are opaque already; marking them just saves drawing what can't be seen.
    document.querySelectorAll("header nav, main .panel, main button, main form").forEach((el) => mark(el.getBoundingClientRect(), 0));
  }

  function put(cell: number, glyph: string, colour: 0 | 1 | 2, alpha: number) {
    if (cell < 0 || cell >= stamp.length || stamp[cell] === frameId) return;
    stamp[cell] = frameId;
    if (veil[cell] || glyph === " " || alpha < 0.1) return;
    const level = Math.max(0, Math.min(LEVELS - 1, Math.round(((alpha - 0.1) / 0.88) * (LEVELS - 1))));
    const b = colour * LEVELS + level;
    cells[b].push(cell);
    chars[b].push(glyph);
  }

  /** How far the sweep has raised column c: 0 flat, 1 full height. */
  function raised(c: number) {
    if (reduced.matches) return 1;
    const x = c / cols;
    if (phase === "sweep") {
      const front = phaseTime / SWEEP_SECONDS;
      return Math.max(0, Math.min(1, (front - x) * 9));
    }
    if (phase === "hold") return 1;
    // Fall: the ridges settle back, left to right.
    const front = (phaseTime / FALL_SECONDS) * 1.3;
    return Math.max(0, Math.min(1, (x - front) * 6 + 1));
  }

  function valueAt(lane: Lane, c: number) {
    const f = Math.min(lane.norm.length - 1, hourAt(c / Math.max(1, cols - 1)));
    const i = Math.floor(f);
    const a = lane.norm[i];
    const b = lane.norm[Math.min(lane.norm.length - 1, i + 1)];
    if (a === null || a === undefined) return null;
    if (b === null || b === undefined) return a;
    return a + (b - a) * (f - i);
  }

  function draw() {
    if (!W) return;
    frameId++;
    for (const b of cells) b.length = 0;
    for (const b of chars) b.length = 0;

    const top = 3;
    const n = lanes.length;
    const spacing = Math.max(2, (rows - top - 2) / n);
    const amp = Math.min(10, spacing * 3.6);
    const sweepCol = phase === "sweep" && !reduced.matches ? Math.floor((phaseTime / SWEEP_SECONDS) * cols) : -1;

    /** Write text along a row, clipped at the edges (a cell past the right edge would wrap to the next row). */
    const write = (r: number, c0: number, text: string, colour: 0 | 1 | 2, alpha: number) => {
      for (let k = 0; k < text.length; k++) {
        const c = c0 + k;
        if (c >= 0 && c < cols && r >= 0 && r < rows) put(r * cols + c, text[k], colour, alpha);
      }
    };

    /** A row near `row` where `len` cells from c0 are clear of the page's text, so labels stay whole. */
    const clearRow = (row: number, c0: number, len: number) => {
      for (const d of [0, -1, 1, -2, 2, -3, 3, -4, 4, -5, 5, -6, 6]) {
        const r = row + d;
        if (r < 0 || r >= rows) continue;
        let ok = true;
        for (let k = 0; k < len && ok; k++) if (veil[r * cols + c0 + k]) ok = false;
        if (ok) return r;
      }
      return -1;
    };

    // Flare labels and rings first: they sit on top of everything.
    for (const f of flares) {
      const age = time - f.t0;
      const life = 1 - age / 3;
      if (life <= 0) continue;
      const radius = age * 18;
      const steps = Math.max(10, Math.round(radius * 3));
      for (let k = 0; k < steps && age < 1.2; k++) {
        const a = (k / steps) * Math.PI * 2;
        const cc = Math.round(f.x + Math.cos(a) * radius);
        const rr = Math.round(f.y + (Math.sin(a) * radius) / 1.6);
        if (cc >= 0 && rr >= 0 && cc < cols && rr < rows) put(rr * cols + cc, ".", 2, 0.7 * (1 - age / 1.2));
      }
      const c0 = Math.min(cols - f.text.length - 1, Math.max(1, Math.round(f.x) - 2));
      const r = clearRow(Math.max(0, Math.round(f.y) - 2), c0, f.text.length);
      if (r < 0) continue;
      // A blank margin either side so the label reads against the ridges.
      write(r, c0 - 1, ` ${f.text} `, 2, 0.95 * Math.min(1, life * 2));
    }

    // The collector gap's label, once the sweep reaches it.
    if (hasGap) {
      const gc = Math.floor(dataShare * (cols - 1));
      const lastTrade = lanes[0].s.points[gapFrom - 1]?.t;
      const lines = ["collector gap", "no trades since", hourLabel(lastTrade), `${hours - gapFrom} h, squeezed`];
      if (reduced.matches || sweepCol < 0 || sweepCol >= gc) {
        lines.forEach((text, li) => {
          // Right-aligned so it fits even where the band is narrower than the words (phones), on a blank margin.
          const c0 = Math.max(1, cols - text.length - 1);
          // Below the nav, which sits over the top rows.
          const r = clearRow(top + 4 + li, c0 - 1, text.length + 1);
          if (r >= 0) write(r, c0 - 1, ` ${text}`, li === 0 ? 2 : 1, li === 0 ? 0.85 : 0.75);
        });
      }
      // The band's left edge.
      for (let r = top; r < rows; r += 2) put(r * cols + gc, ":", 1, 0.3);
    }

    // The hovered series' name, above its ridge (drawn first so the ridges leave it whole).
    lanes.forEach((lane, li) => {
      if (hovered !== lane.s.series) return;
      const base = top + (li + 1) * spacing;
      const label = `${lane.s.label} · last 48 hours`;
      const r = clearRow(Math.max(0, Math.round(base - amp) - 1), 1, label.length);
      if (r >= 0) write(r, 1, label, 0, 0.95);
    });

    // Ridges, front (bottom) first, so nearer ridges hide the ones behind them.
    for (let li = n - 1; li >= 0; li--) {
      const lane = lanes[li];
      const base = top + (li + 1) * spacing;
      const lit = hovered === lane.s.series;
      const ys: (number | null)[] = [];
      for (let c = 0; c < cols; c++) {
        const v = valueAt(lane, c);
        ys.push(v === null ? null : base - v * amp * raised(c));
      }
      for (let c = 0; c < cols; c++) {
        const y = ys[c];
        const swept = reduced.matches || phase !== "sweep" || c <= sweepCol;
        if (y === null) {
          // No trades this hour: static on the baseline.
          const r = Math.round(base);
          if (r >= 0 && r < rows) {
            const g = reduced.matches ? "." : ".:' "[Math.floor(Math.random() * 4)];
            put(r * cols + c, g, 1, swept ? 0.3 : 0.16);
          }
          continue;
        }
        const r = Math.round(y);
        const prev = ys[c - 1] ?? y;
        const next = ys[c + 1] ?? y;
        const dy = next - prev;
        const glyph = Math.abs(dy) < 0.5 ? (y - Math.floor(y) > 0.5 ? "_" : "-") : dy < 0 ? "/" : "\\";
        const hourIdx = Math.min(lane.s.points.length - 1, Math.round(hourAt(c / Math.max(1, cols - 1))));
        const bad = swept && outside(lane.s.points[hourIdx]);
        const alpha = lit ? 0.95 : swept ? 0.4 + 0.35 * (li / n) : 0.16;
        if (r >= 0 && r < rows) put(r * cols + c, glyph, bad ? 2 : 0, bad ? 0.9 : alpha);
        // Steep runs: fill the gap to the next column so the line stays connected.
        const rn = Math.round(next);
        for (let k = Math.min(r, rn) + 1; k < Math.max(r, rn); k++) if (k >= 0 && k < rows) put(k * cols + c, "|", bad ? 2 : 0, bad ? 0.9 : alpha);
        // Occlusion: claim the cells under the ridge down to its baseline, blank.
        for (let k = Math.max(r, rn) + 1; k <= Math.round(base); k++) if (k >= 0 && k < rows) put(k * cols + c, " ", 0, 0);
      }
    }

    // The sweep beam.
    if (sweepCol >= 0 && sweepCol < cols) for (let r = top; r < rows; r += 1) put(r * cols + sweepCol, "¦", 2, 0.28);

    ctx!.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx!.clearRect(0, 0, W, H);
    ctx!.font = `500 ${Math.round(ch * 0.74)}px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`;
    ctx!.textAlign = "center";
    ctx!.textBaseline = "middle";
    for (let b = 0; b < cells.length; b++) {
      if (!cells[b].length) continue;
      ctx!.fillStyle = styles[b];
      const list = cells[b];
      const glyphs = chars[b];
      for (let k = 0; k < list.length; k++) ctx!.fillText(glyphs[k], (list[k] % cols) * cw + cw / 2, Math.floor(list[k] / cols) * ch + ch / 2);
    }
  }

  /** Fire a flare for each out-of-range hour the sweep has just crossed. */
  function fireFlares(fromCol: number, toCol: number) {
    const top = 3;
    const spacing = Math.max(2, (rows - top - 2) / lanes.length);
    const amp = Math.min(10, spacing * 3.6);
    lanes.forEach((lane, li) => {
      const pts = lane.s.points;
      for (let i = 0; i < pts.length; i++) {
        const c = Math.round(xOfHour(i) * (cols - 1));
        if (c < fromCol || c > toCol || !outside(pts[i])) continue;
        const key = `${lane.s.series}@${i}`;
        if (fired.has(key)) continue;
        fired.add(key);
        // One label per lane per sweep keeps the page readable; the ridge still turns accent at every hour.
        if (flares.length >= 3 || flares.some((f) => f.text.startsWith(lane.s.label) && time - f.t0 < 3)) continue;
        const p = pts[i];
        const how = p.usual ? `${(p.value! / p.usual).toFixed(1)}× usual` : p.value! > p.high! ? "above usual" : "below usual";
        const arrow = p.value! > p.high! ? "↑" : "↓";
        const base = top + (li + 1) * spacing;
        flares.push({ x: c, y: base - (lane.norm[i] ?? 0) * amp, t0: time, text: `${lane.s.label} ${arrow} ${how} · ${hourLabel(p.t)}` });
      }
    });
  }

  function frame(now: number) {
    // Capped generously so a throttled or slow device still sweeps at the right pace.
    const dt = Math.min(0.5, (now - last) / 1000);
    last = now;
    time += dt;
    phaseTime += dt;
    if (phase === "sweep") {
      const before = Math.floor(((phaseTime - dt) / SWEEP_SECONDS) * cols);
      fireFlares(before, Math.floor((phaseTime / SWEEP_SECONDS) * cols));
      if (phaseTime >= SWEEP_SECONDS) {
        phase = "hold";
        phaseTime = 0;
      }
    } else if (phase === "hold" && phaseTime >= HOLD_SECONDS) {
      phase = "fall";
      phaseTime = 0;
    } else if (phase === "fall" && phaseTime >= FALL_SECONDS) {
      phase = "sweep";
      phaseTime = 0;
      fired = new Set();
    }
    flares = flares.filter((f) => time - f.t0 < 3);
    draw();
    raf = requestAnimationFrame(frame);
  }

  function schedule() {
    cancelAnimationFrame(raf);
    if (stopped || document.hidden) return;
    if (reduced.matches) {
      draw();
      return;
    }
    last = performance.now();
    raf = requestAnimationFrame(frame);
  }

  let scrollQueued = false;
  const onScroll = () => {
    if (scrollQueued) return;
    scrollQueued = true;
    requestAnimationFrame(() => {
      scrollQueued = false;
      measureText();
      if (reduced.matches) draw();
    });
  };
  const onOver = (e: PointerEvent) => {
    const tile = (e.target as Element | null)?.closest?.("[data-testid^='tile-']");
    hovered = tile ? tile.getAttribute("data-testid")!.slice("tile-".length) : null;
    if (reduced.matches) draw();
  };
  const onTheme = () => {
    colours();
    restyle();
    draw();
  };

  const resizer = new ResizeObserver(measure);
  resizer.observe(canvas);
  // The page's layout settles after data and fonts load; re-measure the text now and then.
  const relayout = window.setInterval(() => {
    measureText();
    if (reduced.matches) draw();
  }, 1500);
  window.addEventListener("scroll", onScroll, { passive: true });
  document.addEventListener("pointerover", onOver);
  document.addEventListener("visibilitychange", schedule);
  reduced.addEventListener("change", schedule);
  const scheme = window.matchMedia("(prefers-color-scheme: dark)");
  scheme.addEventListener("change", onTheme);
  measure();
  schedule();

  return {
    stop() {
      stopped = true;
      cancelAnimationFrame(raf);
      resizer.disconnect();
      clearInterval(relayout);
      window.removeEventListener("scroll", onScroll);
      document.removeEventListener("pointerover", onOver);
      document.removeEventListener("visibilitychange", schedule);
      reduced.removeEventListener("change", schedule);
      scheme.removeEventListener("change", onTheme);
    },
  };
}

export function MarketField({ series }: { series: SeriesView[] }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const field = startField(canvas, series);
    return () => field?.stop();
  }, [series]);
  // Fixed behind the whole page: above the paper and its grain, below everything else. Portalled to <body> because
  // an ancestor in the page makes its own containing block, which would trap a fixed canvas inside the column.
  return createPortal(
    <canvas ref={ref} aria-hidden="true" data-testid="market-field" className="pointer-events-none fixed inset-0 h-full w-full" style={{ zIndex: -1 }} />,
    document.body,
  );
}

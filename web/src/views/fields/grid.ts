// The plumbing every page backdrop shares: a monospace cell grid on a canvas fixed behind the page, a veil over the
// page's own text, batched drawing by colour and alpha, and a frame loop that pauses when the tab is hidden and draws
// one still frame under prefers-reduced-motion. A scene only says which glyph goes in which cell for a given time.
//
// The Now page's MarketField predates this and keeps its own copy of the same mechanics (DECISIONS 2026-10-27).

const LEVELS = 6;

/** 0 ink, 1 muted, 2 accent. */
export type Colour = 0 | 1 | 2;

export interface Grid {
  readonly cols: number;
  readonly rows: number;
  /** Seconds since the scene started (fixed at the scene's stillTime under reduced motion). */
  readonly time: number;
  readonly reduced: boolean;
  /** Claim a cell: the first claim in a frame wins. Alpha below 0.1 or a space claims it blank. */
  put(c: number, r: number, glyph: string, colour: Colour, alpha: number): void;
  /** Text along a row, clipped at the edges. */
  write(r: number, c0: number, text: string, colour: Colour, alpha: number): void;
  /** True when `len` cells from (c0, r) are on screen and clear of the page's text. */
  clear(r: number, c0: number, len: number): boolean;
  /** A row near `row` where `len` cells from c0 are clear, or -1. */
  clearRow(row: number, c0: number, len: number): number;
}

export interface Scene {
  draw(g: Grid): void;
  /** The moment drawn under prefers-reduced-motion. */
  stillTime?: number;
}

/** A theme colour as "r,g,b": the first of `names` that resolves to a hex value, else the fallback. */
function cssRgb(names: string[], fallback: string): string {
  const style = getComputedStyle(document.documentElement);
  for (const name of names) {
    const hex = style.getPropertyValue(name).trim().replace("#", "");
    if (/^[0-9a-f]{6}$/i.test(hex)) return [0, 2, 4].map((k) => parseInt(hex.slice(k, k + 2), 16)).join(",");
  }
  return fallback;
}

/** A stable pseudo-random number in [0, 1) for an integer seed, so scenes jitter the same way every frame. */
export function hash(n: number): number {
  const x = Math.sin(n * 12.9898 + 78.233) * 43758.5453;
  return x - Math.floor(x);
}

/** The cells of a straight stroke from (c0, r0) to (c1, r1) (Bresenham), each with a glyph for the step it takes:
 * `-` along a row, `|` down a column, `/` or `\` where it steps both ways. `visit` gets the cell, the glyph and how
 * far along the stroke it is (0..1). */
export function stroke(c0: number, r0: number, c1: number, r1: number, visit: (c: number, r: number, glyph: string, at: number) => void) {
  c0 = Math.round(c0); r0 = Math.round(r0); c1 = Math.round(c1); r1 = Math.round(r1);
  const dx = Math.abs(c1 - c0), dy = Math.abs(r1 - r0);
  const sx = c0 < c1 ? 1 : -1, sy = r0 < r1 ? 1 : -1;
  const diagonal = sx === sy ? "\\" : "/";
  const n = Math.max(dx, dy) || 1;
  let err = dx - dy, c = c0, r = r0;
  for (let k = 0; ; k++) {
    if (c === c1 && r === r1) {
      visit(c, r, dx >= dy ? "-" : "|", 1);
      break;
    }
    const e2 = 2 * err;
    const stepC = e2 > -dy, stepR = e2 < dx;
    visit(c, r, stepC && stepR ? diagonal : stepR ? "|" : "-", k / n);
    if (stepC) { err -= dy; c += sx; }
    if (stepR) { err += dx; r += sy; }
  }
}

/** Start a scene on a canvas. Returns null where there is no 2D context or ResizeObserver (jsdom, old browsers). */
export function startScene(canvas: HTMLCanvasElement, scene: Scene) {
  const ctx = canvas.getContext("2d");
  if (!ctx || typeof ResizeObserver === "undefined") return null;
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");

  let ink = "", muted = "", accent = "";
  const styles: string[] = [];
  const restyle = () => {
    ink = cssRgb(["--color-ink", "--gc-ink"], "23,20,15");
    muted = cssRgb(["--color-ink-muted", "--gc-muted"], "110,102,90");
    accent = cssRgb(["--color-accent", "--gc-accent"], "193,58,20");
    [ink, muted, accent].forEach((rgb, k) => {
      for (let l = 0; l < LEVELS; l++) styles[k * LEVELS + l] = `rgba(${rgb},${(0.1 + (0.88 * l) / (LEVELS - 1)).toFixed(3)})`;
    });
  };
  restyle();

  let W = 0, H = 0, dpr = 1, cw = 8, ch = 13, cols = 0, rows = 0;
  let stamp = new Uint32Array(0);
  let veil = new Uint8Array(0);
  let frameId = 0;
  const cells: number[][] = Array.from({ length: 3 * LEVELS }, () => []);
  const chars: string[][] = Array.from({ length: 3 * LEVELS }, () => []);
  let time = 0, raf = 0, last = 0, stopped = false;

  const clear = (r: number, c0: number, len: number) => {
    if (r < 0 || r >= rows || c0 < 0 || c0 + len > cols) return false;
    for (let k = 0; k < len; k++) if (veil[r * cols + c0 + k]) return false;
    return true;
  };
  const grid: Grid = {
    get cols() { return cols; },
    get rows() { return rows; },
    get time() { return time; },
    get reduced() { return reduced.matches; },
    put(c, r, glyph, colour, alpha) {
      c = Math.round(c); r = Math.round(r);
      if (c < 0 || c >= cols || r < 0 || r >= rows) return;
      const cell = r * cols + c;
      if (stamp[cell] === frameId) return;
      stamp[cell] = frameId;
      if (veil[cell] || glyph === " " || alpha < 0.1) return;
      const level = Math.max(0, Math.min(LEVELS - 1, Math.round(((Math.min(alpha, 0.98) - 0.1) / 0.88) * (LEVELS - 1))));
      cells[colour * LEVELS + level].push(cell);
      chars[colour * LEVELS + level].push(glyph);
    },
    write(r, c0, text, colour, alpha) {
      for (let k = 0; k < text.length; k++) grid.put(c0 + k, r, text[k], colour, alpha);
    },
    clear,
    clearRow(row, c0, len) {
      for (let d = 0; d <= 6; d++) for (const r of d ? [row - d, row + d] : [row]) if (clear(r, c0, len)) return r;
      return -1;
    },
  };

  function measure() {
    // The canvas's own box, not window.innerWidth: the text veil is in the same CSS pixels as this box.
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

  /** Cells under the page's text stay empty, so no glyph runs through a sentence. */
  function measureText() {
    veil = new Uint8Array(cols * rows);
    const mark = (r: DOMRect, pad: number) => {
      if (r.bottom < 0 || r.top > H || r.width === 0) return;
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
    document.querySelectorAll("header nav, main .panel, main button, main form, main select, main label").forEach((el) => mark(el.getBoundingClientRect(), 0));
  }

  function draw() {
    if (!W) return;
    frameId++;
    for (const b of cells) b.length = 0;
    for (const b of chars) b.length = 0;
    scene.draw(grid);
    ctx!.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx!.clearRect(0, 0, W, H);
    ctx!.font = `500 ${Math.round(ch * 0.74)}px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`;
    ctx!.textAlign = "center";
    ctx!.textBaseline = "middle";
    for (let b = 0; b < cells.length; b++) {
      if (!cells[b].length) continue;
      ctx!.fillStyle = styles[b];
      const list = cells[b], glyphs = chars[b];
      for (let k = 0; k < list.length; k++) ctx!.fillText(glyphs[k], (list[k] % cols) * cw + cw / 2, Math.floor(list[k] / cols) * ch + ch / 2);
    }
  }

  function frame(now: number) {
    time += Math.min(0.5, (now - last) / 1000);
    last = now;
    draw();
    raf = requestAnimationFrame(frame);
  }

  function schedule() {
    cancelAnimationFrame(raf);
    if (stopped || document.hidden) return;
    if (reduced.matches) {
      time = scene.stillTime ?? time;
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
  const onTheme = () => {
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
      document.removeEventListener("visibilitychange", schedule);
      reduced.removeEventListener("change", schedule);
      scheme.removeEventListener("change", onTheme);
    },
  };
}

import { useMemo } from "react";
import type { CatalogResponse } from "../../api";
import { FieldCanvas } from "./FieldCanvas";
import { hash, type Grid, type Scene } from "./grid";

// The catalog's backdrop: a compiler at work on the semantic layer. The tokens of every KPI's formula and exact
// definition drift down the page as faint type; every few seconds one KPI's real label, plain-words formula and exact
// definition assemble out of scattered characters, hold, and dissolve, and the next KPI compiles. Every string comes
// from /api/catalog (the same text the cards print), so nothing here is invented.

const FLY = 1.6;
const HOLD = 4;
const DISSOLVE = 1.2;
const CYCLE = FLY + HOLD + DISSOLVE;

function compiler(cat: CatalogResponse): Scene {
  const kpis = cat.kpis.filter((k) => k.formula || k.definition);
  const tokens = [...new Set(kpis.flatMap((k) => `${k.formula} ${k.definition}`.split(/\s+/)).filter((t) => t.length > 1 && t.length < 24))];

  return {
    stillTime: FLY + 1,
    draw(g: Grid) {
      const { cols, rows, time } = g;
      if (!kpis.length) return;
      const cycle = Math.floor(time / CYCLE);
      const t = time - cycle * CYCLE;
      const k = kpis[cycle % kpis.length];

      // The assembled block: label, formula, definition, as three lines on a clear spot.
      const lines = [
        { text: k.label, colour: 2 as const, alpha: 0.95 },
        { text: `= ${k.formula}`, colour: 0 as const, alpha: 0.8 },
        { text: `→ ${k.definition.replace(/\s+/g, " ")}`, colour: 1 as const, alpha: 0.75 },
      ].map((l) => ({ ...l, text: l.text.length > cols - 6 ? `${l.text.slice(0, cols - 7)}…` : l.text }));
      const width = Math.max(...lines.map((l) => l.text.length));
      const spot = findSpot(g, width, lines.length, cycle);
      if (spot) {
        const fly = g.reduced ? 1 : Math.min(1, t / FLY);
        const ease = 1 - (1 - fly) ** 3;
        const gone = g.reduced ? 0 : Math.max(0, (t - FLY - HOLD) / DISSOLVE);
        lines.forEach((l, li) => {
          for (let i = 0; i < l.text.length; i++) {
            const seed = cycle * 977 + li * 131 + i;
            const tc = spot.c + i, tr = spot.r + li * 2;
            // Each character flies in from its own scattered start and scatters again on the way out.
            const sx = hash(seed) * cols, sy = hash(seed + 7) * rows;
            const ex = tc + (hash(seed + 3) - 0.5) * 30, ey = tr + (hash(seed + 5) - 0.2) * 20;
            const x = gone > 0 ? tc + (ex - tc) * gone : sx + (tc - sx) * ease;
            const y = gone > 0 ? tr + (ey - tr) * gone : sy + (tr - sy) * ease;
            const settled = fly >= 1 && gone === 0;
            g.put(x, y, l.text[i], settled ? l.colour : 1, (settled ? l.alpha : 0.55) * (1 - gone));
          }
        });
        // A cursor after the definition while it holds.
        if (!g.reduced && t > FLY && t < FLY + HOLD && Math.floor(time * 2) % 2 === 0) g.put(spot.c + lines[2].text.length + 1, spot.r + 4, "▌", 2, 0.7);
      }

      // The drifting tokens: columns of type falling slowly, each a real token from a formula or definition.
      const streams = Math.max(8, Math.floor(cols / 9));
      for (let s = 0; s < streams; s++) {
        const tok = tokens[(s * 7 + Math.floor((time * 0.08 + hash(s)) * 3)) % Math.max(1, tokens.length)] ?? "";
        const speed = 0.8 + hash(s + 40) * 1.6;
        const c = Math.floor(hash(s + 80) * (cols - tok.length));
        const r = Math.floor((hash(s + 120) * rows + time * speed) % (rows + 6)) - 3;
        g.write(r, c, tok, 1, 0.18 + 0.12 * hash(s + 160));
      }
    },
  };
}

/** A clear block for `lines` lines (spaced a row apart) of `width` cells: the same spot for a whole cycle. */
function findSpot(g: Grid, width: number, lines: number, seed: number) {
  const { cols, rows } = g;
  if (width + 4 > cols) return null;
  const fits = (r: number, c: number) => {
    for (let li = 0; li < lines; li++) if (!g.clear(r + li * 2, c, width)) return false;
    return true;
  };
  const cands = [cols - width - 3, 3, Math.floor((cols - width) / 2)];
  const start = Math.floor(hash(seed) * rows);
  for (let k = 0; k < rows; k++) {
    const r = (start + k * 3) % Math.max(1, rows - lines * 2);
    for (const c of cands) if (c >= 0 && fits(r, c)) return { r, c };
  }
  return null;
}

export function CompilerField({ catalog }: { catalog: CatalogResponse }) {
  const scene = useMemo(() => compiler(catalog), [catalog]);
  return <FieldCanvas scene={scene} testId="compiler-field" />;
}

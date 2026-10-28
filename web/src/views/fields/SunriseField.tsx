import { useMemo } from "react";
import type { Digest } from "../../api";
import { FieldCanvas } from "./FieldCanvas";
import { hash, type Grid, type Scene } from "./grid";

// The morning read's backdrop: a sun rising over a horizon drawn from the digest's own data. The horizon is the
// rhythm card's weekday x hour grid of volume laid end to end (Monday 00:00 to Sunday 23:00), scaled to its peak, so
// the skyline is the week's real trading rhythm; without that card it falls back to a daily series from another
// card, and with none it is flat. The sun rises, its rays (accent) turn, and the digest's own sentences drift across
// the sky one at a time. No number is drawn that the page doesn't already say.

const RISE = 12;
const HOLD = 6;
const CYCLE = RISE + HOLD;

/** The digest's own series for the horizon, and a caption saying what it is. */
function horizonOf(d: Digest): { values: number[]; caption: string } {
  const rhythm = d.cards.find((c) => c.id === "rhythm" && !c.insufficient)?.data.grid as { weekday: string; hours: (number | null)[] }[] | undefined;
  if (rhythm?.length) {
    const values = rhythm.flatMap((w) => w.hours.map((v) => v ?? 0));
    if (values.some((v) => v > 0)) return { values, caption: `horizon: volume by hour, ${rhythm[0].weekday} to ${rhythm[rhythm.length - 1].weekday}` };
  }
  for (const id of ["kind", "whales"]) {
    const card = d.cards.find((c) => c.id === id && !c.insufficient);
    const days = card?.data.days as { day: string; value: number }[] | undefined;
    if (days && days.length > 1 && days.some((x) => x.value > 0)) return { values: days.map((x) => x.value), caption: `horizon: ${card!.question.toLowerCase()}` };
  }
  return { values: [], caption: "" };
}

function sunrise(d: Digest): Scene {
  const { values, caption } = horizonOf(d);
  const peak = Math.max(1e-9, ...values);
  const norm = values.map((v) => v / peak);
  // The sentences the page already prints, headline first.
  const ticker = [d.headline?.text, ...d.cards.filter((c) => !c.insufficient).map((c) => c.sentence.text)]
    .filter((t, i, all): t is string => !!t && all.indexOf(t) === i);

  const heightAt = (x: number) => {
    if (norm.length < 2) return 0;
    const f = x * (norm.length - 1);
    const i = Math.floor(f);
    const a = norm[i], b = norm[Math.min(norm.length - 1, i + 1)];
    return a + (b - a) * (f - i);
  };

  return {
    stillTime: RISE,
    draw(g: Grid) {
      const { cols, rows, time } = g;
      const t = g.reduced ? RISE : time % CYCLE;
      const rise = Math.min(1, t / RISE);
      const eased = 1 - (1 - rise) ** 3;
      const base = Math.round(rows * 0.72);
      const amp = Math.min(rows * 0.22, 14);
      const horizon: number[] = [];
      for (let c = 0; c < cols; c++) horizon.push(base - heightAt(c / Math.max(1, cols - 1)) * amp);

      // The ticker: one sentence at a time, drifting right to left through the upper sky.
      if (ticker.length && !g.reduced) {
        const k = Math.floor(time / CYCLE) % ticker.length;
        const text = ` ${ticker[k]} `;
        const c0 = Math.round(cols - ((time % CYCLE) / CYCLE) * (cols + text.length));
        const r = g.clearRow(Math.round(rows * 0.12), Math.max(0, c0), Math.min(text.length, cols - Math.max(0, c0)));
        if (r >= 0) g.write(r, c0, text, 1, 0.75 * Math.min(1, rise * 3));
      }
      if (caption) {
        const r = g.clearRow(base + 3, 2, caption.length);
        if (r >= 0) g.write(r, 2, caption, 1, 0.6);
      }

      // The horizon line, and the ground under it claimed blank so the sun sets behind it.
      for (let c = 0; c < cols; c++) {
        const y = horizon[c];
        const dy = (horizon[c + 1] ?? y) - (horizon[c - 1] ?? y);
        const glyph = Math.abs(dy) < 0.5 ? (y - Math.floor(y) > 0.5 ? "_" : "-") : dy < 0 ? "/" : "\\";
        const r = Math.round(y);
        g.put(c, r, glyph, 0, 0.7);
        const rn = Math.round(horizon[c + 1] ?? y);
        for (let k = Math.min(r, rn) + 1; k < Math.max(r, rn); k++) g.put(c, k, "|", 0, 0.7);
        for (let k = Math.max(r, rn) + 1; k < rows; k++) {
          // Furrows: a few dots in the ground, thinning with depth.
          const h = hash(c * 131 + k * 7);
          g.put(c, k, (k - r) % 3 === 0 && h < 0.25 ? "." : " ", 1, 0.25 * (1 - (k - r) / (rows - r + 1)));
        }
      }

      // The sun, rising from below the horizon to a third of the way down the screen.
      const sc = cols * 0.62;
      const sr = base + 6 - eased * (base + 6 - rows * 0.34);
      const R = Math.max(5, Math.min(cols * 0.07, 14));
      const turn = time * 0.15;
      // The disc first, so it claims its cells before the rays.
      for (let r = Math.floor(sr - R / 1.6) - 1; r <= Math.ceil(sr + R / 1.6) + 1; r++) {
        for (let c = Math.floor(sc - R) - 1; c <= Math.ceil(sc + R) + 1; c++) {
          const d = Math.hypot(c - sc, (r - sr) * 1.6);
          if (d > R) continue;
          // A bright rim and a faint, speckled face, so the disc reads as a sun without a solid block behind the page.
          const rim = d > R - 1.3;
          g.put(c, r, rim ? "O" : d > R * 0.7 ? "o" : hash(c * 17 + r) < 0.35 ? "*" : hash(c * 5 + r * 3) < 0.5 ? "." : " ", 2, rim ? 0.85 : 0.3);
        }
      }
      const rays = 18;
      for (let k = 0; k < rays; k++) {
        const a = turn + (k / rays) * Math.PI * 2;
        const len = R * (k % 2 ? 1.6 : 2.6) * (0.4 + 0.6 * eased);
        const ax = Math.cos(a), ay = Math.sin(a) / 1.6;
        const deg = ((Math.atan2(Math.sin(a), Math.cos(a)) * 180) / Math.PI + 360) % 180;
        const glyph = deg < 22.5 || deg > 157.5 ? "-" : deg < 67.5 ? "\\" : deg < 112.5 ? "|" : "/";
        for (let s = R + 1.5; s < R + len; s += 0.8) {
          const c = sc + ax * s, r = sr + ay * s;
          g.put(c, r, glyph, 2, 0.75 * (1 - (s - R) / len) * Math.min(1, rise * 2));
        }
      }

      // Stars, fading as the sun comes up.
      for (let k = 0; k < 60; k++) {
        const c = Math.floor(hash(k) * cols), r = Math.floor(hash(k + 500) * base * 0.8);
        if (r < horizon[c] - 1) g.put(c, r, hash(k + 900) < 0.3 ? "+" : ".", 1, 0.5 * (1 - eased) * (0.6 + 0.4 * Math.sin(time * 2 + k)));
      }
    },
  };
}

export function SunriseField({ digest }: { digest: Digest }) {
  const scene = useMemo(() => sunrise(digest), [digest]);
  return <FieldCanvas scene={scene} testId="sunrise-field" />;
}

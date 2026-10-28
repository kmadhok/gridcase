import { useMemo } from "react";
import type { CatalogResponse } from "../../api";
import { FieldCanvas } from "./FieldCanvas";
import { hash, stroke, type Grid, type Scene } from "./grid";

// Explore's backdrop: the semantic layer as a graph. Each KPI in the catalog is a node down the left edge, each
// dimension a node down the right edge, and every KPI is joined to every dimension (any KPI can be sliced by any dimension, as the
// catalog says). Packets travel the edges; every few seconds one path lights up in accent from a KPI to a dimension
// and is captioned with the question it answers ("Trading volume by Category"). Names come from /api/catalog.

const PATH = 4.5;

function graph(cat: CatalogResponse): Scene {
  const kpis = cat.kpis.map((k) => k.label);
  const dims = cat.dimensions.map((d) => d.label);
  const edges = kpis.flatMap((_, i) => dims.map((__, j) => ({ i, j })));
  // A stride that shares no factor with the edge count, so the walk visits every pair before repeating.
  const gcd = (a: number, b: number): number => (b ? gcd(b, a % b) : a);
  const stride = [7, 5, 3, 11, 13, 1].find((s) => gcd(s, edges.length) === 1) ?? 1;

  return {
    stillTime: PATH * 0.6,
    draw(g: Grid) {
      const { cols, rows, time } = g;
      if (!kpis.length || !dims.length) return;
      const top = 4, span = rows - top - 3;
      // KPI nodes down the left edge with their names to the right; dimensions down the right edge, names to the left.
      // Edges run from the end of one name to the start of the other.
      const kpiAt = (i: number) => {
        const r = Math.round(top + ((i + 0.5) / kpis.length) * span);
        return { c: 2, r, out: Math.min(cols * 0.4, 5 + kpis[i].length) };
      };
      const dimAt = (j: number) => {
        const r = Math.round(top + ((j + 0.5) / dims.length) * span);
        return { c: cols - 3, r, out: Math.max(cols * 0.6, cols - 6 - dims[j].length) };
      };
      const n = Math.floor(time / PATH);
      const t = (time - n * PATH) / PATH;
      // Walk the KPI x dimension pairs in a scrambled but fixed order, so successive paths jump around the graph.
      const lit = edges[(n * stride + 3) % edges.length];
      const grow = g.reduced ? 1 : Math.min(1, t * 2.5);
      const fade = g.reduced ? 1 : Math.min(1, (1 - t) * 6);

      // Labels first, so edges never run through them; a node's label is drawn only if it fits whole.
      kpis.forEach((name, i) => {
        const p = kpiAt(i), on = lit.i === i;
        if (g.clear(p.r, p.c + 2, name.length)) g.write(p.r, p.c + 2, name, on ? 2 : 0, on ? 0.95 * fade : 0.5);
        g.put(p.c, p.r, "o", on ? 2 : 0, on ? 0.95 : 0.6);
      });
      dims.forEach((name, j) => {
        const p = dimAt(j), on = lit.j === j;
        const c0 = p.c - 2 - name.length;
        if (g.clear(p.r, c0, name.length)) g.write(p.r, c0, name, on ? 2 : 0, on ? 0.95 * fade : 0.5);
        g.put(p.c, p.r, "@", on ? 2 : 0, on ? 0.95 : 0.6);
      });

      // The caption: the question the lit path answers, under the graph's middle.
      const a = kpiAt(lit.i), b = dimAt(lit.j);
      const caption = ` ${kpis[lit.i]} by ${dims[lit.j].toLowerCase()} `;
      if (grow >= 1) {
        const c0 = Math.round((a.out + b.out) / 2 - caption.length / 2);
        const r = g.clearRow(Math.round((a.r + b.r) / 2) - 2, c0, caption.length);
        if (r >= 0) g.write(r, c0, caption, 2, 0.9 * fade);
      }

      // The lit path, growing from the KPI to the dimension.
      stroke(a.out, a.r, b.out, b.r, (c, r, glyph, at) => {
        if (at <= grow) g.put(c, r, glyph, 2, 0.85 * fade);
      });

      // Packets on a handful of edges at a time.
      if (!g.reduced) {
        for (let k = 0; k < Math.min(edges.length, 14); k++) {
          const e = edges[Math.floor(hash(k + Math.floor(time / 6) * 31) * edges.length)];
          const p = kpiAt(e.i), q = dimAt(e.j);
          const at = (time * (0.12 + 0.1 * hash(k)) + hash(k + 9)) % 1;
          g.put(p.out + (q.out - p.out) * at, p.r + (q.r - p.r) * at, "*", 0, 0.75);
        }
      }

      // Every edge, faint.
      for (const e of edges) {
        const p = kpiAt(e.i), q = dimAt(e.j);
        stroke(p.out, p.r, q.out, q.r, (c, r, glyph, f) => {
          // Thinned along the middle so the many crossings read as a weave, not a block.
          if (f < 0.12 || f > 0.88 || hash(c * 3 + r * 101) < 0.55) g.put(c, r, glyph, 1, 0.16);
        });
      }
    },
  };
}

export function GraphField({ catalog }: { catalog: CatalogResponse }) {
  const scene = useMemo(() => graph(catalog), [catalog]);
  return <FieldCanvas scene={scene} testId="graph-field" />;
}

import { useMemo } from "react";
import type { CaseFull } from "../../api";
import { pct } from "../../components";
import { STATUS, hypothesisName, kpiLabel, segmentName, stepQuestion } from "../../labels";
import { FieldCanvas } from "./FieldCanvas";
import { stroke, type Grid, type Scene } from "./grid";

// A case's backdrop: the investigation as a tree that grows left to right. The root is what opened the case (the
// alarm, with its real size when the trigger receipt has one); its branches are the drivers the plan tested, the data
// check first, as the planner's validator requires; each driver's twigs are the checks that tested it, in the plan's
// words. Once grown, the best-supported driver and its checks light up in accent and the rest dim. Every name, status
// and percentage comes from the case's own plan, conclusion and receipts.

const BRANCH = 0.7;
const SETTLE = 0.8;
const HOLD = 6;
const FADE = 1;
const D0 = "D0_DATA_PROBLEM";

function tree(c: CaseFull): Scene {
  const ranked = c.conclusion?.ranked ?? [];
  const ids = [...new Set([...ranked.map((v) => v.id), ...(c.plan?.hypotheses ?? [])])];
  // The data check is tested first, so it grows first.
  ids.sort((a, b) => Number(b === D0) - Number(a === D0));
  const top = c.conclusion?.top_hypothesis ?? null;
  const hyps = ids.map((id) => {
    const v = ranked.find((x) => x.id === id);
    const words = v ? ` · ${STATUS[v.status]?.label ?? v.status}${v.status !== "refuted" ? ` · ${pct(v.confidence)} likely` : ""}` : "";
    return { id, label: `${hypothesisName(id)}${words}` };
  });
  const steps = (c.plan?.steps ?? []).map((s) => ({
    ...s,
    label: `${stepQuestion(s.tool, s.inputs)}${c.step_errors?.[s.step_id] ? " · couldn't run" : ""}`,
  }));
  const trigger = c.receipts.find((r) => r.tool === "kpi.alarm" && r.output.status === "alarm");
  const ratio = trigger?.output.ratio == null ? null : Number(trigger.output.ratio);
  const root = `${trigger ? "alarm" : "case"}: ${kpiLabel(c.metric)} · ${segmentName(c.entity)}${ratio !== null && ratio >= 1 ? ` · ${ratio.toFixed(1)}× usual` : ""}`;
  const grown = hyps.length * BRANCH + SETTLE;
  const cycle = grown + HOLD + FADE;

  return {
    stillTime: grown + 1,
    draw(g: Grid) {
      const { cols, rows, time } = g;
      if (!hyps.length) return;
      const t = g.reduced ? grown + 1 : time % cycle;
      const fade = Math.min(1, (cycle - t) / FADE);
      const judged = t >= grown;
      const span = rows - 8;
      const rootAt = { c: 3, r: Math.round(rows / 2) };
      const hx = Math.round(cols * 0.3), sx = Math.round(cols * 0.62);
      const hypAt = (k: number) => Math.round(4 + ((k + 0.5) / hyps.length) * span);
      const stepAt = (k: number) => Math.round(4 + ((k + 0.5) / Math.max(1, steps.length)) * span);
      /** How far branch k has grown: its own slot of the timeline, the data check's first. */
      const grow = (k: number) => Math.max(0, Math.min(1, (t - k * BRANCH) / (BRANCH * 0.7)));
      const look = (on: boolean) => (!judged ? { colour: 0 as const, a: 0.6 } : on ? { colour: 2 as const, a: 0.95 } : { colour: 1 as const, a: 0.3 });
      const fit = (text: string, room: number) => (text.length > room ? `${text.slice(0, Math.max(0, room - 1))}…` : text);

      // Labels first, so the branches never run through them. Each label is drawn whole or not at all.
      const label = (r: number, c0: number, text: string, colour: 0 | 1 | 2, a: number) => {
        if (text && g.clear(r, c0, text.length)) g.write(r, c0, text, colour, a * fade);
      };
      label(rootAt.r - 2, 2, fit(root, hx - 4), judged ? 2 : 0, 0.9);
      hyps.forEach((h, k) => {
        if (grow(k) < 1) return;
        const s = look(h.id === top);
        label(hypAt(k), hx + 2, fit(h.label, sx - hx - 6), s.colour, s.a);
      });
      steps.forEach((s, k) => {
        const owners = s.hypothesis_ids.map((id) => hyps.findIndex((h) => h.id === id)).filter((i) => i >= 0);
        if (!owners.some((i) => grow(i) >= 1)) return;
        const st = look(top !== null && s.hypothesis_ids.includes(top));
        label(stepAt(k), sx + 2, fit(s.label, cols - sx - 4), st.colour, st.a * 0.9);
      });

      // Nodes.
      g.put(rootAt.c, rootAt.r, "◉", judged ? 2 : 0, 0.95 * fade);
      hyps.forEach((h, k) => {
        if (grow(k) >= 1) g.put(hx, hypAt(k), h.id === top && judged ? "●" : "o", look(h.id === top).colour, look(h.id === top).a * fade);
      });
      steps.forEach((s, k) => {
        if (s.hypothesis_ids.some((id) => grow(hyps.findIndex((h) => h.id === id)) >= 1)) g.put(sx, stepAt(k), "·", 1, 0.7 * fade);
      });

      // Branches: root to each driver, then each driver to the checks that tested it.
      hyps.forEach((h, k) => {
        const p = grow(k);
        if (p <= 0) return;
        const s = look(h.id === top);
        stroke(rootAt.c + 2, rootAt.r, hx - 1, hypAt(k), (c, r, glyph, at) => {
          if (at <= p) g.put(c, r, glyph, s.colour, s.a * 0.8 * fade);
        });
        if (p < 1) return;
        const from = Math.min(sx - 3, hx + 3 + h.label.length);
        steps.forEach((st, j) => {
          if (!st.hypothesis_ids.includes(h.id)) return;
          const q = Math.max(0, Math.min(1, (t - k * BRANCH - BRANCH * 0.7) / (BRANCH * 0.6)));
          stroke(from, hypAt(k), sx - 1, stepAt(j), (c, r, glyph, at) => {
            if (at <= q || judged) g.put(c, r, glyph, s.colour, s.a * 0.6 * fade);
          });
        });
      });
    },
  };
}

export function TreeField({ c }: { c: CaseFull }) {
  const scene = useMemo(() => tree(c), [c]);
  return <FieldCanvas scene={scene} testId="tree-field" />;
}

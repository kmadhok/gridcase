import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { startScene, type Scene } from "./grid";

/** A scene on a canvas fixed behind the whole page: above the paper and its grain, below everything else. Portalled
 * to <body> because an ancestor in the page makes its own containing block, which would trap a fixed canvas inside
 * the column. The page carries the same information accessibly, so the canvas is aria-hidden. */
export function FieldCanvas({ scene, testId }: { scene: Scene | null; testId: string }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas || !scene) return;
    const field = startScene(canvas, scene);
    return () => field?.stop();
  }, [scene]);
  return createPortal(
    <canvas ref={ref} aria-hidden="true" data-testid={testId} className="pointer-events-none fixed inset-0 h-full w-full" style={{ zIndex: -1 }} />,
    document.body,
  );
}

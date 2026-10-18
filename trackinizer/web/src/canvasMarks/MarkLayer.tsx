import { type KeyboardEvent, type PointerEvent, useEffect, useRef, useState } from "react";
import { coversOf } from "./markCovers";
import { boundsOf, type MarkPoint, toFractions } from "./markGeometry";
import type { Boxes } from "./useBoxes";
import "./marks.css";

/** The least a drag must cover, as a fraction of the stage, to be a box and not a stray click. */
const LEAST_DRAG = 0.005;

/**
 * The layer over the whole stage that shows the red boxes of the open page. Only a
 * box's outline takes a click, which removes it; the inside, and everything else,
 * passes clicks through to the page. While the pen is on, a drag draws one box. Each
 * box records the visuals and records under it when it is drawn, which the
 * station's agent reads.
 */
export function MarkLayer({ boxes }: { readonly boxes: Boxes }) {
  const layer = useRef<HTMLDivElement>(null);
  const [draft, setDraft] = useState<readonly MarkPoint[]>([]);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const pressed = useRef<number | null>(null);

  useEffect(() => {
    const element = layer.current;
    if (!element) return;
    const measure = () => setSize({ width: element.clientWidth, height: element.clientHeight });
    measure();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    observer?.observe(element);
    return () => observer?.disconnect();
  }, []);

  const at = (event: PointerEvent) => toFractions(event.clientX, event.clientY, layer.current!.getBoundingClientRect());

  const press = (event: PointerEvent) => {
    if (event.button !== 0) return;
    event.currentTarget.setPointerCapture?.(event.pointerId);
    pressed.current = event.pointerId;
    setDraft([at(event)]);
  };

  const move = (event: PointerEvent) => {
    if (pressed.current === event.pointerId) setDraft((now) => [now[0]!, at(event)]);
  };

  const release = (event: PointerEvent) => {
    if (pressed.current !== event.pointerId) return;
    pressed.current = null;
    const [from, to] = [draft[0], draft.at(-1)];
    setDraft([]);
    const stage = layer.current?.parentElement;
    if (!from || !to || !stage || !boxes.page || Math.hypot(to.x - from.x, to.y - from.y) < LEAST_DRAG) return;
    const corners = [from, to];
    boxes.draw({ points: corners, page: boxes.page, covers: coversOf(stage, boundsOf(corners)) });
  };

  const removeByKey = (event: KeyboardEvent, id: string) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    boxes.remove(id);
  };

  return (
    <div ref={layer} className="mark-layer">
      <svg className="mark-svg" width={size.width} height={size.height}>
        {boxes.shown.map((box) => {
          const rect = rectOf(box.points, size);
          return (
            <g key={box.id} className="mark-box" data-mark={box.id}>
              <rect {...rect} className="mark-outline" />
              <rect {...rect} className="mark-hit" role="button" tabIndex={0} aria-label="Remove box"
                onClick={() => boxes.remove(box.id)} onKeyDown={(event) => removeByKey(event, box.id)}>
                <title>Click to remove this box</title>
              </rect>
            </g>
          );
        })}
        {draft.length === 2 && <rect {...rectOf(draft, size)} className="mark-outline" />}
      </svg>
      {boxes.drawing && <div className="mark-capture" onPointerDown={press} onPointerMove={move}
        onPointerUp={release} onPointerCancel={release} />}
      {boxes.error && <p className="mark-error" role="alert">{boxes.error}</p>}
    </div>
  );
}

/** A box's two corners (fractions of the stage) as the rectangle `size` draws. */
function rectOf(points: readonly MarkPoint[], size: { readonly width: number; readonly height: number }) {
  const [a, b] = [points[0]!, points.at(-1)!];
  return {
    x: Math.min(a.x, b.x) * size.width, y: Math.min(a.y, b.y) * size.height,
    width: Math.abs(b.x - a.x) * size.width, height: Math.abs(b.y - a.y) * size.height,
  };
}

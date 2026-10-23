import { type PointerEvent, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { coversOf } from "./markCovers";
import { MarkBox } from "./MarkBox";
import { boundsOf, type MarkPoint, pinTo, toFractions } from "./markGeometry";
import type { TileBodies } from "./tileBodies";
import { tilesInPaintOrder } from "./tiles";
import type { Boxes } from "./useBoxes";
import "./marks.css";

/** The least a drag must cover, as a fraction of the stage, to be a box and not a stray click. */
const LEAST_DRAG = 0.005;

/**
 * The layer over the whole stage that shows the red boxes of the open page. A box
 * drawn over a tile is pinned to it and stands inside the tile's body, which
 * `bodies` holds, so it goes where the tile goes; one drawn over no tile stands in
 * this layer. Only a box's outline takes a click, which removes it; the inside, and
 * everything else, passes clicks through to the page. While the pen is on, a drag
 * draws one box. Each box records the visuals and records under it when it is
 * drawn, which the canvas's agent reads.
 */
export function MarkLayer({ boxes, bodies }: { readonly boxes: Boxes; readonly bodies: TileBodies }) {
  const layer = useRef<HTMLDivElement>(null);
  const [draft, setDraft] = useState<readonly MarkPoint[]>([]);
  const pressed = useRef<number | null>(null);

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
    const { tile, points } = pinTo(corners, stage.getBoundingClientRect(), tilesInPaintOrder(stage));
    boxes.draw({ points: [...points], ...(tile ? { tile } : {}), page: boxes.page, covers: coversOf(stage, boundsOf(corners)) });
  };

  const box = (each: (typeof boxes.shown)[number]) => (
    <MarkBox key={each.id} id={each.id} author={each.author === "agent" ? "agent" : "person"} bounds={boundsOf(each.points)}
      onRemove={boxes.drawing ? undefined : () => boxes.remove(each.id)} />
  );

  return (
    <div ref={layer} className="mark-layer">
      <div className="mark-boxes">
        {boxes.shown.filter((each) => !each.tile).map(box)}
        {draft.length === 2 && <MarkBox bounds={boundsOf(draft)} />}
      </div>
      {boxes.shown.filter((each) => each.tile && bodies.has(each.tile)).map((each) => createPortal(box(each), bodies.get(each.tile!)!, each.id))}
      {boxes.drawing && <div className="mark-capture" onPointerDown={press} onPointerMove={move}
        onPointerUp={release} onPointerCancel={release} />}
      {boxes.error && <p className="mark-error" role="alert">{boxes.error}</p>}
    </div>
  );
}

import type { GraphCamera } from "./camera";
import { LABEL_GAP_PX, LABEL_PX } from "./constants";
import { type DrawNode, isPlaced } from "./model";

const CELL_W = 120;
const CELL_H = 24;
const CELL_STRIDE = 1024;
const MAX_DRAWN_LABELS = 300;

type Box = { left: number; right: number; top: number; bottom: number };

/**
 * Draws outlined Canvas 2D labels on an overlay canvas above any WebGL/Canvas
 * graph layer. Caches `measureText` widths and uses a screen-space grid for
 * O(1) overlap rejection.
 */
export class LabelOverlay {
  readonly #ctx: CanvasRenderingContext2D | null;
  readonly #widths = new Map<string, number>();
  readonly #font: string;

  constructor(canvas: HTMLCanvasElement, fontFamily: string) {
    this.#ctx = canvas.getContext("2d");
    this.#font = `600 ${LABEL_PX}px ${fontFamily}`;
  }

  draw(labelled: readonly DrawNode[], camera: GraphCamera, dpr: number): void {
    const ctx = this.#ctx;
    if (!ctx) return;
    const w = camera.width;
    const h = camera.height;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    if (labelled.length === 0) return;

    ctx.font = this.#font;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.lineJoin = "round";
    ctx.lineWidth = 3;

    const cells = new Map<number, Box[]>();
    let drawn = 0;

    for (const node of labelled) {
      if (drawn >= MAX_DRAWN_LABELS) break;
      const label = node.look.label;
      if (!label || node.hidden || !isPlaced(node)) continue;

      const sx = (node.x - camera.x) * camera.zoom + w / 2;
      const sy =
        (node.y + node.look.radius - camera.y) * camera.zoom + h / 2 + LABEL_GAP_PX;
      if (sy > h || sy + LABEL_PX < 0) continue;

      let width = this.#widths.get(label.text);
      if (width === undefined) {
        width = ctx.measureText(label.text).width;
        this.#widths.set(label.text, width);
      }
      const half = width / 2;
      const left = sx - half;
      const right = sx + half;
      if (right < 0 || left > w) continue;

      const box: Box = { left, right, top: sy, bottom: sy + LABEL_PX };
      if (overlapsGrid(cells, box)) continue;
      insertGrid(cells, box);

      ctx.strokeStyle = label.outline;
      ctx.strokeText(label.text, sx, sy);
      ctx.fillStyle = label.color;
      ctx.fillText(label.text, sx, sy);
      drawn += 1;
    }
  }
}

function overlapsGrid(cells: Map<number, Box[]>, box: Box): boolean {
  const c0 = Math.floor(box.left / CELL_W);
  const c1 = Math.floor(box.right / CELL_W);
  const r0 = Math.floor(box.top / CELL_H);
  const r1 = Math.floor(box.bottom / CELL_H);
  for (let c = c0; c <= c1; c++) {
    for (let r = r0; r <= r1; r++) {
      const list = cells.get(c * CELL_STRIDE + r);
      if (!list) continue;
      for (const other of list) {
        if (
          box.left < other.right &&
          other.left < box.right &&
          box.top < other.bottom &&
          other.top < box.bottom
        ) {
          return true;
        }
      }
    }
  }
  return false;
}

function insertGrid(cells: Map<number, Box[]>, box: Box): void {
  const c0 = Math.floor(box.left / CELL_W);
  const c1 = Math.floor(box.right / CELL_W);
  const r0 = Math.floor(box.top / CELL_H);
  const r1 = Math.floor(box.bottom / CELL_H);
  for (let c = c0; c <= c1; c++) {
    for (let r = r0; r <= r1; r++) {
      const key = c * CELL_STRIDE + r;
      const list = cells.get(key);
      if (list) list.push(box);
      else cells.set(key, [box]);
    }
  }
}

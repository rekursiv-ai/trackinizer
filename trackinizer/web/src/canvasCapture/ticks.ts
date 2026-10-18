/** How far into the picture a tick reaches, in pixels at the size the picture is drawn at 600 px. */
export const TICK = 6;

const HALO = "rgba(255,255,255,0.8)";
const INK = "rgba(0,0,0,0.65)";

/**
 * Draw a thin tick every tenth of the top and left edges, labelled 0.1 to 0.9, so
 * a reader of the picture can turn a place into the fractions a box is drawn with.
 * They stay at the edges and keep a light halo, so neither hides content.
 */
export function drawTicks(context: CanvasRenderingContext2D, size: { readonly width: number; readonly height: number }): void {
  const unit = Math.max(1, Math.min(size.width, size.height) / 600);
  const reach = TICK * unit;
  context.save();
  context.font = `${Math.round(9 * unit)}px sans-serif`;
  context.textBaseline = "top";
  for (let tenth = 1; tenth <= 9; tenth++) {
    const label = (tenth / 10).toFixed(1);
    const x = (size.width * tenth) / 10;
    const y = (size.height * tenth) / 10;
    stroke(context, [x, 0, x, reach], unit);
    stroke(context, [0, y, reach, y], unit);
    write(context, label, x + 2 * unit, reach, unit);
    write(context, label, reach + 2 * unit, y - 4 * unit, unit);
  }
  context.restore();
}

function stroke(context: CanvasRenderingContext2D, [x1, y1, x2, y2]: readonly [number, number, number, number], unit: number): void {
  for (const [style, width] of [[HALO, 3 * unit], [INK, unit]] as const) {
    context.strokeStyle = style;
    context.lineWidth = width;
    context.beginPath();
    context.moveTo(x1, y1);
    context.lineTo(x2, y2);
    context.stroke();
  }
}

function write(context: CanvasRenderingContext2D, label: string, x: number, y: number, unit: number): void {
  context.lineWidth = 3 * unit;
  context.strokeStyle = HALO;
  context.strokeText(label, x, y);
  context.fillStyle = INK;
  context.fillText(label, x, y);
}

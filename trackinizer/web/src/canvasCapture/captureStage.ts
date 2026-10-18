import { newUuid } from "../api/idempotency";
import type { CaptureAnswer } from "../api/workspaces";
import { tilesInPaintOrder } from "../canvasMarks/tiles";
import { loadImage, rasterize } from "./raster";
import type { CaptureSource } from "./stageRegistry";
import { drawTicks } from "./ticks";

/** The longest side of the picture, in pixels; a larger stage is drawn smaller. */
export const LONG_SIDE = 1600;
/** The most the picture may weigh once encoded. */
export const MAX_BYTES = 1_500_000;
/** How long a frame has to draw itself before it stays blank. */
export const FRAME_WAIT_MS = 2000;

const JPEG_QUALITIES = [0.85, 0.6, 0.4] as const;
const PNG_PREFIX = "data:image/png;base64,";

/**
 * Draw the stage the way the person sees it, as an answer to an agent's request.
 *
 * A fraction of the picture is a fraction of the stage, which is what a red box is
 * drawn in. The sandboxed frames of HTML Artifacts cannot be read from here, so
 * each draws itself when asked and its picture goes at that frame's place; a frame
 * that stays silent stays blank and is named. The answer is an error, never a throw.
 */
export async function captureStage(source: CaptureSource, options: { readonly frameWaitMs?: number } = {}): Promise<CaptureAnswer> {
  const { stage } = source;
  const bounds = stage.getBoundingClientRect();
  if (bounds.width === 0 || bounds.height === 0) return { error: "The canvas has no size on screen." };
  const scale = Math.min(1, LONG_SIDE / Math.max(bounds.width, bounds.height));
  const frames = [...stage.querySelectorAll<HTMLIFrameElement>("iframe.artifact-html-frame")];
  // A box pinned to a tile stands inside it, so it is left out of the tile's picture as well.
  const marks = [...stage.querySelectorAll("[data-mark]")];
  try {
    // A fixed element resolves against the picture, not the screen, so it is drawn alone.
    const fixed = fixedIn(stage);
    const [surface, lifted, drawn] = await Promise.all([
      // The boxes are drawn last, from where they are on screen, so that nothing lifted covers them.
      rasterize(stage, scale, { leave: [...fixed, ...stage.querySelectorAll(".mark-layer"), ...marks] }),
      Promise.all(fixed.map((each) => rasterize(each, scale, { style: ANCHORED, leave: marks }))),
      Promise.all(frames.map((frame) => askFrame(frame, options.frameWaitMs ?? FRAME_WAIT_MS))),
    ]);
    const context = surface.getContext("2d");
    if (!context) return { error: "The canvas could not be drawn." };
    for (const [index, each] of fixed.entries()) lift(context, lifted[index]!, each, bounds, surface);
    const blank: string[] = [];
    for (const [index, frame] of frames.entries()) {
      const placed = await place(context, drawn[index], frame.getBoundingClientRect(), bounds, surface);
      if (!placed) blank.push(nameOf(frame));
    }
    const boxes = boxesOn(stage, bounds);
    outline(context, boxes, bounds, surface);
    drawTicks(context, surface);
    const encoded = encode(surface);
    if (!encoded) return { error: "The picture is too large to send." };
    return {
      image: {
        width: surface.width,
        height: surface.height,
        ...encoded,
        not_captured: [...new Set(blank)].slice(0, 20),
        boxes: boxes.map(({ id, author }) => ({ id, author })),
        tiles: tilesIn(stage, bounds),
      },
    };
  } catch (error) {
    return { error: `The canvas could not be drawn: ${error instanceof Error ? error.message : String(error)}`.slice(0, 200) };
  }
}

/** A red box that is on screen: its id and author, where it stands and the part of that which shows. */
type ShownBox = { readonly id: string; readonly author: "person" | "agent"; readonly rect: DOMRect; readonly seen: Seen };

/** The edges, in pixels, of what shows. */
type Seen = { readonly left: number; readonly top: number; readonly right: number; readonly bottom: number };

/**
 * The red boxes on `stage` where the screen shows them, from their own rects, so the
 * 24 px floor and a tile's moves are in them. A box with nothing of it in view, off
 * the stage, folded away or cut off by its tile, is not on screen.
 */
function boxesOn(stage: HTMLElement, bounds: DOMRect): ShownBox[] {
  return [...stage.querySelectorAll<SVGElement>("[data-mark]")].flatMap((element) => {
    const rect = element.getBoundingClientRect();
    const tile = element.closest<HTMLElement>("[data-visual-instance]");
    const [top, right, bottom, left] = tile ? insetOf(tile.style.clipPath) : [0, 0, 0, 0];
    const cut = tile?.getBoundingClientRect() ?? bounds;
    const seen = {
      left: Math.max(rect.left, bounds.left, cut.left + left),
      top: Math.max(rect.top, bounds.top, cut.top + top),
      right: Math.min(rect.right, bounds.right, cut.right - right),
      bottom: Math.min(rect.bottom, bounds.bottom, cut.bottom - bottom),
    };
    if (rect.width <= 0 || rect.height <= 0 || seen.right <= seen.left || seen.bottom <= seen.top) return [];
    return [{ id: element.dataset.mark!, author: element.dataset.author === "agent" ? "agent" as const : "person" as const, rect, seen }];
  });
}

/** Every visual on `stage` with its body's edges as fractions of the picture, in paint order, the topmost last. */
function tilesIn(stage: HTMLElement, bounds: DOMRect) {
  const fraction = (value: number, origin: number, size: number) => Math.round(Math.min(1, Math.max(0, (value - origin) / size)) * 10_000) / 10_000;
  return tilesInPaintOrder(stage).map(({ id, rect }) => ({
    id,
    left: fraction(rect.left, bounds.left, bounds.width), top: fraction(rect.top, bounds.top, bounds.height),
    right: fraction(rect.left + rect.width, bounds.left, bounds.width), bottom: fraction(rect.top + rect.height, bounds.top, bounds.height),
  })).filter((tile) => tile.right > tile.left && tile.bottom > tile.top).slice(-MAX_TILES);
}

/** The most tiles an answer carries, which is the server's bound; the topmost are kept. */
const MAX_TILES = 50;

/** Draw each red box as its outline, in the red of the mark layer, where it stands on the picture and cut to what shows. */
function outline(
  context: CanvasRenderingContext2D,
  boxes: readonly ShownBox[],
  stage: DOMRect,
  surface: { readonly width: number; readonly height: number },
): void {
  const [across, down] = [surface.width / stage.width, surface.height / stage.height];
  context.save();
  context.strokeStyle = BOX_RED;
  context.lineWidth = Math.max(2, 2 * (surface.width / 800));
  for (const { rect, seen } of boxes) {
    context.save();
    context.beginPath();
    context.rect((seen.left - stage.left) * across, (seen.top - stage.top) * down, (seen.right - seen.left) * across, (seen.bottom - seen.top) * down);
    context.clip();
    context.strokeRect((rect.left - stage.left) * across, (rect.top - stage.top) * down, rect.width * across, rect.height * down);
    context.restore();
  }
  context.restore();
}

/** The red of `marks.css`. */
const BOX_RED = "#e5484d";

/** Where a fixed element's picture is drawn from: its own corner, with nothing cut away. */
const ANCHORED: Partial<CSSStyleDeclaration> = { left: "0", top: "0", right: "auto", bottom: "auto", clipPath: "none" };

/** The elements of the stage that are fixed to the screen, outermost only. */
function fixedIn(stage: HTMLElement): HTMLElement[] {
  const found: HTMLElement[] = [];
  for (const each of stage.querySelectorAll<HTMLElement>("*")) {
    if (getComputedStyle(each).position === "fixed" && !found.some((outer) => outer.contains(each))) found.push(each);
  }
  return found;
}

/** Draw a fixed element's own picture where the screen shows it, cut to the part that is visible. */
function lift(
  context: CanvasRenderingContext2D,
  picture: CanvasImageSource,
  element: HTMLElement,
  stage: DOMRect,
  surface: { readonly width: number; readonly height: number },
): void {
  const [across, down] = [surface.width / stage.width, surface.height / stage.height];
  const rect = element.getBoundingClientRect();
  const [top, right, bottom, left] = insetOf(element.style.clipPath);
  const seen = {
    left: Math.max(rect.left + left, stage.left),
    top: Math.max(rect.top + top, stage.top),
    right: Math.min(rect.right - right, stage.right),
    bottom: Math.min(rect.bottom - bottom, stage.bottom),
  };
  context.save();
  context.beginPath();
  context.rect((seen.left - stage.left) * across, (seen.top - stage.top) * down, Math.max(0, seen.right - seen.left) * across, Math.max(0, seen.bottom - seen.top) * down);
  context.clip();
  context.drawImage(picture, (rect.left - stage.left) * across, (rect.top - stage.top) * down, rect.width * across, rect.height * down);
  context.restore();
}

/** The four sides, in pixels, of an `inset(...)` clip path; none cut away if it is another kind. */
function insetOf(clipPath: string): [number, number, number, number] {
  const found = /^inset\(\s*([\d.]+)px\s+([\d.]+)px\s+([\d.]+)px\s+([\d.]+)px\s*\)$/.exec(clipPath.trim());
  return found ? [Number(found[1]), Number(found[2]), Number(found[3]), Number(found[4])] : [0, 0, 0, 0];
}

/** Ask a frame for a picture of itself; null when it does not answer in time. */
function askFrame(frame: HTMLIFrameElement, waitMs: number): Promise<string | null> {
  const target = frame.contentWindow;
  if (!target) return Promise.resolve(null);
  const id = newUuid();
  return new Promise((resolve) => {
    const finish = (png: string | null) => {
      clearTimeout(timer);
      window.removeEventListener("message", heard);
      resolve(png);
    };
    // Only this frame's own window, and only the answer to this request.
    const heard = (event: MessageEvent) => {
      const data: unknown = event.data;
      if (event.source !== target || typeof data !== "object" || data === null) return;
      const { type, id: asked, png } = data as { [name: string]: unknown };
      if (type !== "trax-capture-result" || asked !== id) return;
      finish(typeof png === "string" && png.startsWith(PNG_PREFIX) ? png : null);
    };
    const timer = setTimeout(() => finish(null), waitMs);
    window.addEventListener("message", heard);
    target.postMessage({ type: "trax-capture", id }, "*");
  });
}

/** Draw a frame's picture at the frame's place on the stage; false when there is none to draw. */
async function place(
  context: CanvasRenderingContext2D,
  png: string | null | undefined,
  frame: DOMRect,
  stage: DOMRect,
  surface: { readonly width: number; readonly height: number },
): Promise<boolean> {
  if (!png) return false;
  try {
    const image = await loadImage(png);
    const [across, down] = [surface.width / stage.width, surface.height / stage.height];
    context.drawImage(image, (frame.left - stage.left) * across, (frame.top - stage.top) * down, frame.width * across, frame.height * down);
    return true;
  } catch {
    return false;
  }
}

/** The visual a frame is in, or the Artifact it shows, for the line that names what stayed blank. */
function nameOf(frame: HTMLIFrameElement): string {
  return (frame.closest("[data-visual-type]")?.getAttribute("data-visual-type") ?? "an HTML Artifact").slice(0, 80);
}

/** PNG if it is small enough, else JPEG at falling quality; null if none is. */
function encode(surface: HTMLCanvasElement): { readonly mime: "image/png" | "image/jpeg"; readonly data: string } | null {
  const png = surface.toDataURL("image/png");
  if (weight(png) <= MAX_BYTES) return { mime: "image/png", data: png.slice(png.indexOf(",") + 1) };
  for (const quality of JPEG_QUALITIES) {
    const jpeg = surface.toDataURL("image/jpeg", quality);
    if (weight(jpeg) <= MAX_BYTES) return { mime: "image/jpeg", data: jpeg.slice(jpeg.indexOf(",") + 1) };
  }
  return null;
}

/** The bytes a data URL decodes to. */
function weight(url: string): number {
  return Math.floor(((url.length - url.indexOf(",") - 1) * 3) / 4);
}

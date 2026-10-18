import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { captureStage, LONG_SIDE, MAX_BYTES } from "./captureStage";
import { loadImage, rasterize } from "./raster";
import type { CaptureSource } from "./stageRegistry";
import { drawTicks } from "./ticks";

vi.mock("./raster", () => ({ rasterize: vi.fn(), loadImage: vi.fn() }));
vi.mock("./ticks", () => ({ drawTicks: vi.fn() }));

const PNG = "data:image/png;base64,AAAA";
const drawn: unknown[][] = [];
const clips: number[][] = [];
const strokes: { rect: number[]; color: unknown }[] = [];
/** Every image and box drawn, in order. */
const order: string[] = [];
const context = {
  strokeStyle: "" as unknown,
  lineWidth: 0,
  strokeRect(...rect: number[]) { strokes.push({ rect, color: this.strokeStyle }); order.push("box"); },
  drawImage: (...args: unknown[]) => void (drawn.push(args), order.push("image")),
  rect: (...args: number[]) => void clips.push(args),
  save: () => {}, restore: () => {}, beginPath: () => {}, clip: () => {},
};
const picture = { marker: "frame picture" } as unknown as HTMLImageElement;

/** A canvas that is `width` x `height` and encodes as the test says. */
function surface(width: number, height: number, encode: (mime: string, quality?: number) => string = () => "data:image/png;base64,iVBOR") {
  return { width, height, getContext: () => context, toDataURL: vi.fn(encode) } as unknown as HTMLCanvasElement & { toDataURL: ReturnType<typeof vi.fn> };
}

function rect(left: number, top: number, width: number, height: number): DOMRect {
  return { left, top, width, height, right: left + width, bottom: top + height, x: left, y: top, toJSON: () => ({}) };
}

/** A stage of `size` at (100, 50), holding one visual with one frame at `frameRect`, if given. */
function stage(size: [number, number], frameRect?: DOMRect) {
  const element = document.createElement("div");
  element.getBoundingClientRect = () => rect(100, 50, ...size);
  const tile = document.createElement("div");
  tile.dataset.visualType = "trax.artifact";
  element.append(tile);
  document.body.append(element);
  let frame: HTMLIFrameElement | undefined;
  if (frameRect) {
    frame = document.createElement("iframe");
    frame.className = "artifact-html-frame";
    frame.getBoundingClientRect = () => frameRect;
    tile.append(frame);
  }
  return { element, frame };
}

/** Make `frame` answer its next request with `png`, the way the page inside it does. */
function answering(frame: HTMLIFrameElement, reply: (id: string) => { source: Window | null; data: unknown } | null) {
  const target = frame.contentWindow!;
  vi.spyOn(target, "postMessage").mockImplementation((message: unknown) => {
    const answer = reply((message as { id: string }).id);
    if (answer) setTimeout(() => window.dispatchEvent(new MessageEvent("message", { data: answer.data, source: answer.source })));
  });
}

const sourceOf = (element: HTMLElement, boxes: CaptureSource["boxes"] = () => []): CaptureSource => ({ stage: element, boxes });

beforeEach(() => {
  drawn.length = 0;
  clips.length = 0;
  strokes.length = 0;
  order.length = 0;
  vi.mocked(loadImage).mockResolvedValue(picture);
});
afterEach(() => {
  vi.clearAllMocks();
  document.body.replaceChildren();
});

test("a frame's picture is drawn at the frame's place on the stage, in the picture's own scale", async () => {
  const { element, frame } = stage([3200, 1600], rect(900, 450, 800, 400));
  vi.mocked(rasterize).mockResolvedValue(surface(1600, 800));
  answering(frame!, (id) => ({ source: frame!.contentWindow, data: { type: "trax-capture-result", id, png: PNG } }));

  const answer = await captureStage(sourceOf(element));

  expect(rasterize).toHaveBeenCalledWith(element, LONG_SIDE / 3200, { leave: [] });
  // (900 - 100, 450 - 50) on a 3200 x 1600 stage, halved into a 1600 x 800 picture.
  expect(drawn).toEqual([[picture, 400, 200, 400, 200]]);
  expect(loadImage).toHaveBeenCalledWith(PNG);
  expect(answer).toMatchObject({ image: { width: 1600, height: 800, mime: "image/png", not_captured: [] } });
});

test("a stage within the long side is drawn at full size", async () => {
  const { element } = stage([800, 400]);
  vi.mocked(rasterize).mockResolvedValue(surface(800, 400));

  await captureStage(sourceOf(element));

  expect(rasterize).toHaveBeenCalledWith(element, 1, { leave: [] });
});

test("a frame that does not answer stays blank and is named by its visual", async () => {
  const { element, frame } = stage([800, 400], rect(100, 50, 400, 200));
  vi.mocked(rasterize).mockResolvedValue(surface(800, 400));
  answering(frame!, () => null);

  const answer = await captureStage(sourceOf(element), { frameWaitMs: 5 });

  expect(drawn).toEqual([]);
  expect(answer).toMatchObject({ image: { not_captured: ["trax.artifact"] } });
});

test("only the frame's own window, and only the answer to this request, is taken", async () => {
  const { element, frame } = stage([800, 400], rect(100, 50, 400, 200));
  vi.mocked(rasterize).mockResolvedValue(surface(800, 400));
  answering(frame!, (id) => {
    // Three wrong answers, one after another: another window, another id, a bad picture.
    setTimeout(() => window.dispatchEvent(new MessageEvent("message", { data: { type: "trax-capture-result", id, png: PNG }, source: window })));
    setTimeout(() => window.dispatchEvent(new MessageEvent("message", { data: { type: "trax-capture-result", id: "other", png: PNG }, source: frame!.contentWindow })));
    return { source: frame!.contentWindow, data: { type: "trax-capture-result", id, png: "javascript:alert(1)" } };
  });

  const answer = await captureStage(sourceOf(element), { frameWaitMs: 20 });

  expect(drawn).toEqual([]);
  expect(answer).toMatchObject({ image: { not_captured: ["trax.artifact"] } });
});

test("the red boxes on the stage are listed with who drew them", async () => {
  const { element } = stage([800, 400]);
  vi.mocked(rasterize).mockResolvedValue(surface(800, 400));
  const points = [{ x: 0.1, y: 0.2 }, { x: 0.4, y: 0.6 }];
  const boxes = [{ id: "b1", author: "person", points }, { id: "b2", author: "agent", points }] as const;

  const answer = await captureStage(sourceOf(element, () => boxes));

  // Only the id and the author go to the server, which refuses other keys.
  expect(answer).toMatchObject({ image: { boxes: [{ id: "b1", author: "person" }, { id: "b2", author: "agent" }] } });
  expect(JSON.stringify(answer)).not.toContain("points");
});

test("the mark layer is left out of the stage's picture, because its boxes are drawn from their data", async () => {
  const { element } = stage([800, 400]);
  const layer = document.createElement("div");
  layer.className = "mark-layer";
  element.append(layer);
  vi.mocked(rasterize).mockResolvedValue(surface(800, 400));

  await captureStage(sourceOf(element));

  expect(vi.mocked(rasterize).mock.calls[0]![2]).toEqual({ leave: [layer] });
});

test("each box is outlined in the mark layer's red at its fractions of the picture", async () => {
  const { element } = stage([3200, 1600]);
  vi.mocked(rasterize).mockResolvedValue(surface(1600, 800));
  const box = { id: "b1", author: "person", points: [{ x: 0.5, y: 0.75 }, { x: 0.25, y: 0.25 }] } as const;

  await captureStage(sourceOf(element, () => [box]));

  // Either corner first: left 0.25, top 0.25, 0.25 wide, 0.5 tall, of a 1600 x 800 picture.
  expect(strokes).toEqual([{ rect: [400, 200, 400, 400], color: "#e5484d" }]);
});

test("boxes are drawn after the fixed tiles and the frames, so nothing covers them", async () => {
  const { element } = stage([800, 400]);
  const tile = fixedTile(element, rect(300, 150, 400, 200));
  const frame = document.createElement("iframe");
  frame.className = "artifact-html-frame";
  frame.getBoundingClientRect = () => rect(320, 170, 300, 100);
  tile.append(frame);
  pictures(surface(800, 400), { marker: "tile" });
  answering(frame, (id) => ({ source: frame.contentWindow, data: { type: "trax-capture-result", id, png: PNG } }));
  const box = { id: "b1", author: "person", points: [{ x: 0.1, y: 0.1 }, { x: 0.9, y: 0.9 }] } as const;

  await captureStage(sourceOf(element, () => [box]));

  expect(order).toEqual(["image", "image", "box"]);
});

test("the ticks are drawn on the finished picture", async () => {
  const { element } = stage([800, 400]);
  const canvas = surface(800, 400);
  vi.mocked(rasterize).mockResolvedValue(canvas);

  await captureStage(sourceOf(element));

  expect(drawTicks).toHaveBeenCalledWith(context, canvas);
});

test("a picture over the weight cap falls to JPEG, and to a lower quality until it fits", async () => {
  const { element } = stage([800, 400]);
  const big = `data:image/png;base64,${"A".repeat(Math.ceil((MAX_BYTES * 4) / 3) + 8)}`;
  const canvas = surface(800, 400, (mime, quality) => (mime === "image/png" || quality === 0.85 ? big : "data:image/jpeg;base64,/9j/"));
  vi.mocked(rasterize).mockResolvedValue(canvas);

  const answer = await captureStage(sourceOf(element));

  expect(canvas.toDataURL.mock.calls).toEqual([["image/png"], ["image/jpeg", 0.85], ["image/jpeg", 0.6]]);
  expect(answer).toMatchObject({ image: { mime: "image/jpeg", data: "/9j/" } });
});

test("a picture that fits at no quality is an error, not a throw", async () => {
  const { element } = stage([800, 400]);
  const big = `data:image/png;base64,${"A".repeat(Math.ceil((MAX_BYTES * 4) / 3) + 8)}`;
  vi.mocked(rasterize).mockResolvedValue(surface(800, 400, () => big));

  expect(await captureStage(sourceOf(element))).toEqual({ error: "The picture is too large to send." });
});

test("a stage with no size, and a rasterizer that fails, each answer with the reason", async () => {
  const { element } = stage([0, 0]);
  expect(await captureStage(sourceOf(element))).toEqual({ error: "The canvas has no size on screen." });

  const { element: sized } = stage([800, 400]);
  vi.mocked(rasterize).mockRejectedValue(new Error("tainted"));
  expect(await captureStage(sourceOf(sized))).toEqual({ error: "The canvas could not be drawn: tainted" });
});

/** A fixed element in `element`, as the Browse tile is: placed by the screen, cut by `clipPath`. */
function fixedTile(element: HTMLElement, place: DOMRect, clipPath = "none") {
  const tile = document.createElement("div");
  tile.style.position = "fixed";
  tile.style.clipPath = clipPath;
  tile.getBoundingClientRect = () => place;
  element.append(tile);
  return tile;
}

/** The pictures `rasterize` makes: the stage's, and each fixed element's own. */
function pictures(stagePicture: HTMLCanvasElement, tilePicture: unknown) {
  vi.mocked(rasterize).mockImplementation(async (node) => (node.style.position === "fixed" ? (tilePicture as HTMLCanvasElement) : stagePicture));
}

const ANCHORED = { left: "0", top: "0", right: "auto", bottom: "auto", clipPath: "none" };

test("a fixed element is left out of the stage's picture, because it would resolve against the picture", async () => {
  const { element } = stage([800, 400]);
  const tile = fixedTile(element, rect(300, 150, 200, 100));
  pictures(surface(800, 400), { marker: "tile" });

  await captureStage(sourceOf(element));

  expect(vi.mocked(rasterize).mock.calls[0]).toEqual([element, 1, { leave: [tile] }]);
});

test("a fixed element is drawn alone, from its own corner with nothing cut away", async () => {
  const { element } = stage([800, 400]);
  const tile = fixedTile(element, rect(300, 150, 200, 100), "inset(10px 20px 30px 40px)");
  pictures(surface(800, 400), { marker: "tile" });

  await captureStage(sourceOf(element));

  expect(vi.mocked(rasterize)).toHaveBeenCalledWith(tile, 1, { style: ANCHORED });
});

test("a fixed element's picture goes where the screen shows it, relative to the stage and in the picture's scale", async () => {
  const { element } = stage([3200, 1600]);
  fixedTile(element, rect(900, 450, 800, 400));
  const tilePicture = { marker: "tile" };
  pictures(surface(1600, 800), tilePicture);

  await captureStage(sourceOf(element));

  // (900 - 100, 450 - 50) on the stage, halved into the picture.
  expect(drawn).toEqual([[tilePicture, 400, 200, 400, 200]]);
});

test("the inset clip of a fixed element is applied to what is drawn", async () => {
  const { element } = stage([800, 400]);
  fixedTile(element, rect(300, 150, 200, 100), "inset(10px 20px 30px 40px)");
  pictures(surface(800, 400), { marker: "tile" });

  await captureStage(sourceOf(element));

  // Left 300 + 40, top 150 + 10, less 20 on the right and 30 below, from the stage's (100, 50).
  expect(clips).toEqual([[240, 110, 140, 60]]);
});

test("a fixed element cut by the stage's own edge is clipped to the stage", async () => {
  const { element } = stage([800, 400]);
  fixedTile(element, rect(50, 20, 200, 100));
  pictures(surface(800, 400), { marker: "tile" });

  await captureStage(sourceOf(element));

  // Only the part of (50..250, 20..120) inside the stage (100..900, 50..450) shows.
  expect(clips).toEqual([[0, 0, 150, 70]]);
});

test("a frame inside a fixed element still lands at its own rect, after the element", async () => {
  const { element } = stage([800, 400]);
  const tile = fixedTile(element, rect(300, 150, 400, 200));
  const frame = document.createElement("iframe");
  frame.className = "artifact-html-frame";
  frame.getBoundingClientRect = () => rect(320, 170, 300, 100);
  tile.append(frame);
  const tilePicture = { marker: "tile" };
  pictures(surface(800, 400), tilePicture);
  answering(frame, (id) => ({ source: frame.contentWindow, data: { type: "trax-capture-result", id, png: PNG } }));

  await captureStage(sourceOf(element));

  expect(drawn).toEqual([[tilePicture, 200, 100, 400, 200], [picture, 220, 120, 300, 100]]);
});

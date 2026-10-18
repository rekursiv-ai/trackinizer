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

const sourceOf = (element: HTMLElement): CaptureSource => ({ stage: element });

/** A red box in `parent`, as the page draws one: an element with an id and author that stands at `place`. */
function redBox(parent: HTMLElement, place: DOMRect, id = "b1", author = "person") {
  const box = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  box.dataset.mark = id;
  box.dataset.author = author;
  box.getBoundingClientRect = () => place;
  parent.append(box);
  return box;
}

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
  redBox(element, rect(180, 130, 300, 160), "b1");
  redBox(element, rect(180, 130, 300, 160), "b2", "agent");

  const answer = await captureStage(sourceOf(element));

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

test("each box is outlined in the mark layer's red where its element stands on the picture", async () => {
  const { element } = stage([3200, 1600]);
  vi.mocked(rasterize).mockResolvedValue(surface(1600, 800));
  redBox(element, rect(900, 450, 800, 800));

  await captureStage(sourceOf(element));

  // Where the element stands on the 3200 x 1600 stage at (100, 50): left 0.25, top 0.25, 0.25 wide, 0.5 tall, of a 1600 x 800 picture.
  expect(strokes).toEqual([{ rect: [400, 200, 400, 400], color: "#e5484d" }]);
});

/** A visual of the stage whose body stands at `place`; a floating one lies over the docked ones at `lift`. */
function visualAt(element: HTMLElement, id: string, place: DOMRect, options: { readonly lift?: number; readonly clipPath?: string } = {}) {
  const tile = document.createElement("div");
  tile.dataset.visualInstance = id;
  tile.className = options.lift === undefined ? "visual-tile" : "visual-tile visual-tile-floating";
  if (options.lift !== undefined) tile.style.zIndex = String(options.lift);
  if (options.clipPath) tile.style.clipPath = options.clipPath;
  tile.getBoundingClientRect = () => place;
  const body = document.createElement("div");
  body.className = "visual-tile-body";
  body.getBoundingClientRect = () => place;
  tile.append(body);
  element.append(tile);
  return body;
}

test("a box is listed and drawn only when some of it is on screen: not folded away, off the stage or cut off by its tile", async () => {
  const { element } = stage([800, 400]);
  vi.mocked(rasterize).mockResolvedValue(surface(800, 400));
  redBox(element, rect(150, 100, 100, 100), "shown");
  redBox(element, rect(150, 100, 0, 0), "folded", "agent");
  redBox(element, rect(1000, 100, 50, 50), "off");
  const tile = visualAt(element, "t1", rect(100, 50, 400, 200), { clipPath: "inset(0px 300px 0px 0px)" });
  redBox(tile, rect(300, 100, 50, 50), "cut");
  redBox(tile, rect(190, 100, 50, 50), "kept");

  const answer = await captureStage(sourceOf(element));

  expect(answer).toMatchObject({ image: { boxes: [{ id: "shown", author: "person" }, { id: "kept", author: "person" }] } });
  expect(strokes.map((each) => each.rect)).toEqual([[50, 50, 100, 100], [90, 50, 50, 50]]);
});

test("a box at the 24 px floor is drawn 24 px wide and tall, where it stands", async () => {
  const { element } = stage([800, 400]);
  vi.mocked(rasterize).mockResolvedValue(surface(800, 400));
  redBox(element, rect(288, 238, 24, 24));

  await captureStage(sourceOf(element));

  expect(strokes.map((each) => each.rect)).toEqual([[188, 188, 24, 24]]);
});

test("every box is left out of the pictures of the stage and of the fixed tiles, so it is not drawn twice", async () => {
  const { element } = stage([800, 400]);
  const fixed = fixedTile(element, rect(300, 150, 400, 200));
  const inTile = redBox(fixed, rect(320, 170, 50, 50), "in-tile");
  const loose = redBox(element, rect(150, 100, 50, 50), "loose");
  pictures(surface(800, 400), { marker: "tile" });

  await captureStage(sourceOf(element));

  const [stagePass, tilePass] = vi.mocked(rasterize).mock.calls.map((call) => call[2]!.leave);
  expect(stagePass).toEqual([fixed, inTile, loose]);
  expect(tilePass).toEqual([inTile, loose]);
});

test("the tiles are the visuals of the stage in paint order, as fractions of the picture, and a folded one is left out", async () => {
  const { element } = stage([800, 400]);
  vi.mocked(rasterize).mockResolvedValue(surface(800, 400));
  visualAt(element, "float-high", rect(500, 50, 200, 100), { lift: 12 });
  visualAt(element, "docked-b", rect(500, 50, 400, 400));
  visualAt(element, "float-low", rect(100, 250, 400, 200), { lift: 10 });
  visualAt(element, "docked-a", rect(100, 50, 400, 400));
  visualAt(element, "folded", rect(100, 50, 0, 0), { lift: 11 });

  const answer = await captureStage(sourceOf(element));

  expect("image" in answer ? answer.image.tiles : []).toEqual([
    { id: "docked-b", left: 0.5, top: 0, right: 1, bottom: 1 },
    { id: "docked-a", left: 0, top: 0, right: 0.5, bottom: 1 },
    { id: "float-low", left: 0, top: 0.5, right: 0.5, bottom: 1 },
    { id: "float-high", left: 0.5, top: 0, right: 0.75, bottom: 0.25 },
  ]);
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
  redBox(element, rect(180, 90, 640, 320));

  await captureStage(sourceOf(element));

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

  expect(vi.mocked(rasterize)).toHaveBeenCalledWith(tile, 1, { style: ANCHORED, leave: [] });
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

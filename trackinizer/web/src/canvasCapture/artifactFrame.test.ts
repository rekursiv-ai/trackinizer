import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

// Paths, not URLs: jsdom's `URL` is not the one `fs` checks for.
const where = (path: string) => new URL(path, import.meta.url).pathname;
const read = (path: string) => readFileSync(where(path), "utf8");
const SCRIPT = read("../../../server/assets/artifact_capture.js");
const LIBRARY = where("../../node_modules/html-to-image/dist/html-to-image.js");

type Library = { toPng: ReturnType<typeof vi.fn> };
const heard: EventListener[] = [];
let sent: unknown[];
let library: Library;

/** Run the script as the served page does, remembering the listener it adds. */
function load() {
  const add = window.addEventListener.bind(window);
  vi.spyOn(window, "addEventListener").mockImplementation((type: string, listener: EventListenerOrEventListenerObject, options?: boolean | AddEventListenerOptions) => {
    if (type === "message") heard.push(listener as EventListener);
    add(type, listener, options);
  });
  window.eval(SCRIPT);
}

/** Ask the page for a picture as `source` does. */
function ask(source: MessageEventSource | null, data: unknown) {
  window.dispatchEvent(new MessageEvent("message", { data, source }));
}

const flush = () => new Promise((resolve) => setTimeout(resolve));

beforeEach(() => {
  sent = [];
  library = { toPng: vi.fn().mockResolvedValue("data:image/png;base64,iVBOR") };
  // In jsdom the top window is its own parent, so a reply to the parent lands here.
  vi.spyOn(window.parent, "postMessage").mockImplementation((message: unknown) => void sent.push(message));
  Object.assign(window, { htmlToImage: library });
});

afterEach(() => {
  for (const listener of heard.splice(0)) window.removeEventListener("message", listener);
  delete (window as { htmlToImage?: unknown }).htmlToImage;
  document.head.replaceChildren();
  vi.restoreAllMocks();
});

test("the page does nothing until it is asked: no listener work, no script, no message", () => {
  delete (window as { htmlToImage?: unknown }).htmlToImage;
  load();
  expect(sent).toEqual([]);
  expect(document.head.querySelector("script")).toBeNull();
});

test("a request from the parent window is answered to the parent with the picture and its id", async () => {
  load();
  ask(window.parent, { type: "trax-capture", id: "r1" });
  await flush();
  expect(sent).toEqual([{ type: "trax-capture-result", id: "r1", png: "data:image/png;base64,iVBOR", width: window.innerWidth, height: window.innerHeight }]);
  expect(library.toPng).toHaveBeenCalledWith(document.documentElement, expect.objectContaining({ width: window.innerWidth, height: window.innerHeight, skipFonts: true, pixelRatio: 1 }));
});

test("the picture is the viewport as scrolled", async () => {
  Object.assign(window, { scrollX: 30, scrollY: 400 });
  load();
  ask(window.parent, { type: "trax-capture", id: "r1" });
  await flush();
  expect(library.toPng.mock.calls[0]![1]).toMatchObject({ style: { transform: "translate(-30px,-400px)" } });
});

test("a request from any other window, or of any other kind, is ignored", async () => {
  const other = document.body.appendChild(document.createElement("iframe")).contentWindow;
  load();
  ask(other, { type: "trax-capture", id: "r1" });
  ask(window.parent, { type: "something-else", id: "r2" });
  ask(window.parent, { type: "trax-capture" });
  ask(window.parent, null);
  ask(null, { type: "trax-capture", id: "r3" });
  await flush();
  expect(sent).toEqual([]);
  expect(library.toPng).not.toHaveBeenCalled();
});

test("a rasterizer that fails answers with the reason, and never throws into the page", async () => {
  library.toPng.mockRejectedValue(new Error("tainted canvas"));
  load();
  ask(window.parent, { type: "trax-capture", id: "r1" });
  await flush();
  expect(sent).toEqual([{ type: "trax-capture-result", id: "r1", error: "tainted canvas" }]);
});

test("the library is loaded from the pinned URL with its integrity hash, once, on the first request", async () => {
  delete (window as { htmlToImage?: unknown }).htmlToImage;
  load();
  ask(window.parent, { type: "trax-capture", id: "r1" });
  ask(window.parent, { type: "trax-capture", id: "r2" });
  const scripts = document.head.querySelectorAll("script");
  expect(scripts).toHaveLength(1);
  const script = scripts[0]!;
  expect(script.src).toBe("https://cdn.jsdelivr.net/npm/html-to-image@1.11.13/dist/html-to-image.js");
  expect(script.integrity).toBe(`sha384-${createHash("sha384").update(readFileSync(LIBRARY)).digest("base64")}`);
  expect(script.crossOrigin).toBe("anonymous");
  Object.assign(window, { htmlToImage: library });
  script.onload!(new Event("load"));
  await flush();
  expect(sent.map((each) => (each as { id: string }).id)).toEqual(["r1", "r2"]);
});

test("a library that does not load answers with the reason, and the next request tries again", async () => {
  delete (window as { htmlToImage?: unknown }).htmlToImage;
  load();
  ask(window.parent, { type: "trax-capture", id: "r1" });
  document.head.querySelector("script")!.onerror!(new Event("error"));
  await flush();
  expect(sent).toEqual([{ type: "trax-capture-result", id: "r1", error: "the capture library did not load" }]);
  ask(window.parent, { type: "trax-capture", id: "r2" });
  expect(document.head.querySelectorAll("script")).toHaveLength(2);
});

test("the pinned library version is the one the web app depends on", () => {
  const { dependencies } = JSON.parse(read("../../package.json")) as { dependencies: { [name: string]: string } };
  expect(SCRIPT).toContain(`html-to-image@${dependencies["html-to-image"]}/dist/html-to-image.js`);
});

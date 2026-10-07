// Record the Craftax post's Trackinizer movie: the graph of the campaign
// growing in the order it was made, grouped by root, in the light theme with
// the sidebar away; then the two moments the post names, each as its own
// neighbourhood held at full speed under a caption, the rest of the page
// blurred, then its records opened; then the passage in the campaign log that
// found the scoring tool's bias, highlighted the same way.
//
// ORIGIN is a local no-auth trackinizer server holding the campaign's records
// (the hosted graph's islands under the roadmap, the Fire/Ice diagnosis and
// the fine-tune screen, mirrored oldest first so that Replay grows them in the
// campaign's order; its ids differ from the hosted ones, so the records the
// movie visits are found by title). A headless Chromium plays the script at
// VIEW, SCALE times denser, while Playwright's screencast keeps every frame it
// paints; the parts are marked and scheduled as in demo_video.ts, and ffmpeg
// (on PATH) encodes the post's MP4 and WebP poster, like the strategy clips.
//
// Usage: node scripts/craftax_video.ts ORIGIN OUT [--encode-only], after
// `npm run build`. Writes trackinizer-craftax.mp4, trackinizer-craftax.webp
// and parts.json (the parts' start times) into OUT, keeping the frames and
// their timing under OUT/work; --encode-only encodes those again.
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { type Browser, chromium, type Page } from "@playwright/test";
import { graphSettled, keepGraphState, requireTool } from "./capture.ts";

/** One frame the screencast kept: its file and when it came, in seconds. */
type Frame = { readonly file: string; readonly at: number };
/** How a part plays: `speed` times as fast, or fitted to `seconds`; `still` is the longest a frame of it may hold, over HOLD_MAX_S. */
type Play = ({ readonly speed: number } | { readonly seconds: number }) & { readonly still?: number };
/** Where a part of the video starts, and how it plays. */
type Part = { readonly name: string; readonly at: number } & Play;
type Mark = (name: string, play: Play) => void;

/** Output frames a second, the longest a still frame may show, and x264's quality (a UI reads sharp at 28). */
const FPS = 30;
const HOLD_MAX_S = 1;
const CRF = "28";
/** How long the replay of the whole graph plays. */
const REPLAY_S = 6;
/** The window the app lays out in, in CSS pixels: the demo's, which reads at the post's width. */
const VIEW = { width: 1280, height: 800 };
/** Device pixels per CSS pixel: the frames are SCALE times VIEW, sharp on a dense screen. */
const SCALE = 2;
const GLIDE_STEPS = 30;

/** The records the movie visits, by title; ids differ between servers. */
const MOMENT_1 = {
  root: "Execute Fire/Ice diagnosis with replay, interventions and learning evidence",
  evidence: "q89 post-gate causal matrix: discovery, route, health and terrain",
  belief: "Craftax2 agents fail deep floors from navigation and exploration, not missing equipment",
};
const MOMENT_2 = {
  issue: "Craftax2 tabula rasa win-rate campaign (5 parallel fine-tunes from g2-div8)",
  passage: "shortest episode",
};
const CAPTION_1 = ["The pigman was not the problem.", "100 saved games replayed under 12 conditions: steering to the ladder rescued 47 of 90. Health or supplies rescued none."];
const CAPTION_2 = ["The scoring tool was hiding wins.", "The campaign log: the evaluation kept each agent's shortest game. True win rate about 2%, matching training."];

const [originArg, outArg, flag] = process.argv.slice(2);
if (!originArg || !outArg || (flag !== undefined && flag !== "--encode-only")) {
  throw new Error("Usage: node scripts/craftax_video.ts ORIGIN OUT [--encode-only]");
}
const OUT = resolve(outArg);
const WORK = join(OUT, "work");
const CAPTURE = join(WORK, "capture.json");
requireTool("ffmpeg", ["-version"], "ffmpeg (`brew install ffmpeg`, `apt install ffmpeg`)");
mkdirSync(join(WORK, "frames"), { recursive: true });

const frames: Frame[] = [];
const parts: Part[] = [];
if (flag === "--encode-only") {
  type Kept = { name: string; at: number; still?: number } & ({ speed: number | null } | { seconds: number });
  const kept = JSON.parse(readFileSync(CAPTURE, "utf8")) as { frames: Frame[]; parts: Kept[] };
  frames.push(...kept.frames);
  parts.push(...kept.parts.map((part) => ("speed" in part ? { ...part, speed: part.speed ?? Infinity } : part)));
} else {
  // The screencast frames a page at its device size, so a context's
  // deviceScaleFactor alone leaves them at VIEW; the browser's own scale factor
  // makes the device SCALE times denser.
  const browser = await chromium.launch({ args: [`--force-device-scale-factor=${SCALE}`] });
  try {
    await record(browser, originArg);
  } finally {
    await browser.close();
  }
  // JSON has no Infinity: a part of infinite speed is kept as null and left out.
  writeFileSync(CAPTURE, `${JSON.stringify({ frames, parts }, (_, value: unknown) => (value === Infinity ? null : value), 1)}\n`);
  console.log(`kept ${frames.length} frames over ${frames.at(-1)?.at.toFixed(1)} s; parts: ${parts.map((p) => p.name).join(", ")}`);
}
for (const line of encode(frames, parts)) console.log(line);

async function record(browser: Browser, origin: string): Promise<void> {
  const context = await browser.newContext({
    viewport: { width: VIEW.width, height: VIEW.height },
    deviceScaleFactor: SCALE,
    colorScheme: "light",
    locale: "en-US",
    timezoneId: "UTC",
  });
  await keepGraphState(context, { roots: true });
  await context.addInitScript(() => {
    localStorage.setItem("trackinizer.theme", "light");
    sessionStorage.setItem("trackinizer.v2.panel.app.sidebar", "true");
  });
  await context.addInitScript(drawPointer);
  await context.addInitScript(captionTools);
  const page = await context.newPage();
  await page.goto(`${origin}/app/#/graph`);
  await page.locator(".graph-count").filter({ hasText: /nodes/ }).waitFor({ timeout: 120_000 });
  const start = performance.now();
  const clock = () => (performance.now() - start) / 1000;
  await page.screencast.start({
    size: { width: VIEW.width * SCALE, height: VIEW.height * SCALE },
    quality: 92,
    onFrame: ({ data: jpeg }) => {
      const file = join(WORK, "frames", `${String(frames.length).padStart(6, "0")}.jpg`);
      writeFileSync(file, jpeg);
      frames.push({ file, at: clock() });
    },
  });
  const part: Mark = (name, play) => parts.push({ name, at: clock(), ...play });
  await showGrowth(page, part);
  await showMoment1(page, part);
  await showMoment2(page, part);
  await page.screencast.stop();
  await context.close();
}

/** Replay the whole campaign graph, grouped by root, fitted as it grows, with the whole stage to itself. */
async function showGrowth(page: Page, part: Mark): Promise<void> {
  part("start", { speed: Infinity });
  await graphSettled(page);
  const total = await drawnCount(page);
  await page.keyboard.press("[");
  await page.getByRole("button", { name: "Replay" }).click();
  await page.getByRole("listbox", { name: "Replay at…" }).getByRole("option", { name: "10x" }).click();
  await page.mouse.move(VIEW.width / 2, 30);
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await showCaption(page, ["Trackinizer, during the Craftax campaign", "Every question, experiment and verdict, in the order it was made."]);
  part("replay", { seconds: REPLAY_S / 10 });
  await page.getByRole("button", { name: "Replaying at 10x" }).waitFor();
  const replayed = page.getByRole("button", { name: "Replay", exact: true });
  let tenths = 1;
  for (let check = 0; !(await replayed.isVisible()); check += 1, await page.waitForTimeout(250)) {
    if (check > 2_400) throw new Error("The replay did not end within 10 minutes.");
    const drawn = await drawnCount(page);
    for (; tenths < 10 && drawn >= (tenths * total) / 10; tenths += 1) part("replay", { seconds: REPLAY_S / 10 });
    if (check % 4 === 0) await page.keyboard.press("f");
  }
  part("settle", { seconds: 1.5 });
  await graphSettled(page);
  await page.keyboard.press("f");
  await hideCaption(page);
  await page.waitForTimeout(1_200);
}

/**
 * Open the roots list, pick the Fire/Ice diagnosis root, focus on it and hold
 * its neighbourhood under the caption, then open the causal matrix and the
 * belief it led to. One hop holds both and stays within the island; a second
 * reaches a record of another island, laid out far away, which would shrink
 * the fit.
 */
async function showMoment1(page: Page, part: Mark): Promise<void> {
  part("moment1", { speed: 2 });
  // The key's counts told the growth; from here the neighbourhoods have the canvas alone.
  await page.getByRole("button", { name: "Key", exact: true }).click();
  await page.waitForTimeout(400);
  await page.keyboard.press("[");
  const root = page.getByRole("listbox", { name: "Roots" }).getByRole("option").filter({ hasText: MOMENT_1.root.slice(0, 40) });
  await root.waitFor();
  await page.waitForTimeout(600);
  await glideTo(page, root);
  await root.click();
  await page.locator(".peek h1").waitFor();
  await page.waitForTimeout(1_800);
  await page.keyboard.press("[");
  await page.waitForTimeout(600);
  await page.keyboard.press(".");
  await page.waitForTimeout(1_500);
  await hops(page, 1);
  await onlyThese(page);
  await hold(page, part, "moment1-hold", CAPTION_1);
  part("moment1-records", { speed: 2 });
  // Zoom around each record before clicking it: the zoom keeps it under the pointer, so the click lands.
  await zoomAt(page, MOMENT_1.evidence, 2);
  await page.waitForTimeout(600);
  await clickNode(page, MOMENT_1.evidence);
  await page.waitForTimeout(2_800);
  await page.keyboard.press("f");
  await page.waitForTimeout(900);
  await zoomAt(page, MOMENT_1.belief, 2);
  await page.waitForTimeout(600);
  await clickNode(page, MOMENT_1.belief);
  await page.waitForTimeout(2_800);
  await page.keyboard.press("Escape");
  await page.keyboard.press("Escape");
  await page.waitForTimeout(600);
}

/** Search for the win-rate campaign, focus on it and hold it under the caption, then open its log at the passage about the scoring tool. */
async function showMoment2(page: Page, part: Mark): Promise<void> {
  part("moment2", { speed: 2 });
  const box = page.getByRole("searchbox", { name: "Search the graph" }).or(page.getByPlaceholder("Search the graph"));
  await glideTo(page, box);
  await box.click();
  await page.keyboard.type("tabula rasa win-rate", { delay: 40 });
  const match = page.getByRole("listbox", { name: "Matches" }).getByRole("option").filter({ hasText: "tabula rasa win-rate campaign" }).first();
  await match.waitFor();
  await page.waitForTimeout(600);
  await glideTo(page, match);
  await match.click();
  await page.locator(".peek h1").filter({ hasText: "tabula rasa" }).waitFor();
  await page.waitForTimeout(1_200);
  // The search box keeps the keyboard, so focus by double-clicking the match.
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await match.dblclick();
  await page.getByRole("group", { name: "Hops" }).waitFor({ timeout: 15_000 });
  await page.waitForTimeout(800);
  await page.keyboard.press("[");
  await page.waitForTimeout(600);
  await hops(page, 1);
  await onlyThese(page);
  await hold(page, part, "moment2-hold", CAPTION_2);
  part("moment2-log", { speed: 2 });
  await zoomAt(page, MOMENT_2.issue.slice(0, 40), 3);
  await page.waitForTimeout(600);
  await clickNode(page, MOMENT_2.issue);
  await page.waitForTimeout(1_200);
  const open = page.locator(".peek").getByRole("link", { name: "Open" });
  await glideTo(page, open);
  await open.click();
  await page.getByRole("heading", { level: 1, name: MOMENT_2.issue.slice(0, 30) }).waitFor({ timeout: 30_000 });
  await page.waitForTimeout(1_000);
  await highlightPassage(page, MOMENT_2.passage);
  // The smooth scroll must end before the veil's hole is cut around the passage.
  await page.waitForTimeout(1_800);
  part("passage", { speed: 1, still: 5 });
  await showCaption(page, CAPTION_2, ".cx-mark");
  await page.waitForTimeout(5_000);
  await hideCaption(page);
  await page.waitForTimeout(800);
}

/**
 * The moment itself: the selection cleared so the neighbourhood has the stage,
 * fitted, then held at the movie's own speed under `caption`, with everything
 * but the graph blurred.
 */
async function hold(page: Page, part: Mark, name: string, caption: readonly string[]): Promise<void> {
  await page.keyboard.press("Escape");
  await page.waitForTimeout(400);
  await page.keyboard.press("f");
  await page.waitForTimeout(1_200);
  part(name, { speed: 1, still: 4 });
  await showCaption(page, caption, ".graph-canvas");
  await page.waitForTimeout(3_500);
  await hideCaption(page);
  await page.waitForTimeout(500);
}

/** Pick `count` hops around the focus. */
async function hops(page: Page, count: number): Promise<void> {
  const button = page.getByRole("group", { name: "Hops" }).getByRole("button", { name: new RegExp(`^${count} hop`) });
  await glideTo(page, button);
  await button.click();
  await page.waitForTimeout(600);
}

/**
 * Encode the kept frames into OUT: the MP4 and the poster (the graph grown
 * and grouped, as the settle part shows it). Each frame shows for as long as
 * it did, divided by its part's speed, and at most `HOLD_MAX_S` (or the
 * part's own `still`); a part of infinite speed is left out. Returns what it
 * wrote.
 */
function encode(frames: readonly Frame[], parts: readonly Part[]): string[] {
  const timeline = schedule(frames, parts);
  const list = join(WORK, "frames.ffconcat");
  const lines = timeline.flatMap(({ file, seconds }) => [`file '${file}'`, `duration ${seconds.toFixed(4)}`]);
  writeFileSync(list, ["ffconcat version 1.0", ...lines, `file '${timeline.at(-1)!.file}'`, ""].join("\n"));
  const input = ["-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", list];
  const mp4 = join(OUT, "trackinizer-craftax.mp4");
  const video = [...input, "-vf", `fps=${FPS},scale=out_range=tv,format=yuv420p`, "-color_range", "tv"];
  execFileSync("ffmpeg", [...video, "-c:v", "libx264", "-preset", "slow", "-crf", CRF, "-movflags", "+faststart", mp4]);
  const still = timeline.find(({ part }) => part === "settle")!.file;
  const poster = join(OUT, "trackinizer-craftax.webp");
  execFileSync("ffmpeg", ["-hide_banner", "-loglevel", "error", "-y", "-i", still, "-c:v", "libwebp", "-quality", "85", poster]);
  const starts = partStarts(timeline, parts);
  writeFileSync(join(OUT, "parts.json"), `${JSON.stringify(starts, null, 2)}\n`);
  const total = timeline.reduce((sum, { seconds }) => sum + seconds, 0);
  return [
    `video: ${total.toFixed(1)} s from ${frames.length} frames; parts start at ${JSON.stringify(starts)}`,
    ...[mp4, poster].map((file) => `${file}: ${statSync(file).size} bytes`),
  ];
}

/** Each kept frame with its part and how long it shows. */
function schedule(frames: readonly Frame[], parts: readonly Part[]): { file: string; part: string; seconds: number }[] {
  const speedOf = (part: Part, end: number) => ("speed" in part ? part.speed : (end - part.at) / part.seconds);
  const timeline = [];
  for (const [k, frame] of frames.entries()) {
    const index = parts.findLastIndex((part) => part.at <= frame.at);
    const part = parts[index];
    if (!part) continue;
    const end = parts[index + 1]?.at ?? frames.at(-1)!.at;
    const shown = (frames[k + 1]?.at ?? frame.at + HOLD_MAX_S) - frame.at;
    const speed = speedOf(part, end);
    if (Number.isFinite(speed)) timeline.push({ file: frame.file, part: part.name, seconds: Math.min(shown / speed, part.still ?? HOLD_MAX_S) });
  }
  return timeline;
}

/** Where each part starts in the video, in seconds. */
function partStarts(timeline: readonly { part: string; seconds: number }[], parts: readonly Part[]): { [part: string]: number } {
  const starts: { [part: string]: number } = {};
  let at = 0;
  for (const { part, seconds } of timeline) {
    starts[part] ??= Number(at.toFixed(2));
    at += seconds;
  }
  return Object.fromEntries(parts.filter(({ name }) => name in starts).map(({ name }) => [name, starts[name]!]));
}

/** Zoom in `notches` wheel steps around the node titled `title`, so its cluster fills the view. */
async function zoomAt(page: Page, title: string, notches: number): Promise<void> {
  const at = await screenOfTitle(page, title);
  await glide(page, at.x, at.y);
  for (let step = 0; step < notches; step += 1) {
    await page.mouse.wheel(0, -120);
    await page.waitForTimeout(220);
  }
}

/** Hide what lies outside the focus, so a fit frames the neighbourhood alone. */
async function onlyThese(page: Page): Promise<void> {
  const only = page.getByRole("group", { name: "Outside the focus" }).getByRole("button", { name: "Only these" });
  await glideTo(page, only);
  await only.click();
  await page.waitForTimeout(400);
}

/** Glide to the drawn node titled `title` and click it; a miss is logged, not fatal. */
async function clickNode(page: Page, title: string): Promise<void> {
  const at = await screenOfTitle(page, title);
  await glide(page, at.x, at.y);
  await page.waitForTimeout(500);
  await page.mouse.click(at.x, at.y);
  try {
    await page.locator(".peek h1").filter({ hasText: title.slice(0, 24) }).waitFor({ timeout: 5_000 });
  } catch {
    console.warn(`The click did not open ${title.slice(0, 40)}; going on.`);
  }
}

/** Where the node titled `title` is in the window. */
async function screenOfTitle(page: Page, title: string): Promise<{ x: number; y: number }> {
  return page.evaluate((wanted) => {
    const debug = (window as unknown as { trackinizer: { graph(): { nodes: { title: string; screen: { x: number; y: number } }[] } } }).trackinizer;
    const node = debug.graph().nodes.find((row) => row.title.startsWith(wanted));
    if (!node) throw new Error(`No drawn node titled ${wanted}`);
    return node.screen;
  }, title);
}

/** How many nodes the graph draws now. */
async function drawnCount(page: Page): Promise<number> {
  return page.evaluate(() => (window as unknown as { trackinizer: { graph(): { nodes: unknown[] } } }).trackinizer.graph().nodes.length);
}

/** Scroll the opened record to the first paragraph holding `text` and mark it. */
async function highlightPassage(page: Page, text: string): Promise<void> {
  await page.evaluate((wanted) => {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node: Text | null = null;
    while ((node = walker.nextNode() as Text | null)) {
      if (node.data.toLowerCase().includes(wanted.toLowerCase())) break;
    }
    if (!node) throw new Error(`No text holding ${wanted}`);
    const block = node.parentElement!.closest("p, li, blockquote, td") ?? node.parentElement!;
    block.classList.add("cx-mark");
    const style = document.createElement("style");
    style.textContent = ".cx-mark { background: #fff2a8; box-shadow: 0 0 0 6px #fff2a8; border-radius: 4px; transition: background 600ms; }";
    document.head.appendChild(style);
    block.scrollIntoView({ block: "center", behavior: "smooth" });
  }, text);
}

/** Show a caption card over the page: a heading line and a sentence; with `keep`, blur all but what that selector matches. */
async function showCaption(page: Page, [head, body]: readonly string[], keep?: string): Promise<void> {
  await page.evaluate(
    ([h, b, k]) => (window as unknown as { cxCaption(h: string, b: string, k?: string): void }).cxCaption(h, b, k),
    [head, body, keep] as const,
  );
}

async function hideCaption(page: Page): Promise<void> {
  await page.evaluate(() => (window as unknown as { cxCaptionHide(): void }).cxCaptionHide());
  await page.waitForTimeout(400);
}

/** Move the pointer to the middle of `target` the way a hand would. */
async function glideTo(page: Page, target: ReturnType<Page["locator"]>): Promise<void> {
  const box = (await target.boundingBox())!;
  await glide(page, box.x + Math.min(box.width / 2, 120), box.y + box.height / 2);
}

/** Move the pointer to `x`, `y` over half a second, easing in and out, a frame a step. */
async function glide(page: Page, x: number, y: number): Promise<void> {
  const rest = { x: VIEW.width / 2, y: VIEW.height / 2 };
  const from = await page.evaluate((home) => (window as unknown as { pointerAt?: { x: number; y: number } }).pointerAt ?? home, rest);
  for (let step = 1; step <= GLIDE_STEPS; step += 1) {
    const t = step / GLIDE_STEPS;
    const eased = t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2;
    await page.mouse.move(from.x + (x - from.x) * eased, from.y + (y - from.y) * eased);
    await page.waitForTimeout(16);
  }
}

/** Draw a pointer that follows the mouse, and remember where it is (`window.pointerAt`). */
function drawPointer(): void {
  const pointer = document.createElement("div");
  pointer.setAttribute("aria-hidden", "true");
  pointer.style.cssText =
    "position:fixed;left:0;top:0;width:20px;height:20px;z-index:2147483647;pointer-events:none;" +
    "background:no-repeat url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 20 20'%3E" +
    "%3Cpath d='M2 1l14 9-6 1.5 3.5 7-2.5 1.2-3.5-7L3 17z' fill='white' stroke='black' stroke-width='1.2'/%3E%3C/svg%3E\");" +
    "opacity:0";
  const at = { x: 0, y: 0 };
  Object.assign(window, { pointerAt: at });
  document.addEventListener(
    "mousemove",
    (event) => {
      at.x = event.clientX;
      at.y = event.clientY;
      pointer.style.opacity = "1";
      pointer.style.transform = `translate(${event.clientX}px,${event.clientY}px)`;
    },
    { capture: true },
  );
  document.addEventListener("DOMContentLoaded", () => document.body.appendChild(pointer));
}

/**
 * The caption card the movie draws over the page, as `window.cxCaption(head,
 * body, keep?)` and `window.cxCaptionHide()`. With `keep`, a selector, a veil
 * blurs and lightens the whole page except a hole cut around the first element
 * it matches, so the moment holds on that alone; the graph's zoom buttons hide
 * under it.
 */
function captionTools(): void {
  let card: HTMLDivElement | null = null;
  let veil: HTMLDivElement | null = null;
  const ensureCard = () => {
    if (card) return card;
    card = document.createElement("div");
    card.setAttribute("aria-hidden", "true");
    card.style.cssText =
      "position:fixed;left:28px;bottom:28px;max-width:600px;z-index:2147483646;pointer-events:none;" +
      "padding:16px 20px;border-radius:12px;background:rgba(255,255,255,0.97);color:#101112;" +
      "box-shadow:0 12px 40px rgba(0,0,0,0.18),0 0 0 1px rgba(0,0,0,0.06);" +
      "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;" +
      "opacity:0;transform:translateY(8px);transition:opacity 350ms ease,transform 350ms ease";
    document.body.appendChild(card);
    return card;
  };
  const ensureVeil = () => {
    if (veil) return veil;
    veil = document.createElement("div");
    veil.setAttribute("aria-hidden", "true");
    veil.style.cssText =
      "position:fixed;inset:0;z-index:2147483645;pointer-events:none;background:rgba(255,255,255,0.55);" +
      "backdrop-filter:blur(9px);-webkit-backdrop-filter:blur(9px);opacity:0;transition:opacity 450ms ease";
    const style = document.createElement("style");
    style.textContent = ".cx-moment .graph-zoom { visibility: hidden; }";
    document.head.appendChild(style);
    document.body.appendChild(veil);
    return veil;
  };
  Object.assign(window, {
    cxCaption(head: string, body: string, keep?: string) {
      const box = ensureCard();
      box.innerHTML = "";
      const h = document.createElement("div");
      h.style.cssText = "font-size:19px;font-weight:650;line-height:26px;margin-bottom:4px";
      h.textContent = head;
      const b = document.createElement("div");
      b.style.cssText = "font-size:15px;line-height:22px;color:#3b3f45";
      b.textContent = body;
      box.append(h, b);
      const kept = keep ? document.querySelector(keep) : null;
      if (kept) {
        const r = kept.getBoundingClientRect();
        const [l, t, w, b2] = [r.left - 8, r.top - 8, r.right + 8, r.bottom + 8].map((v) => Math.round(v));
        const v = ensureVeil();
        v.style.clipPath = `polygon(evenodd, 0 0, 100% 0, 100% 100%, 0 100%, 0 0, ${l}px ${t}px, ${w}px ${t}px, ${w}px ${b2}px, ${l}px ${b2}px, ${l}px ${t}px)`;
        document.documentElement.classList.add("cx-moment");
        requestAnimationFrame(() => {
          v.style.opacity = "1";
        });
      }
      requestAnimationFrame(() => {
        box.style.opacity = "1";
        box.style.transform = "translateY(0)";
      });
    },
    cxCaptionHide() {
      if (card) {
        card.style.opacity = "0";
        card.style.transform = "translateY(8px)";
      }
      if (veil) veil.style.opacity = "0";
      document.documentElement.classList.remove("cx-moment");
    },
  });
}

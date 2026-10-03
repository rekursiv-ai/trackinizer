// Record the README's demo of trackinizer's main views, at most a minute long:
// the graph of some 5,000 nodes growing in the order they were made, grouped
// by root; a root's island, a Belief in it and its neighbourhood; an Issue and
// an HTML Artifact; and a message to an agent in the console, answered.
//
// It starts a seeded server of its own on port 8829 (scripts/capture.ts) and a
// stand-in for the messaged agent's `trax run`, which drains what reaches the
// session and appends a canned reply as the session's next records. A
// headless Chromium plays the script at 1280 by 800 in the dark theme while
// Playwright's screencast keeps every frame it paints, with the time it came.
// The script marks where each part starts and how fast it plays: the replay is
// fitted to five seconds, a tenth of its nodes each half second, the settling
// after it to about one, the rest plays at twice its speed, and any still
// stretch is cut to a second. ffmpeg then encodes the frames as H.264 (MP4)
// and VP9 (WebM) at 30 fps, and img2webp makes a short, small animated WebP of
// the opening for the README itself. ffmpeg and img2webp (libwebp) must be on
// PATH.
//
// Usage: node scripts/demo_video.ts DIR, after `./npm run demo-video -- DIR`
// has built the app. It writes demo.mp4, demo.webm, poster.jpg, poster.webp,
// teaser.webp and parts.json, the parts' start times in the video, into DIR.
import { execFileSync } from "node:child_process";
import { mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { type Browser, chromium, type Page } from "@playwright/test";
import { graphSettled, keepGraphState, requireTool, type Seeded, serveSeeded } from "./capture.ts";

/** One frame the screencast kept: its file and when it came, in seconds. */
type Frame = { readonly file: string; readonly at: number };

/** Where a part of the video starts, and how it plays: `speed` times as fast, or fitted to `seconds`. */
type Part = { readonly name: string; readonly at: number } & ({ readonly speed: number } | { readonly seconds: number });

/** What the stand-in agent says to any message. */
const REPLY =
  "Done: all ten reruns on one GPU type scored 71.3 to 71.5, the same 0.2-point spread, so the GPU type does not explain the drift.";
/** What the demo asks the agent. */
const QUESTION = "how did the one-GPU reruns go?";
/** Output frames a second, and the longest a still frame may show. */
const FPS = 30;
const HOLD_MAX_S = 1;
/** How long the replay of the whole graph plays. */
const REPLAY_S = 5;
/** Mouse moves a glide takes, a frame apart. */
const GLIDE_STEPS = 30;

const outArg = process.argv[2];
if (!outArg) throw new Error("Usage: node scripts/demo_video.ts DIR");
const OUT = resolve(outArg);
requireTool("ffmpeg", ["-version"], "ffmpeg (`brew install ffmpeg`, `apt install ffmpeg`)");
requireTool("img2webp", ["-version"], "libwebp (`brew install webp`, `apt install webp`)");
mkdirSync(OUT, { recursive: true });
const served = await serveSeeded(8829);
try {
  const agent = standIn(served.origin, served.seeded.session);
  const frames: Frame[] = [];
  const parts: Part[] = [];
  const browser = await chromium.launch();
  try {
    await record(browser, served, { frames, parts });
  } finally {
    await browser.close();
    await agent.stop();
  }
  for (const line of encode(frames, parts, served.data)) console.log(line);
} finally {
  await served.stop();
}

/** Play the demo in a fresh window, keeping its frames and marking its parts. */
async function record(
  browser: Browser,
  { origin, seeded, data }: { origin: string; seeded: Seeded; data: string },
  { frames, parts }: { frames: Frame[]; parts: Part[] },
): Promise<void> {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 },
    colorScheme: "dark",
    locale: "en-US",
    timezoneId: "UTC",
  });
  await keepGraphState(context, { roots: true });
  // Headless Chromium draws no pointer, so the page draws one where the mouse is.
  await context.addInitScript(drawPointer);
  const page = await context.newPage();
  await page.goto(`${origin}/app/#/graph`);
  await page.locator(".graph-count").filter({ hasText: "5,000 nodes" }).waitFor();
  const start = performance.now();
  const clock = () => (performance.now() - start) / 1000;
  mkdirSync(join(data, "frames"), { recursive: true });
  await page.screencast.start({
    size: { width: 1280, height: 800 },
    quality: 92,
    onFrame: ({ data: jpeg }) => {
      const file = join(data, "frames", `${String(frames.length).padStart(6, "0")}.jpg`);
      writeFileSync(file, jpeg);
      frames.push({ file, at: clock() });
    },
  });
  const part = (name: string, play: { speed: number } | { seconds: number }) => parts.push({ name, at: clock(), ...play });
  await showGraph(page, seeded, part);
  await showIssue(page, part);
  await showArtifact(page, part);
  await showConsole(page, seeded, part);
  await page.screencast.stop();
  await context.close();
}

type Mark = (name: string, play: { speed: number } | { seconds: number }) => void;

/** Replay the whole graph grouped by root, then frame the showcase's island, select its Belief and focus on it. */
async function showGraph(page: Page, seeded: Seeded, part: Mark): Promise<void> {
  part("start", { speed: Infinity });
  await graphSettled(page);
  const total = await drawnCount(page);
  await page.getByRole("button", { name: "Replay" }).click();
  await page.getByRole("listbox", { name: "Replay at…" }).getByRole("option", { name: "10x" }).click();
  // Before the replay shows: the pointer off the key, over the empty header, and no
  // focus ring on Replay, which the fits' keys would otherwise draw.
  await page.mouse.move(640, 30);
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  // Each node the replay adds costs more the more it has drawn, so the last
  // thousand take most of its time: it is played a tenth of its nodes at a
  // time, each tenth fitted to half a second, so the graph grows at one pace.
  // The view follows the growth, a fit (`f`) a second, so what has grown fills
  // it from the first nodes on.
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
  part("settle", { seconds: 1.2 });
  await graphSettled(page);
  part("roots", { speed: 2 });
  await page.waitForTimeout(1_200);
  await glideTo(page, page.getByRole("group", { name: "Sort roots" }).getByRole("button", { name: "Size" }));
  await page.getByRole("group", { name: "Sort roots" }).getByRole("button", { name: "Size" }).click();
  await page.waitForTimeout(800);
  const root = page.getByRole("listbox", { name: "Roots" }).getByRole("option").filter({ hasText: await titleOf(page, seeded.root) });
  await glideTo(page, root);
  await root.click();
  await page.locator(".peek h1").waitFor();
  await page.waitForTimeout(2_400);
  const belief = await screenOf(page, seeded.cited);
  await glide(page, belief.x, belief.y);
  await page.waitForTimeout(900);
  await page.mouse.click(belief.x, belief.y);
  await page.locator(".peek h1").filter({ hasText: await titleOf(page, seeded.cited) }).waitFor();
  await page.waitForTimeout(2_000);
  await page.keyboard.press(".");
  await page.waitForTimeout(1_800);
  const hops = page.getByRole("button", { name: /^2 hops/ });
  await glideTo(page, hops);
  await hops.click();
  await page.waitForTimeout(1_800);
}

/** Open the Issues list, then the tokenizer effort's Issue, and scroll to its rail and graph. */
async function showIssue(page: Page, part: Mark): Promise<void> {
  part("issues", { speed: 2 });
  await clickNav(page, "Issues");
  await page.locator(".row").first().waitFor();
  await page.waitForTimeout(1_500);
  const row = page.locator(".row").filter({ hasText: "Make the tokenizer twice as fast" });
  await glideTo(page, row);
  await row.click();
  await page.getByRole("heading", { level: 1, name: "Make the tokenizer twice as fast" }).waitFor();
  await page.waitForTimeout(1_800);
  await scroll(page, 500);
  await page.waitForTimeout(1_600);
}

/** Open the Artifacts list, then the published HTML report, scrolled to its sandboxed page. */
async function showArtifact(page: Page, part: Mark): Promise<void> {
  part("artifacts", { speed: 2 });
  await clickNav(page, "Artifacts");
  await page.locator(".row").first().waitFor();
  await page.waitForTimeout(1_200);
  // A published Artifact is complete, so it is among the closed ones, the newest first.
  const closed = page.getByRole("button", { name: "Closed", exact: true });
  await glideTo(page, closed);
  await closed.click();
  await page.waitForTimeout(1_000);
  const row = page.locator(".row").filter({ hasText: "Merge-rank cache: benchmark report" });
  await glideTo(page, row);
  await row.click();
  const frame = page.locator(".artifact-html-frame");
  await frame.waitFor();
  await page.frameLocator(".artifact-html-frame").getByRole("heading", { name: "Merge-rank cache benchmark" }).waitFor();
  await page.waitForTimeout(1_000);
  await frame.scrollIntoViewIfNeeded();
  await page.waitForTimeout(2_200);
}

/** Pick the agent in the console, ask it through @, and wait for its answer. */
async function showConsole(page: Page, seeded: Seeded, part: Mark): Promise<void> {
  part("console", { speed: 2 });
  await clickNav(page, "Console");
  await page.locator(".console-line").last().waitFor();
  await page.waitForTimeout(1_200);
  const agent = page.getByRole("region", { name: "Agents" }).getByRole("checkbox", { name: seeded.agent, exact: true });
  await glideTo(page, agent);
  await agent.check();
  await page.waitForTimeout(1_000);
  const box = page.getByLabel("Message", { exact: true });
  await glideTo(page, box);
  await box.click();
  await page.keyboard.type(`@${seeded.agent.slice(0, 3)}`, { delay: 90 });
  await page.getByRole("listbox", { name: "Agents" }).waitFor();
  await page.waitForTimeout(700);
  await page.keyboard.press("Tab");
  await page.keyboard.type(QUESTION, { delay: 45 });
  await page.waitForTimeout(400);
  await page.keyboard.press("Enter");
  await page.locator(".console-line").filter({ hasText: REPLY.slice(0, 40) }).waitFor({ timeout: 30_000 });
  await page.waitForTimeout(2_400);
}

/**
 * The messaged session's `trax run`, played by this script: it waits on the
 * session's inbound queue, as `trax run` does, which marks the session polled
 * so the console may message it, and answers each message as the session's
 * next two records: the message as typed into the CLI, then the reply.
 */
function standIn(origin: string, session: string): { stop(): Promise<void> } {
  const stopped = new AbortController();
  const loop = (async () => {
    while (!stopped.signal.aborted) {
      const drained = await fetch(`${origin}/api/sessions/${session}/inbound?wait_sec=5`, { signal: stopped.signal }).catch(
        () => null,
      );
      if (!drained?.ok) continue;
      const { messages } = (await drained.json()) as { messages: { text: string; source: string }[] };
      for (const message of messages) await answer(origin, session, message);
    }
  })();
  return {
    stop: async () => {
      stopped.abort();
      await loop;
    },
  };
}

/** Append `message` and the reply to `session`'s first file, after a moment's thought. */
async function answer(origin: string, session: string, message: { text: string; source: string }): Promise<void> {
  await append(origin, session, "UserMessage", `${message.source}: ${message.text}`);
  await new Promise((resolve) => setTimeout(resolve, 1_500));
  await append(origin, session, "AssistantMessage", REPLY);
}

/** Append one turn of `kind` saying `content` to `session`'s first file, stamped now. */
async function append(origin: string, session: string, kind: string, content: string): Promise<void> {
  const { parts } = (await (await fetch(`${origin}/api/sessions/${session}/parts`)).json()) as {
    parts: { name: string; metadata: object; ir_id: string; format: string; records: number }[];
  };
  const { name, metadata, ir_id, format, records } = parts[0]!;
  const timestamp = new Date().toISOString();
  const appended = await fetch(`${origin}/api/sessions/${session}/records`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({
      name,
      manifest: { name, metadata, ir_id, format, records: records + 1 },
      records: [{ idx: records, kind, timestamp, payload: { content, timestamp }, text: content }],
    }),
  });
  if (!appended.ok) throw new Error(`The stand-in agent could not reply: ${appended.status} ${await appended.text()}`);
}

/**
 * Encode the kept frames into OUT: the MP4, the WebM, the poster (the graph
 * grown and grouped) and the teaser (the opening, small). Each frame shows for
 * as long as it did, divided by its part's speed, and at most `HOLD_MAX_S`; a
 * part of speed Infinity is left out. Returns what it wrote.
 */
function encode(frames: readonly Frame[], parts: readonly Part[], work: string): string[] {
  const timeline = schedule(frames, parts);
  const list = join(work, "frames.ffconcat");
  const lines = timeline.flatMap(({ file, seconds }) => [`file '${file}'`, `duration ${seconds.toFixed(4)}`]);
  writeFileSync(list, ["ffconcat version 1.0", ...lines, `file '${timeline.at(-1)!.file}'`, ""].join("\n"));
  const input = ["-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", list];
  const mp4 = join(OUT, "demo.mp4");
  const video = [...input, "-vf", `fps=${FPS},scale=out_range=tv,format=yuv420p`, "-color_range", "tv"];
  execFileSync("ffmpeg", [...video, "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-movflags", "+faststart", mp4]);
  const webm = join(OUT, "demo.webm");
  execFileSync("ffmpeg", [...video, "-c:v", "libvpx-vp9", "-crf", "34", "-b:v", "0", "-row-mt", "1", webm]);
  const starts = partStarts(timeline, parts);
  const poster = timeline.find(({ part }) => part === "roots")!.file;
  writeFileSync(join(OUT, "poster.jpg"), readFileSync(poster));
  execFileSync("cwebp", ["-quiet", "-q", "85", poster, "-o", join(OUT, "poster.webp")]);
  const teaser = makeTeaser(mp4, starts.roots! + 3, work);
  writeFileSync(join(OUT, "parts.json"), `${JSON.stringify(starts, null, 2)}\n`);
  const total = timeline.reduce((sum, { seconds }) => sum + seconds, 0);
  return [
    `video: ${total.toFixed(1)} s from ${frames.length} frames; parts start at ${JSON.stringify(starts)}`,
    ...[mp4, webm, join(OUT, "poster.jpg"), join(OUT, "poster.webp"), teaser].map((file) => `${file}: ${statSync(file).size} bytes`),
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
    if (Number.isFinite(speed)) timeline.push({ file: frame.file, part: part.name, seconds: Math.min(shown / speed, HOLD_MAX_S) });
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

/**
 * The README's teaser: the video's first `seconds`, half the size, at 10 fps,
 * as a looping lossy WebP of at most 750 KiB, the most a file in the repository
 * may weigh; the quality steps down until it fits.
 */
function makeTeaser(mp4: string, seconds: number, work: string): string {
  const stills = join(work, "teaser");
  mkdirSync(stills, { recursive: true });
  execFileSync("ffmpeg", ["-hide_banner", "-loglevel", "error", "-y", "-t", seconds.toFixed(2), "-i", mp4, "-vf", "fps=10,scale=640:-1:flags=lanczos", join(stills, "%04d.png")]);
  const pngs = readdirSync(stills)
    .toSorted()
    .map((name) => join(stills, name));
  const teaser = join(OUT, "teaser.webp");
  for (const quality of [70, 60, 50, 40, 30, 20]) {
    execFileSync("img2webp", ["-loop", "0", "-lossy", "-q", String(quality), "-m", "6", "-d", "100", ...pngs, "-o", teaser], { stdio: "ignore" });
    if (statSync(teaser).size <= 750 * 1024) return teaser;
  }
  throw new Error(`The teaser is over 750 KiB at the lowest quality tried: ${statSync(teaser).size} bytes.`);
}

/** How many nodes the graph draws now. */
async function drawnCount(page: Page): Promise<number> {
  return page.evaluate(() => (window as unknown as { trackinizer: { graph(): { nodes: unknown[] } } }).trackinizer.graph().nodes.length);
}

/** The title the graph draws node `id` with. */
async function titleOf(page: Page, id: string): Promise<string> {
  return page.evaluate((wanted) => {
    const debug = (window as unknown as { trackinizer: { graph(): { nodes: { id: string; title: string }[] } } }).trackinizer;
    return debug.graph().nodes.find((node) => node.id === wanted)!.title;
  }, id);
}

/** Where node `id` is in the window. */
async function screenOf(page: Page, id: string): Promise<{ x: number; y: number }> {
  return page.evaluate((wanted) => {
    const debug = (window as unknown as { trackinizer: { graph(): { nodes: { id: string; screen: { x: number; y: number } }[] } } }).trackinizer;
    return debug.graph().nodes.find((node) => node.id === wanted)!.screen;
  }, id);
}

/** Click the sidebar's entry `label`, gliding to it first. */
async function clickNav(page: Page, label: string): Promise<void> {
  const entry = page.getByRole("navigation", { name: "Sidebar" }).getByRole("link", { name: label, exact: true });
  await glideTo(page, entry);
  await entry.click();
}

/** Move the pointer to the middle of `target` the way a hand would. */
async function glideTo(page: Page, target: ReturnType<Page["locator"]>): Promise<void> {
  const box = (await target.boundingBox())!;
  await glide(page, box.x + Math.min(box.width / 2, 120), box.y + box.height / 2);
}

/** Move the pointer to `x`, `y` over half a second, easing in and out, a frame a step. */
async function glide(page: Page, x: number, y: number): Promise<void> {
  const from = await page.evaluate(() => (window as unknown as { pointerAt?: { x: number; y: number } }).pointerAt ?? { x: 640, y: 400 });
  for (let step = 1; step <= GLIDE_STEPS; step += 1) {
    const t = step / GLIDE_STEPS;
    const eased = t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2;
    await page.mouse.move(from.x + (x - from.x) * eased, from.y + (y - from.y) * eased);
    await page.waitForTimeout(16);
  }
}

/** Scroll the view under the pointer by `pixels`, in small steps. */
async function scroll(page: Page, pixels: number): Promise<void> {
  for (let done = 0; done < pixels; done += 50) {
    await page.mouse.wheel(0, 50);
    await page.waitForTimeout(25);
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
    "transform:translate(640px,400px)";
  const at = { x: 640, y: 400 };
  Object.assign(window, { pointerAt: at });
  document.addEventListener(
    "mousemove",
    (event) => {
      at.x = event.clientX;
      at.y = event.clientY;
      pointer.style.transform = `translate(${event.clientX - 2}px,${event.clientY - 1}px)`;
    },
    true,
  );
  const attach = () => document.documentElement.append(pointer);
  if (document.documentElement) attach();
  else document.addEventListener("DOMContentLoaded", attach);
}

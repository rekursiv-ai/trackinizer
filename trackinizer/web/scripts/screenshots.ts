// Capture the public README's screenshots (docs/screenshots/*.webp) from a
// synthetic seed: the large graph, the console, and one Belief, Paper and
// Experiment.
//
// It starts a trackinizer server of its own on port 8812 (scripts/capture.ts)
// serving the build in dist-screenshots/, which `npm run screenshots` makes
// first, seeded by scripts/seed_screenshots.py; and captures each view at one
// window size and scale, in the dark theme, once its requests have answered, its
// fonts have loaded and, on the graph, the layout has stopped. Then it stops the
// server and removes its data. Each is encoded as lossless WebP by cwebp
// (libwebp), about a third of a PNG's size for these flat-coloured views, so
// cwebp must be on PATH.
//
// Usage: node scripts/screenshots.ts [outDir], outDir defaulting to ../docs/screenshots/.
import { execFileSync } from "node:child_process";
import { mkdirSync } from "node:fs";
import { join, resolve } from "node:path";
import { type Browser, chromium, type Page } from "@playwright/test";
import {
  graphSettled,
  keepGraphState,
  openRequests,
  requireTool,
  type Seeded,
  serveSeeded,
  settledRequests,
  WEB,
} from "./capture.ts";

/** One screenshot: its file, the hash it opens, and what shows it has drawn. */
type Shot = { readonly file: string; readonly hash: string; readonly drawn: (page: Page) => Promise<void> };

const OUT = resolve(process.argv[2] ?? join(WEB, "..", "docs", "screenshots"));

requireTool("cwebp", ["-version"], "libwebp (`brew install webp`, `apt install webp`)");
const served = await serveSeeded(8812);
try {
  mkdirSync(OUT, { recursive: true });
  // No launch deadline of our own: Playwright still fails at once if the browser
  // exits, and a cold first launch took up to 35 s on a CI runner, past its 30 s default.
  const browser = await chromium.launch({ timeout: 0 });
  try {
    for (const shot of shots(served.seeded)) console.log(await capture(browser, shot, served));
  } finally {
    await browser.close();
  }
} finally {
  await served.stop();
}

/** The README's five views, under the file names it links them by. */
function shots(seeded: Seeded): Shot[] {
  const detail = (file: string, id: string): Shot => ({
    file,
    hash: `#/lookup/${id}`,
    drawn: (page) => page.getByRole("heading", { level: 1 }).waitFor(),
  });
  return [
    // The newest 5,000 nodes grouped by root, each root's island labelled, the
    // roots list hidden so the graph takes the width.
    { file: "graph.webp", hash: "#/graph", drawn: graphSettled },
    { file: "chat.webp", hash: "#/console", drawn: (page) => page.locator(".console-line").last().waitFor() },
    detail("belief.webp", seeded.belief),
    detail("paper.webp", seeded.paper),
    detail("experiment.webp", seeded.experiment),
  ];
}

/** Open `shot` in a window of its own, wait until it is still, and write it; returns the file written. */
async function capture(browser: Browser, shot: Shot, { origin, data }: { origin: string; data: string }): Promise<string> {
  // The README shows each 640 px wide: half of this window, which is wide
  // enough for a detail's side rail, at twice the scale.
  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 },
    deviceScaleFactor: 2,
    colorScheme: "dark",
    locale: "en-US",
    timezoneId: "UTC",
    reducedMotion: "reduce",
  });
  try {
    await keepGraphState(context, { roots: false });
    const page = await context.newPage();
    const open = openRequests(page);
    await page.goto(`${origin}/app/${shot.hash}`);
    await shot.drawn(page);
    await settledRequests(page, open);
    await page.evaluate(async () => {
      await document.fonts.ready;
    });
    const png = join(data, "shot.png");
    await page.screenshot({ path: png, animations: "disabled", caret: "hide" });
    const path = join(OUT, shot.file);
    execFileSync("cwebp", ["-quiet", "-lossless", "-z", "9", png, "-o", path]);
    return path;
  } finally {
    await context.close();
  }
}

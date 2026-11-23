// THE FIXED VERIFIER. One script, written once, run by every design pass.
//
// Each agent used to write its own Playwright check from scratch — tokens spent
// re-deriving the same five assertions, and a different set of them each time. This is
// the harness instead: `node scripts/verify-answer.ts` after copying a composition to
// dist/blocks-latest.json. No rebuild: the route FETCHES that JSON at runtime, so only
// a change to the renderer itself needs `npm run build`.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { chromium } from "@playwright/test";

const url = process.env.VERIFY_URL ?? "http://127.0.0.1:8795/app/#/blocks";
const shot = process.argv[2];

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1760, height: 1400 }, deviceScaleFactor: 2 });
const problems: string[] = [];
page.on("pageerror", (e) => problems.push(`PAGE ERROR: ${e.message}`));
await page.goto(url, { waitUntil: "networkidle" });
// The composition is fetched, not bundled, so the census below waits for the first item to
// mount instead of trusting a fixed sleep; the sleep still runs, for the SMIL clock and
// the fonts, but a slow route no longer reads as an empty page.
await page.waitForSelector(".ui-composed-b > [data-node]", { timeout: 15000 }).catch(() => null);
await page.waitForTimeout(3500);

// 0. THE MOUNT CENSUS, before every other check, for two reasons. A ceiling check alone is
//    satisfied by a page that drew nothing — zero never exceeds a ceiling — so the answer's
//    own `compose` length is what proves the route is really showing it. And each geometry
//    check below auto-waits on a selector a non-answer page never produces, so without this
//    first the harness dies on a Playwright timeout instead of printing a verdict.
//
//    The two counts are taken at one depth: `compose.length` is the top level, and a nested
//    `card` or `row` would add its children to a flat `[data-node]` count.
const answer = JSON.parse(readFileSync(join(import.meta.dirname, "..", "dist", "blocks-latest.json"), "utf8")) as {
  readonly budget?: Record<string, number>;
  readonly compose?: readonly { readonly node: string }[];
};
const drawn = await page.evaluate(() => {
  const byKind: Record<string, number> = {};
  for (const node of document.querySelectorAll("[data-node]")) {
    const kind = node.getAttribute("data-node");
    if (kind) byKind[kind] = (byKind[kind] ?? 0) + 1;
  }
  return { byKind, items: document.querySelectorAll(".ui-composed-b > [data-node]").length };
});
const declared = answer.compose?.length ?? 0;

// The per-node contracts, counted here so both the checks and the closing readout use the
// same numbers. `.count()` resolves against the live DOM and never waits, so it is safe on
// a page that mounted nothing.
const counts = {
  chart: await page.locator(".ui-chart").count(),
  embed: await page.locator(".ui-embed").count(),
  flow: await page.locator(".react-flow").count(),
  image: await page.locator(".ui-image img").count(),
  code: await page.locator(".ui-code").count(),
  plate: await page.locator(".ui-plate").count(),
};

async function finish(): Promise<void> {
  if (shot) await page.screenshot({ path: shot, fullPage: true });
  await browser.close();
  console.log("nodes drawn:", Object.entries(drawn.byKind).map(([k, n]) => `${k}×${n}`).join(" ") || "none");
  console.log("contract nodes:", Object.entries(counts).filter(([, n]) => n).map(([k, n]) => `${k}×${n}`).join(" ") || "none");
  if (problems.length === 0) console.log("VERIFY: clean");
  else { console.log(`VERIFY: ${problems.length} problem(s)`); for (const p of problems) console.log("  -", p); }
  process.exit(problems.length ? 1 : 0);
}

if (declared === 0 || drawn.items !== declared) {
  problems.push(
    `NOT MOUNTED: the answer declares ${declared} compose items and the page drew ${drawn.items}`
    + " — the route, the fetched JSON, or the item wrapper is not what this shot shows",
  );
  await finish();
}

// 1. A refusal means a node rejected what it was given and said which field was wrong.
for (const text of await page.locator("text=/was not drawn/").allTextContents()) problems.push(`REFUSED: ${text.trim()}`);

// 2. Nothing in the composition may occupy no space.
const zero = await page.locator(".ui-composed-b > *").evaluateAll((ns) =>
  ns.filter((n) => { const b = n.getBoundingClientRect(); return b.width === 0 || b.height === 0; })
    .map((n) => n.className || n.tagName));
for (const sel of zero) problems.push(`ZERO SIZE: ${sel}`);

// 3. A measured block under full-width chrome should have spanned.
const pane = (await page.locator(".ui-composed-b").first().boundingBox())?.width ?? 0;
const narrow = await page.locator(".ui-composed-b > :is(.ui-prose, .ui-marks, .ui-t.is-read)").evaluateAll(
  (ns, lane) => ns.filter((n) => n.getBoundingClientRect().width < lane - 200).map((n) => `${n.className} @${Math.round(n.getBoundingClientRect().width)}`), pane);
for (const n of narrow) problems.push(`NOT SPANNED (lane ${Math.round(pane)}): ${n}`);

// 4. Chrome is monochrome outside a quoted plate; the plate's own interior is light.
const outside = await page.locator(".ui-composed-b > *:not(.ui-plate) *").evaluateAll((ns) =>
  [...new Set(ns.map((e) => getComputedStyle(e).color))].filter(Boolean));
const tinted = outside.filter((c) => { const m = /(\d+), (\d+), (\d+)/.exec(c); if (!m) return false;
  const [r, g, b] = [+m[1]!, +m[2]!, +m[3]!];
  const spread = Math.max(r, g, b) - Math.min(r, g, b);
  return spread > 24 && !(g > r && g > b) && !(r > g && r > b && r > 150); });
for (const c of tinted) problems.push(`TINTED CHROME: ${c}`);

// 5. Per-node contracts, checked only for nodes the composition actually used.
if (counts.embed) {
  // The frame is sandboxed without allow-same-origin, so its document is unreadable
  // from here on purpose. What is provable is that a page was addressed and given room.
  const box = await page.locator(".ui-embed iframe").first().boundingBox().catch(() => null);
  if (!box || box.height < 120) problems.push(`EMBED: hosted frame is ${Math.round(box?.height ?? 0)}px tall`);
  const addr = await page.locator(".ui-embed-bar a").count();
  if (addr !== counts.embed) problems.push(`EMBED: ${counts.embed} hosted pages, ${addr} print their address`);
}
if (counts.chart) {
  if (!(await page.locator(".ui-chart svg").count())) problems.push("CHART: no svg drawn");
  const ticks = await page.locator(".ui-chart svg .role-axis-label text").count();
  if (ticks > 10) problems.push(`CHART: ${ticks} tick labels, over the ~8 the law allows`);
}
if (counts.flow) {
  const labels = await page.locator(".react-flow__edge-text").evaluateAll((ns) =>
    ns.map((n) => (n as SVGGraphicsElement).getBoundingClientRect()));
  let overlaps = 0;
  for (let i = 0; i < labels.length; i++) for (let j = i + 1; j < labels.length; j++) {
    const a = labels[i]!, b = labels[j]!;
    if (a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom) overlaps++;
  }
  if (overlaps) problems.push(`FLOW: ${overlaps} pairs of edge labels overlap`);
}
if (counts.image) {
  const w = await page.locator(".ui-image img").first().evaluate((e: HTMLImageElement) => e.naturalWidth);
  if (!w) problems.push("IMAGE: did not load (naturalWidth 0) — a broken figure is worse than none");
}
if (counts.code && !(await page.locator(".ui-code .hljs-keyword, .ui-code .hljs-string").count())) {
  problems.push("CODE: no syntax tokens, so it rendered as plain text");
}
if (counts.plate) {
  const bg = await page.locator(".ui-plate").first().evaluate((e) => getComputedStyle(e).backgroundColor);
  if (!/25[0-5]/.test(bg)) problems.push(`PLATE: background ${bg} is not the page's own light surface`);
}

// 6. The noise budget, measured. LAW.md rung 7 says a pane must state how much is
//    allowed "counted in the units the answer actually has" — and until now nothing read
//    the number off the rendered page, so the rung was a sentence and not a gate.
//
//    The ceilings are the author's, never this script's: they ship inside the answer as
//    `budget`, keyed by the node names the answer itself used. So the check needs no list
//    of what a node is or when to reach for one — every item carries its own `data-node`,
//    so check 0's census counts marks of each kind in the DOM and holds the page to its own
//    promise.
const budget = answer.budget ?? {};
if (Object.keys(budget).length === 0) {
  problems.push(`NO BUDGET: the answer declares no ceiling and drew ${JSON.stringify(drawn.byKind)}`);
}
for (const [unit, ceiling] of Object.entries(budget)) {
  const drew = drawn.byKind[unit] ?? 0;
  if (drew > ceiling) problems.push(`NOISE: ${drew} \`${unit}\` drawn against a declared ceiling of ${ceiling}`);
}

// 7. Anti-monotony guard. LAW.md section "what never belongs in an answer" says:
//    "a surface carrying one kind of thing is nearly always wrong... if every item on a
//    surface reads as a paragraph, the composition step did not happen." So the verifier
//    checks that at least two distinct node kinds are present unless the composition is
//    empty (caught in check 0). This doesn't mandate specific shapes — only that the author
//    chose more than one primitive. If an agent writes prose-only content, fail fast and
//    prompt them to use code/facts/path/marks instead of defaulting to paragraphs.
const distinctKinds = Object.keys(drawn.byKind).filter((k) => k !== "");
if (distinctKinds.length < 2 && declared > 0) {
  problems.push(`MONOTONY: ${declared} items but only ${distinctKinds.join(", ")} — compose with multiple primitives, not just paragraphs`);
}

await finish();

// THE VERIFIER THAT LOOKS AT WHAT A READER WILL LOOK AT.
//
// `verify-answer.ts` checks one composition on one fixed route, against contracts the
// composition declares about itself. This checks the STATION — whatever it is actually
// showing, composed or catalog-drawn — and it checks the things that are wrong on a
// surface no matter what that surface is about.
//
// It exists because of a real miss. A fold shipped reading `7 blocks · 0 wires` while a
// card inside it printed `→ came from K#2`, and two other cards showed their body as raw
// markdown — `The seeded root for the **local web app preview**. - Lists… a [link](…)` —
// clipped at both edges of the window. Every one of those is visible in one screenshot and
// none of them was catchable by a contract the answer declared, because the answer did not
// draw that surface: the catalog did.
//
// So the checks here are about the RENDER, not the payload, and none of them names a node,
// a visual or a class this repo ships. A check that knew `knowledge-card` would pass the
// next surface that breaks the same way.
//
//   node scripts/verify-canvas.ts                     # the station, to /tmp/canvas.png
//   node scripts/verify-canvas.ts /tmp/shot.png       # somewhere else
//   VERIFY_URL=… node scripts/verify-canvas.ts        # a different app
//
// Exits non-zero when it has findings, so a caller can gate on it. The findings are also
// written as JSON beside the shot, which is what the judging pass reads.
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { chromium } from "@playwright/test";

const url = process.env.VERIFY_URL ?? "http://127.0.0.1:8799/app/";
const shot = process.argv[2] ?? "/tmp/canvas.png";

/** One thing wrong with the render, in the words a reader would use. */
type Finding = { readonly check: string; readonly what: string; readonly where: string };

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1680, height: 1050 }, deviceScaleFactor: 2 });
const findings: Finding[] = [];
page.on("pageerror", (error) => findings.push({ check: "page-error", what: error.message, where: "window" }));
page.on("console", (message) => {
  if (message.type() === "error" && !/favicon|billing/.test(message.text())) {
    findings.push({ check: "console-error", what: message.text().slice(0, 200), where: "window" });
  }
});

await page.goto(url, { waitUntil: "networkidle" });
// The station fetches its state, its visuals and each visual's own data in turn, so a
// fixed wait is the honest way to let the last of those land. A selector would have to
// name a surface, and naming one is the thing this file refuses to do.
await page.waitForTimeout(8000);

const seen = await page.evaluate(() => {
  /** The text this element paints ITSELF — its own text nodes, not its children's.
   *
   *  Comparing an element's `textContent` with its children's is not the same question:
   *  a container with several children concatenates them into a string that matches no
   *  single child, so it looks like the owner of every word under it. That mistake made
   *  the first run of this file report the whole app shell as raw markdown. */
  const own = (el: Element) =>
    [...el.childNodes]
      .filter((node) => node.nodeType === 3)
      .map((node) => node.textContent ?? "")
      .join(" ")
      .trim();
  const owns = (el: Element) => own(el).length > 1;

  /** The box an element actually paints into.
   *
   *  `getBoundingClientRect` reports 0x0 for an inline element in some layouts, and the
   *  text this file most needs to read — a claim, a line, a footer — is in inline spans.
   *  A width floor on that rect silently emptied the sample: the first version of this
   *  check looked at 1,489 elements and not one of them was a word inside a card.
   *  `getClientRects` returns one rect per line box, so their union is the real ink. */
  const inked = (el: Element) => {
    const rects = [...el.getClientRects()];
    if (rects.length === 0) return null;
    const left = Math.min(...rects.map((r) => r.left));
    const right = Math.max(...rects.map((r) => r.right));
    const top = Math.min(...rects.map((r) => r.top));
    const bottom = Math.max(...rects.map((r) => r.bottom));
    return { left, right, top, bottom, width: right - left, height: bottom - top };
  };

  // Shown to a sighted reader. `checkVisibility` answers for display, visibility and
  // content-visibility at once; the size floor then rules out the visually-hidden label,
  // a 1px clipped box that exists for a screen reader and is not a render at all.
  const visible = (el: Element) => {
    if ("checkVisibility" in el && !(el as HTMLElement).checkVisibility({ contentVisibilityAuto: true, opacityProperty: true, visibilityProperty: true })) return false;
    const box = inked(el);
    if (!box || box.width < 8 || box.height < 8) return false;
    return Number(getComputedStyle(el).opacity) > 0.05;
  };

  /** Is this element quoting something verbatim?
   *
   *  A terminal pane, a code block or a command argument shows its content as it was
   *  written, and markdown inside one is the SOURCE being quoted — correct, not a
   *  renderer that failed to parse. Reporting those made the first useful run of this
   *  file nine-tenths noise, all of it the chat column quoting `DESIGN.md` at itself. */
  const verbatim = (el: Element) => {
    for (let at: Element | null = el; at; at = at.parentElement) {
      if (/^(pre|code|kbd|samp|textarea)$/.test(at.tagName.toLowerCase())) return true;
      const style = getComputedStyle(at);
      if (/^(pre|pre-wrap|break-spaces)$/.test(style.whiteSpace)) return true;
      // Monospace is this design's machine register: a command, a path, a stored value,
      // shown as it is held. Markdown characters inside one are the thing being quoted.
      if (/mono/i.test(style.fontFamily)) return true;
    }
    return false;
  };

  const name = (el: Element) => {
    const cls = String((el as HTMLElement).className ?? "").split(/\s+/).filter(Boolean).slice(0, 2).join(".");
    return `${el.tagName.toLowerCase()}${cls ? `.${cls}` : ""}`;
  };

  const all = [...document.querySelectorAll("body *")].filter(visible);

  // 1. CLIPPED SIDEWAYS. A box whose own content is WIDER than the box shows the reader
  //    a sentence with its start or its end cut off — this is what turned "Across views"
  //    into "ross views" on a shipped surface.
  //
  //    Width only, on purpose. Vertical overflow is a pattern this app uses deliberately
  //    and often: a collapsed tool row is one line of a twelve-line command, with a `▸`
  //    that opens it. Flagging height reported every one of those and buried the real
  //    finding, so the check asks the question a reader asks — is a word cut in half? —
  //    and not the question the box model happens to answer.
  //    And it measures the clip the way it actually happens: an element's ink extending
  //    past the edge of the nearest ancestor that CLIPS it. `scrollWidth > clientWidth`
  //    only answers for an element that is its own scroll box, which no inline span is —
  //    "Across views" was not a span overflowing itself, it was a card sitting past the
  //    window's edge, and the box-model version of this check could never have seen it.
  const clipper = (el: Element) => {
    for (let at = el.parentElement; at; at = at.parentElement) {
      const style = getComputedStyle(at);
      if (/hidden|clip|auto|scroll/.test(style.overflowX)) return at;
    }
    return null;
  };
  const clipped = all
    .filter(owns)
    .filter((el) => getComputedStyle(el).textOverflow !== "ellipsis") // an ellipsis is a decision, not a clip
    .map((el) => {
      const host = clipper(el);
      const box = inked(el);
      if (!host || !box) return null;
      const edge = host.getBoundingClientRect();
      const over = Math.max(edge.left - box.left, box.right - edge.right);
      return over > 2 ? { el, over, host } : null;
    })
    .filter((row) => row !== null)
    .slice(0, 12)
    .map((row) => ({
      where: name(row!.el),
      what: `cut off by ${Math.round(row!.over)}px at the edge of ${name(row!.host)}`,
      text: own(row!.el).slice(0, 70),
    }));

  // 2. RAW MARKUP. Markdown that reached the screen as characters is a renderer that was
  //    handed a body it does not parse. A reader sees `**bold**` and asterisks.
  const rawish = /\*\*\S|\]\(https?:|^\s*[-*]\s+\S.*\n\s*[-*]\s+|```/m;
  const raw = all
    .filter(owns)
    .filter((el) => !verbatim(el))
    .filter((el) => rawish.test(own(el)))
    .slice(0, 8)
    .map((el) => ({ where: name(el), what: "markdown shown as characters", text: own(el).slice(0, 90) }));

  // 3. NOT A CHECK: SELF-CONTRADICTION.
  //
  //    A surface that counts itself and gets it wrong — `7 blocks · 0 wires` printed
  //    under a card reading `came from K#2` — is the loudest tell a render is broken,
  //    and this file deliberately does NOT check it.
  //
  //    The version that was here read `<n> <word>` out of a tally line and compared it
  //    with `[class*=word]`. On the very fold it was written for it reported "7 block
  //    while the page draws 33", having counted `ui-block-slot` and `ui-block-claim`,
  //    and "0 wire while the page draws 1", having counted the empty SVG that holds the
  //    wires. Both numbers were wrong and both looked authoritative. There is no sound
  //    way to get from a noun a surface prints to the class a renderer happens to use
  //    for it, and a check that invents numbers costs more than the bug it looks for —
  //    it teaches whoever reads the report to stop believing the rest of it.
  //
  //    This is what the judging pass is for: a reader looking at the shot sees that a
  //    card says it came from another one and that nothing joins them.

  // 4. EMPTY SURFACE. A mounted container with a real size and nothing inside it is a
  //    pane that failed to draw, and it reads to a user as a bug even when the data
  //    behind it is genuinely empty — an empty state is words, not a void.
  const hollow = all
    .filter((el) => {
      const box = el.getBoundingClientRect();
      if (box.width < 220 || box.height < 160) return false;
      // A canvas, an svg or an image IS the drawing — it has no children and no words
      // by construction, and counting it as empty measures the element rather than
      // what it paints.
      if (/^(canvas|svg|img|video|iframe)$/.test(el.tagName.toLowerCase())) return false;
      // An overlay with nothing in it is a layer waiting for content — the red-box
      // drawing surface before a box is drawn — not a pane that failed to render.
      const style = getComputedStyle(el);
      if (el.children.length === 0 && /absolute|fixed/.test(style.position)) return false;
      if (style.pointerEvents === "none" && el.children.length === 0) return false;
      return (el.textContent ?? "").trim() === "" && el.querySelectorAll("svg, canvas, img, video").length === 0;
    })
    .slice(0, 6)
    .map((el) => ({ where: name(el), what: "a sized container with no words and no drawing", text: "" }));

  return { clipped, raw, hollow, nodes: all.length };
});

for (const row of seen.clipped) findings.push({ check: "clipped", what: `${row.what} — "${row.text}"`, where: row.where });
for (const row of seen.raw) findings.push({ check: "raw-markup", what: `${row.what} — "${row.text}"`, where: row.where });
for (const row of seen.hollow) findings.push({ check: "empty", what: row.what, where: row.where });

// 5. THE MOUNT CENSUS. Everything above asks "is what is drawn wrong?" and a page that
//    drew the WRONG THING answers no to all of it. This asks the other question: is the
//    composition on disk the composition on the screen?
//
//    It is here because the same mistake happened three times in one session. A node was
//    added to `blocks-latest.json`, the stage was showing a different visual, and every
//    check came back clean — so "clean" was reported for a surface that never mounted the
//    work. The third time, the file had been overwritten by someone else's run and the
//    node being looked for did not exist at all. None of that is visible in a screenshot,
//    and all of it is obvious from two numbers.
const composed = (() => {
  try {
    return JSON.parse(readFileSync(join(import.meta.dirname, "..", "dist", "blocks-latest.json"), "utf8")) as {
      readonly title?: string;
      readonly compose?: readonly { readonly node: string }[];
    };
  } catch {
    return null;
  }
})();

if (composed?.compose) {
  const declared = composed.compose.map((item) => item.node);
  const drawn = await page.evaluate(() =>
    [...document.querySelectorAll(".ui-composed-b > [data-node]")].map((el) => el.getAttribute("data-node") ?? ""));
  if (drawn.length === 0) {
    findings.push({
      check: "not-mounted",
      where: "stage",
      what: `the composition on disk ("${composed.title ?? "untitled"}", ${declared.length} items) is not on the stage — `
        + "the canvas is showing something else, so nothing below was verified against it",
    });
  } else {
    // Counted per kind at the top level: a kind declared twice and drawn once is a
    // silent drop, which is exactly how a figure with the wrong tag key disappears.
    const tally = (list: readonly string[]) =>
      list.reduce<Record<string, number>>((into, kind) => ({ ...into, [kind]: (into[kind] ?? 0) + 1 }), {});
    const want = tally(declared);
    const got = tally(drawn);
    for (const kind of Object.keys(want)) {
      if ((got[kind] ?? 0) < want[kind]!) {
        findings.push({
          check: "dropped",
          where: `node "${kind}"`,
          what: `declared ${want[kind]} and drew ${got[kind] ?? 0} — the item was lost without an error`,
        });
      }
    }
  }
}

await page.screenshot({ path: shot, fullPage: false });
await browser.close();

const report = `${shot.replace(/\.png$/, "")}.findings.json`;
writeFileSync(report, JSON.stringify({ url, shot, nodes: seen.nodes, findings }, null, 2));

process.stdout.write(`verify-canvas: ${seen.nodes} elements · ${findings.length} finding${findings.length === 1 ? "" : "s"}\n`);
for (const finding of findings) process.stdout.write(`  [${finding.check}] ${finding.where}: ${finding.what}\n`);
process.stdout.write(`shot  ${shot}\nreport ${report}\n`);
if (findings.length > 0) {
  process.stdout.write("\nThese are machine findings. LOOK AT THE SHOT YOURSELF before you answer:\n");
  process.stdout.write("a clean report on an ugly page is still an ugly page.\n");
}
process.exit(findings.length === 0 ? 0 : 1);

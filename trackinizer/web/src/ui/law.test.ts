// THE DESIGN LAW IS ENFORCED, NOT REQUESTED.
//
// LAW.md tells the agent what a good answer looks like. A rule stated in prose and
// checked by nobody is a rule a generated page breaks on its first draft, so every
// law that is machine-checkable is checked here, against the stylesheets the answer
// surfaces actually ship. The uncheckable half — is this prose any good — is defended
// structurally instead: the agent fills a type and the primitives decide where ink
// goes, so it has no way to ask for a gradient.
//
// A second check reads LAW.md itself — and what it checks is that the law stays a
// METHOD. How a pane looks is decided in the stylesheets above and stated there once;
// the moment the law also states a size, an ink or a node's name, two sources describe
// one design and they will disagree. So this file fails when the law GROWS a setting,
// not when it loses one.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test } from "vitest";
import { CATALOG } from "./catalog.ts";

/** The stylesheets of the answer surfaces: the design library, the shapes assembled
 *  out of it, and the two folds that draw blocks. The app's own chrome — a drawer, the
 *  canvas frame — is not an answer and keeps its own rules. */
/**
 * Every stylesheet an answer can actually draw with, found by walking the real
 * import graph from the two places an answer is mounted — never hand-listed.
 *
 * A hand-written list is the bug this replaces: `compose.css`, `flow.css` and
 * `code.css` all shipped for most of a session before this file knew they existed,
 * because adding a sheet to the law meant remembering to add it to a second place too.
 * A node reviewing this very gap called it out by name — "the eight sheets named are a
 * hand-written list" — and it recurred within the hour, which is the proof a human
 * remembering to update a list is not a fix. Walking `import` statements is: a CSS
 * import reached from an entry point is, by construction, a sheet an answer can draw
 * with, and one that stops being reachable drops out on its own.
 */
const ENTRY_POINTS = ["blocks/index.tsx", "visuals/Knowledge.tsx"] as const;

const src = join(import.meta.dirname, "..");

function discoverSheets(): readonly string[] {
  const found = new Set<string>();
  const visited = new Set<string>();
  const walk = (relPath: string) => {
    if (visited.has(relPath)) return;
    visited.add(relPath);
    const abs = join(src, relPath);
    let text: string;
    try {
      text = readFileSync(abs, "utf8");
    } catch {
      return; // A bare import with no extension resolved wrong; not this walk's job to fix.
    }
    const dir = relPath.split("/").slice(0, -1).join("/");
    for (const match of text.matchAll(/from\s+["']([^"']+)["']|import\s+["']([^"']+)["']/g)) {
      // A bare `import "./foo.css"` (every CSS import in this codebase) matches the
      // second alternative, so group 1 is undefined and the value sits in group 2;
      // destructuring only group 1 silently dropped every one — found stayed empty
      // while the walk otherwise ran correctly through every .tsx it did find.
      const target = match[1] ?? match[2];
      if (!target?.startsWith(".")) continue;
      const resolved = normalise(dir ? `${dir}/${target}` : target);
      if (resolved.endsWith(".css")) {
        found.add(resolved);
      } else if (!resolved.includes(".")) {
        // An extensionless specifier is a .ts or .tsx module; try both so the walk
        // continues through it.
        for (const ext of [".tsx", ".ts"]) if (visited.has(resolved + ext) || fileExists(resolved + ext)) { walk(resolved + ext); break; }
      }
    }
  };
  for (const entry of ENTRY_POINTS) walk(entry);
  return [...found].sort();
}

/** Collapse `a/b/../c` to `a/c`, the way a relative import is resolved. */
function normalise(path: string): string {
  const parts: string[] = [];
  for (const part of path.split("/")) {
    if (part === "." || part === "") continue;
    if (part === "..") parts.pop();
    else parts.push(part);
  }
  return parts.join("/");
}

function fileExists(relPath: string): boolean {
  try {
    readFileSync(join(src, relPath));
    return true;
  } catch {
    return false;
  }
}

const SHEETS = discoverSheets();
const sheets = SHEETS.map((path) => ({ path, text: readFileSync(join(src, path), "utf8") }));

test("the walk that finds the answer sheets reaches at least the ones known by name", () => {
  // A sentinel, not a list to maintain: if this ever shrinks below the known set, the
  // walk itself broke, which is worse than the hand-list it replaced.
  const known = ["ui/sections.css", "ui/block.css", "ui/table.css", "visuals/Knowledge.css", "ui/compose.css", "ui/flow.css", "ui/code.css"];
  for (const path of known) expect(SHEETS, `discoverSheets() did not reach ${path}`).toContain(path);
});

/** Every line of the answer sheets, as `path:line` and the line itself. */
const lines = sheets.flatMap(({ path, text }) =>
  text.split("\n").map((line, index) => ({ at: `${path}:${index + 1}`, line })),
);

/** The lines a law is allowed to ignore: a comment states the rule, it does not break it. */
const code = lines.filter(({ line }) => !/^\s*(\/\*|\*|\/\/)/.test(line));

test("elevation on an answer surface is a hairline and a step of surface, never a shadow", () => {
  // A glow ring and a drop shadow are what a generated page reaches for to say
  // "card". The design says it with one hairline and `--surface-hover`, which is the
  // step the theme already holds in both themes and every skin.
  // `box-shadow: none` is a removal of a library default, not a shadow drawn — a sheet
  // stripping React Flow's own box-shadow is exactly the law being followed. Matched
  // on the declaration's own value, not a lookahead beside `\s*`: a zero-width `\s*`
  // let an earlier version of this check match `box-shadow: none` as a violation,
  // because the lookahead landed on the space before "none" rather than on "none".
  const shadows = code.filter(({ line }) => {
    const value = /box-shadow:\s*([^;}]+)/.exec(line)?.[1]?.trim();
    return value !== undefined && value !== "none" && !/^none\s*!important$/.test(value);
  });
  expect(shadows.map(({ at }) => at)).toEqual([]);
});

test("a colour on an answer surface is a token, so it is right in all eight skins", () => {
  // A literal hex is a colour authored for the dark default and wrong in the light
  // theme and the six skins, where nothing recomputes it. `.ui-plate` is the one named
  // exception: a quoted page's own white-and-black is a fixed fact about the document
  // being reproduced, not an author's ink that should follow the reader's skin — the
  // same reasoning that already exempts the plate's title and byline from the
  // left-ragged rule below. Scoped to the rule itself, not to the whole file.
  const inPlateRule = (at: string) => {
    const text = sheets.find((sheet) => sheet.path === at.split(":")[0])!.text;
    const lineNo = Number(at.split(":")[1]) - 1;
    const upTo = text.split("\n").slice(0, lineNo + 1).join("\n");
    const openSelector = /\.ui-plate\s*\{[^}]*$/.test(upTo);
    return openSelector;
  };
  const literal = code.filter(({ line, at }) => /#[0-9a-fA-F]{3,8}\b/.test(line) && !inPlateRule(at));
  expect(literal.map(({ at, line }) => `${at} ${line.trim()}`)).toEqual([]);
});

test("colour lands on a glyph or a data mark, and never on a word", () => {
  // This is the app's own rule and lives here, in the CSS, stated once: hue is an
  // encoding, and the law no longer repeats it (a setting in two places is a setting
  // that drifts). So the thing to check is WHICH PROPERTY carries the tint:
  //
  //   `color:` paints text. A tinted text run makes a sentence that needs colour to be
  //   understood, so it must sit on the verdict glyph itself (`.ui-mark.is-fit b`).
  //
  //   `background:` paints a mark. A bar whose length and sign ARE the datum is the
  //   encoding the law allows — the same licence a chart's series already has.
  //
  // This was widened once, deliberately, when a valence bar (`because.css`) was caught
  // by the old check. The old rule demanded every tint sit on a `b`, which was stricter
  // than the law it was meant to enforce. Widening by PROPERTY keeps it principled: the
  // alternative — exempting a file by name — is the exclusion-list mistake that let a
  // real pill-badge violation through earlier in this codebase's life.
  const tinted = code.filter(({ line }) => /var\(--l-(evidence|against)\)/.test(line));
  expect(tinted.length, "the evidence tints are still used").toBeGreaterThan(0);
  const onText = tinted.filter(({ line }) => /(^|[^-\w])color:\s*var\(--l-/.test(line));
  expect(onText.length, "the tints are still used on a verdict glyph somewhere").toBeGreaterThan(0);
  for (const { at, line } of onText) {
    const selector = line.split("{")[0]!.trim();
    expect(/(^|\s)b(\.[\w-]+)?$/.test(selector), `${at} tints a text run: ${selector}`).toBe(true);
  }
});

test("a selected or active state borders in --text, which is emphasis, never --ink-machine, which locates", () => {
  const borders = code.filter(({ line }) => /\.(is-on|selected)\b.*(border|outline)[\w-]*:\s*[^;]*var\(--/.test(line));
  const wrong = borders.filter(({ line }) => /var\(--(ink-machine|ink-tertiary|muted)\)/.test(line));
  expect(wrong.map(({ at, line }) => `${at} ${line.trim()}`)).toEqual([]);
});

test("text on an answer surface never takes an ink that fails WCAG AA on it", () => {
  // styles.test.ts proves --ink-tertiary and --accent sit under 4.5:1 on every
  // surface. This keeps them off the surfaces a scientist reads.
  //
  // The property has to be `color` and not merely end in it: the unanchored pattern
  // this replaces also matched `border-color: var(--accent)`, which is a hairline and
  // not an ink — nothing reads a border, so a contrast ratio says nothing about it. It
  // went unnoticed only because the two rules that draw a focused edge in the accent
  // were still written as literal hex, and so were invisible to a check that looks for
  // tokens. Tokenising them is what surfaced it.
  const failing = code.filter(({ line }) => /(?<![-\w])color:\s*var\(--(ink-tertiary|accent)\)/.test(line));
  expect(failing.map(({ at, line }) => `${at} ${line.trim()}`)).toEqual([]);
});

test("a corner is the theme's radius or the card's own, and nothing invented past it", () => {
  // A large radius is the other half of the generated-card look. 10px is the block's,
  // 8px the chip's, 4px a token inside a row; anything above reads as a pill.
  // `50%` is exempt: a dot or an avatar draws a true circle with it, which is the
  // correct value for that shape at any element size — the rule is about an invented
  // PX figure, not about whether a round mark exists at all.
  const bad = code.flatMap(({ at, line }) =>
    [...line.matchAll(/border-radius:\s*([^;}]+)/g)]
      .filter(([, value]) => !value!.includes("var(") && value!.trim() !== "50%" && Number.parseFloat(value!) > 10)
      .map(([, value]) => `${at} ${value!.trim()}`),
  );
  expect(bad).toEqual([]);
});

test("our own chrome is left-ragged: no answer sheet centres a column", () => {
  // A centred column has no left edge for a reader to scan. The exemption this test
  // used to carry was the paper template's plate, which quoted a printed page and so
  // kept its publisher's centring; the template is deleted, so the rule is absolute
  // again. A reproduced document still has a node in the catalog — `plate` — and if
  // that sheet ever needs the exemption back, it comes with a named file, not a
  // string prefix over the whole census.
  const centred = code.filter(({ line }) => /text-align:\s*center/.test(line));
  expect(centred.map(({ at }) => at)).toEqual([]);
});

/** The law as the agent reads it. */
const lawPath = join(src, "..", "..", "trax", "docs", "skills", "trax", "LAW.md");
const law = readFileSync(lawPath, "utf8");

test("the law states the method the stylesheets cannot enforce", () => {
  // The rules above are the app's own look and are proved against its CSS. What is left
  // to prove here is that the law still carries the reasoning, which no stylesheet can:
  // how to order an answer, and that the graph is ambient. Matched with the law's
  // wrapping collapsed, so re-flowing a paragraph is not a failure and deleting a rule is.
  const prose = law.replace(/\s+/g, " ");
  for (const rule of [
    "A researcher never says",
    "what each would settle, not what each costs",
    "Nothing is invented to fill a slot",
    "an author cannot see their own page",
    "What is checkable is checked",
  ]) {
    expect(prose, `LAW.md no longer states: ${rule}`).toContain(rule);
  }
});

test("the law names no catalog node — the vocabulary is the brief's, and it is generated", () => {
  // Each entry already says what it renders and when to pick it, in the code that
  // registers it. The law repeating those names is how a second, hand-maintained copy
  // of the catalog comes to exist and quietly disagrees with the first.
  for (const { node } of CATALOG) {
    expect(law, `LAW.md names the catalog entry ${node}`).not.toContain(node);
  }
});

test("the law holds no size, colour or token: the app's stylesheets are the only source of the look", () => {
  const hits = law.match(/\d[\d,.]*(?:px|vw|vh|rem|em)\b|#[0-9a-fA-F]{6}\b|var\(--/g) ?? [];
  expect(hits, `LAW.md states a design setting: ${hits.join(" · ")}`).toEqual([]);
});

test("the loop injects no capability list of its own — the brief is where what exists is stated", () => {
  // Derived from the catalog, not hand-listed: a preamble that names the libraries is a
  // second copy of them, and it is the copy that goes stale when a node is added.
  const loop = readFileSync(join(src, "..", "scripts", "ask.ts"), "utf8");
  const libraries = [...new Set(CATALOG.map((n) => n.library))]
    .filter((l) => !l.startsWith("ui/"))
    .flatMap((l) => l.split(/\s*\+\s*/));
  for (const lib of libraries) {
    expect(loop, `scripts/ask.ts hand-types ${lib}`).not.toContain(lib);
  }
  expect(loop, "scripts/ask.ts must send the agent to the generated brief").toContain("DESIGN.md");
});

test("the law is read before every answer, so it has a byte ceiling of its own", () => {
  // DESIGN.md carries what exists and is generated from the code; this carries how to
  // think and is written by hand. They are read together and their ceilings are
  // separate, so a longer brief can never evict a rule of the method.
  //
  // 13 KiB, DOWN from 16. Five sections left the file: `## scale`, `## the two faces`,
  // `## ink and surface`, `## the section rule` and `## what is never drawn`'s styling
  // half. Each was a copy — the sizes, faces, inks, radii and centring they stated are
  // the app's, decided once in its stylesheets and held by the tests above, which is
  // precisely why a ceiling that only ever rises is not a ceiling. Two things arrived in
  // their place: `## what is not yours to decide`, naming who owns what, and `## what
  // never belongs in an answer`, the short list of bans that are CONTENT — emoji, an
  // invented icon, a pill badge, a hero opening, a rounded figure — and so can be
  // written by an agent into any stylesheet's blind spot.
  //
  // The method itself — the brief, the six steps, reading a question's shape, what earns
  // a form, the four seats — was not touched to fit this number. If a rule has to go for
  // the ceiling's sake, the ceiling is the thing that is wrong.
  //
  // 19.5 KiB, and the raise is recorded rather than implied because it is two things at
  // once. The file measured 16,528 against a 13,312 ceiling when this was next read: it
  // had already been written past the limit and the number never moved with it, so this
  // check had been failing for some time and is only now being answered. That gap is the
  // argument for a ceiling that is maintained, not for keeping a stale one.
  //
  // The 2.2 KiB on top is two sections — `## a relation is drawn, not described` and
  // `## a territory is part of an answer, not the answer`. They are here because answer
  // after answer came out as a column of paragraphs with a number in each, while the
  // things that draw relations sat in the brief unread. Every rule the law had was about
  // whether a form is EARNED; none said that a relation left in sentences is a finding
  // the reader has to assemble themselves, or that handing a whole surface to a
  // neighbourhood gives back the distillation that was the job. Neither names a node —
  // what to reach for is the brief's to say, and the law stays method.
  expect(Buffer.byteLength(law, "utf8")).toBeLessThanOrEqual(19968);
});

// ── The ink and face guards, ported out of the review template's test when that
// template was deleted. They never asserted the template: they assert the law on the
// sheets that survive it, which is the reason they are here and not in a file named
// after one surface.

test("hue lives on a mark's glyph and a block's slot, and no accent or violet reaches the library", () => {
  for (const sheet of ["sections.css", "block.css"]) {
    const css = readFileSync(join(import.meta.dirname, sheet), "utf8");
    expect([...css.matchAll(/--(accent|violet|amber|group-\d)/g)].map((f) => f[0])).toEqual([]);
    for (const line of css.split("\n")) {
      if (!/color:\s*var\(--l-(evidence|against)\)/.test(line)) continue;
      // The declaration's own selector is the line above it in this sheet's shape.
      expect(line).toMatch(/^\.ui-mark\.is-\w+ b \{/);
    }
  }
  // A surface may wear its own hue, but only on a slot and on the selection's edge —
  // the queue's box and the one place open under it — and only through the token the
  // block reads. Nothing else is allowed a colour, so a hue can never mark a word, a
  // wire or a verdict.
  const fr = readFileSync(join(import.meta.dirname, "frame.css"), "utf8");
  const hued = [...fr.matchAll(/^\.([-\w]+(?: \.[-\w]+)?) \{[^}]*var\(--group-\d\)/gm)].map((m) => m[1]);
  expect(hued).toEqual(["ui-queue .ui-block"]);
  expect(fr).toMatch(/--block-slot-ink: var\(--group-4\)/);
  expect(fr).toMatch(/\[aria-pressed="true"\] \{[^}]*border-color: var\(--group-4\)/);
});

test("what is read takes the prose face in white; what locates takes mono in machine ink", () => {
  const css = readFileSync(join(import.meta.dirname, "sections.css"), "utf8");
  expect(css).toMatch(/\.ui-prose \{[^}]*color: var\(--text\)/);
  expect(css).toMatch(/\.ui-mark \{[^}]*color: var\(--text\)/);
  expect(css).toMatch(/\.ui-prose \{[^}]*font: 400 [\d.]+px\/[\d.]+ var\(--sans\)/);
  // A section's label names the section, so it is read in white; only its measure and
  // its hairline stay grey. No hue may reach a word, which is why it is not violet.
  expect(css).toMatch(/\.ui-sec b \{ font-weight: 500; color: var\(--text\); \}/);
  expect(css).toMatch(/\.ui-sec em \{[^}]*color: var\(--ink-machine\)/);
  expect(css).toMatch(/\.ui-paths span \{[^}]*color: var\(--ink-machine\)/);
  expect(css).toMatch(/\.ui-paths span \{[^}]*font: 400 [\d.]+px\/[\d.]+ var\(--mono\)/);
  // A code token is a thing in the file, so it takes the machine face at the reading
  // ink: the sentence around it is the argument, this is its address.
  expect(css).toMatch(/\.ui-tok \{[^}]*font-family: var\(--mono\)/);
  expect(css).toMatch(/\.ui-tok \{[^}]*color: var\(--text\)/);
  // A chip indexes an answer rather than being read as prose, so the frame sets the whole
  // strip in the machine's face and lets weight say which line answers.
  const blk = readFileSync(join(import.meta.dirname, "block.css"), "utf8");
  expect(blk).toMatch(/\.is-row \.ui-block-claim \{ font: 500 [\d.]+px\/[\d.]+ var\(--mono\)/);
  expect(blk).toMatch(/\.is-row \.ui-block-line \{ font: 400 [\d.]+px\/[\d.]+ var\(--mono\)/);
  expect(blk).toMatch(/\.is-row \.ui-block-line \{[^}]*color: var\(--text\)/);
  expect(blk).toMatch(/\.is-row \.ui-block-line \.is-said \{ font-weight: 500; \}/);
  expect(blk).toMatch(/\.is-row \.ui-block-foot \{[^}]*color: var\(--ink-machine\)/);
  expect(blk).toMatch(/\.is-card \.ui-block-foot \{[^}]*color: var\(--text\)/);
  // Nothing on either sheet is allowed the tinted step-back greys.
  for (const sheet of [css, blk]) {
    expect([...sheet.matchAll(/var\(--(ink-muted|ink-tertiary)\)/g)].map((f) => f[0])).toEqual([]);
  }
});

test("the design's panes take the design's two faces, and no sheet restates them", () => {
  const root = readFileSync(join(import.meta.dirname, "../styles.css"), "utf8");
  const panes = [...root.matchAll(/^:root:not\(\[data-skin\]\) :is\(([^)]*)\)/gm)].map((m) => m[1]);
  // A panel opens from a portal on the body, so it inherits no face from the pane it
  // was opened from; without its own name here its title renders in the system stack
  // over a pane in Inter, which is two faces for one answer.
  expect(panes).toEqual([".chat-panel, .ui-panel, .ui-composed"]);
  const rule = root.match(/:root:not\(\[data-skin\]\) :is\([^)]*\) \{([^}]*)\}/)?.[1] ?? "";
  // The two tokens alone are not enough: font-family is computed where it is declared,
  // so a descendant that names no family inherits the stack the body resolved.
  expect(rule).toMatch(/font-family: var\(--sans\);/);
  expect(rule).toMatch(/--mono: "JetBrains Mono Variable"/);
  expect(rule).toMatch(/--sans: "Inter Variable"/);
  for (const sheet of ["canvas.css", "sections.css", "block.css", "compose.css"]) {
    const css = readFileSync(
      join(import.meta.dirname, sheet === "canvas.css" ? "../visuals" : ".", sheet),
      "utf8",
    );
    expect([...css.matchAll(/--(?:mono|sans):/g)]).toEqual([]);
  }
});

test("the block owns its anatomy and each surface owns only the box it rests on", () => {
  const blk = readFileSync(join(import.meta.dirname, "block.css"), "utf8");
  // The parts and the two densities are stated once, here.
  for (const part of ["slot", "claim", "line", "foot"]) {
    expect(blk).toContain(`.ui-block-${part} {`);
  }
  expect(blk).toMatch(/\.is-card \.ui-block-claim \{/);
  expect(blk).toMatch(/\.is-row \.ui-block-claim \{/);
  // No surface restates a part: the fold's card is lifted and the register's chip sits
  // flat, and that difference is a box, not a type scale.
  for (const sheet of ["Knowledge.css", "compose.css"]) {
    const css = readFileSync(
      join(import.meta.dirname, sheet === "Knowledge.css" ? "../visuals" : ".", sheet),
      "utf8",
    );
    expect([...css.matchAll(/\.ui-block-(slot|claim|line|foot)/g)].map((f) => f[0])).toEqual([]);
  }
});

test("no answer sheet owns a colour literal: white, machine grey, one hue", () => {
  for (const sheet of ["frame.css", "sections.css", "block.css", "compose.css", "nodes.css"]) {
    const css = readFileSync(join(import.meta.dirname, sheet), "utf8");
    const inks = [...css.matchAll(/#[0-9a-fA-F]{3,8}\b|rgba?\(/g)].map((f) => f[0]);
    expect(inks, `${sheet} names its own colour`).toEqual([]);
    const tiers = [...css.matchAll(/color: var\(--([a-z0-9-]+)\)/g)]
      .map((f) => f[1]);
    expect([...new Set(tiers)].filter((t) => ![
      "text", "ink-machine", "group-4", "line", "line-strong", "surface", "surface-raised",
      "surface-hover", "bg",
      // The one pair a design sheet may step outside the three inks for: a diff reads
      // green in and pink out, which is a fact about the change and not a level.
      "l-evidence", "l-against",
    ].includes(t)), `${sheet} steps outside the three inks`).toEqual([]);
  }
});

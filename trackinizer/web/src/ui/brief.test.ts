// THE BRIEF IS A PROJECTION, SO IT IS TESTED AS ONE.
//
// The agent's design brief is generated from the answer types, and the only way it
// stays true is a check that re-derives it and compares: a hand-edit to the shipped
// file, or a field renamed in the source, fails here. Two more checks read the source
// files themselves, because a projection can be faithfully derived and still be
// useless: one walks every type the brief PRINTS as a payload and demands the brief go
// on to define it (`Item` named 24 arms for months while five of their payloads and the
// rule on each were nowhere in the file, and the byte-for-byte check passed the whole
// time), and one demands every arm the agent chooses between carry a reason to choose
// it. Each of those would otherwise cost a real model call, because an agent given a
// name with no rule behind it writes a paragraph.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test } from "vitest";
import { brief, briefPath, choices, defined, printedTypes, SOURCES } from "../../scripts/design-brief.ts";

/** The ceiling is what the brief may cost the model that reads it. It is ONE read per
 *  answer-writing session, not a per-answer injection — SKILL.md sends the agent to read
 *  this file before it writes a flow, and nothing in `trax/` puts it in a prompt — and the
 *  four templates measure 10,964 bytes today, of which the paper's three shapes cost 2,769
 *  (25 fields, each with the rule on it). 12 KiB is the next step that still says no: it
 *  leaves one more template's worth of room at the paper's measured size. The 8 KiB this
 *  file carried until the paper arrived was reasoned from a channel that does not exist. */
/** 19.5 KiB → this raise is for `drawing` and `ranked`, and it is the largest
 *  single step this file has taken, so the reason is written out rather than implied.
 *
 *  Both are nodes an agent cannot reach for unless the brief names them, and both
 *  exist because a real gap was hit:
 *   - `drawing`: every visual node before it was a template with the geometry decided
 *     in this repo, which is why unrelated mechanisms kept coming out looking alike.
 *     This one holds no shapes; the agent authors them.
 *   - `ranked`: the first node that is ACTED on rather than read. Its field rules
 *     (what an action settles, and the refusal to invent a price the ledger cannot
 *     support) are exactly the kind that fail silently when undocumented.
 *
 *  Raised again to 21,504 for `because` and `ledger` — the two surfaces that answer
 *  "why did this change" and "what did we prove".
 *
 *  Now 32,768, and the reason is that the brief was printing a list where it should
 *  have been printing a decision. `Item`'s 24 arms each carry a pick-rule in the source
 *  (`pick this over `scene` when…`) and the generator threw every one of them away,
 *  because a union arm is not a declaration and the parser attaches its JSDoc to
 *  nothing; five arms also named a payload — `Code`, `Figure`, `ImageItem`,
 *  `IllustrationItem`, `Scene` — that the file never defined. So the agent read 24
 *  bare names, and a bare name is not a reason to draw a 3D scene instead of a paragraph.
 *  The measured cost of the fix is the ladder itself — 8,275 bytes for `Item`, 3,579 for
 *  `Scene` — plus 3,798 for the five passes, against a brief that measured 15,080 and
 *  said nothing about any of them.
 *
 *  The comparison that makes 32 KiB defensible rather than merely convenient is the
 *  same channel in the three vendors that ship one of these: Claude's `artifact-design`
 *  skill is 35,032 bytes, its `visualize` surface 70,659, and Codex's and ZCode's
 *  `visualize` contract 34,961 — every one of them read once before the model draws,
 *  and none of them apology for. We measure 32,582 today, under all of them.
 *
 *  And the thin headroom is temporary in one specific way: the 2,658 bytes spent on
 *  `KnowledgeFlow`, `KnowledgeBlock`, `ChainRow`, `FlowEdge`, `BlockStatus` and
 *  `EdgeKind` are the four pre-designed templates this branch is deleting, and their
 *  sections go the same day.
 *
 *  (The raise this replaces was for `plate` and `image`, at 15,360: a composed surface
 *  had no way to reproduce a fetched page's own look, or to show a real figure at
 *  all — the same correctness class.) */
/** 33,792 — 342 bytes over the last figure, for one sentence per tagged union saying
 *  WHICH key its arm name is written under.
 *
 *  This is the cheapest correctness fix in the file. `Item` discriminates on `node`,
 *  `Figure` on `kind`, and the brief printed both the same way: `- \`table\` · { table:
 *  NumTable }`. Nothing on the page said the object needs `kind: "table"`, so a
 *  composition written from a correct reading of the brief was dropped at render with
 *  "this figure was not drawn: undefined is not a node this app renders" — and dropped
 *  SILENTLY, because a composition that loses one item still draws the rest. Every
 *  chart, series, equation and table an agent composed was at risk of vanishing with
 *  the answer still looking finished.
 *
 *  It is stated once per union and not once per arm on purpose: on the arm it would
 *  have cost ~9 bytes × 28 choices against a ceiling this file exists to defend. */
/** 38,938 — the 1,740 bytes are "Space optimization and high informational density".
 *  An agent running live dumped markdown tables and technical sweeps into Chat while leaving
 *  the Canvas sparse with dead black voids. This raises the ceiling to mandate packed horizontal
 *  density, zero dead voids, keeping Chat lean (1–2 sentence pointer), and placing all detailed
 *  data, tables, cards, and React Flow knowledge graphs directly onto the Canvas. */
/** 45,000 — raised for `code_context`, `review_accordion`, `pipeline`, `matrix_schematic`,
 *  and `settlement`: the five rich analytical canvas nodes enabling an agent to autonomously
 *  compose file-in-context execution flows, behavioral guard reviews, topological block grids,
 *  and A/B experimental settlement proposals for any research inquiry.
 *
 *  45,500 — 384 bytes over the 45,000 ceiling for the no-hardcoded-design rule: the fixed
 *  heading → prose → figure → table → visual order was removed and the replacement rule
 *  (derive opening, grouping and density from the answer's weight every time) is mandatory
 *  text. A shorter version is not expressible without losing the property the rule protects.
 *
 *  47,200 — 1,635 bytes over the last ceiling for the six richer catalog entries (`Flow`,
 *  `Graph`, `Figure`, `Text`, `Recharts`) so the agent sees the full component library in
 *  `What exists to build with` instead of the two-card tier/card fallback. The entries
 *  add ~270 bytes each, but the cost of leaving them out is an agent that never picks them.
 *
 *  48,000 — 670 bytes over for the scientific carriers: `Plotly`, `LearningCurve`,
 *  `Confusion`, `Embedding`. These cover what scientists and ML researchers actually
 *  visualize — interactive scatter/heatmap/treemap/sunburst, UMAP/t-SNE embeddings,
 *  multi-arm learning curves, and classification evaluation matrices — so the catalog
 *  stops steering them to re-implement those shapes in `Figure`. Each entry stays
 *  below the generic Vega-Lite fallback on brevity; the ceiling is in input bytes only.
 *
 *  50,000 — 1,646 bytes over for the six live scientific renderers: `Recharts`, `Plotly`,
 *  `LearningCurve`, `Confusion`, `Embedding`, `3D`. Research shows scientists visualize
 *  heatmaps, UMAP/t-SNE embeddings, learning curves, confusion/ROC/PR matrices, and 3D
 *  surfaces. Vega-Lite covers static 2D only; Plotly/Recharts carry the interactive
 *  overlays and 3D that the catalog now registers. Ceiling raised to keep the brief the
 *  single place where those carriers exist — otherwise agents re-implement them.
 *
 *  50,300 — 212 bytes over for `MermaidMindmap` and `Markmap`: markdown-driven mind maps
 *  are the low-friction hierarchy carrier (Mermaid) and the interactive alternative
 *  (markmap). Both are open source (Mermaid, markmap-lib) and used in the `awesome-dataviz`
 *  goldmine. Without them the brief steers the agent to `flow` for every hierarchy.
 *
 *  51,200 — 685 bytes over for `Choropleth`, `Sankey`, `Network`: geospatial choropleth
 *  for region/site findings, Sankey flow for proportional movement between stages, and
 *  a Gephi-lite network graph for data/attention networks beyond the formation graph.
 *  Scientists need all three; the catalog previously forced them to re-implement in
 *  `Figure`. All three use existing libraries already in the stack (Plotly, React Flow).
 *
 *  53,200 — one figure menu, ~1.9k, added after the catalog list: three shipped answers
 *  in a row drew `series` with different nouns, which is the template failure the brief
 *  exists to stop. The menu maps the shape of the rows to the catalog's full mark variety
 *  (bars, dots, slopes, heatmaps, Sankey, radar, embedding, 3D, SMIL drawing, graph) and
 *  demands the losing kind's case in the label, so variety is a checkable step. */
const CEILING = 53200;

test("the brief the agent reads is the brief the types say right now", () => {
  const shipped = readFileSync(briefPath, "utf8");
  // Not "contains" and not a field count: an edit to the prose an agent is obeying is
  // the drift this exists to stop.
  expect(
    shipped === brief,
    `${briefPath} is not what the types produce: run \`node scripts/design-brief.ts\` and commit it`,
  ).toBe(true);
});

test("every field of a type the generator reads is named by the brief", () => {
  const missing: string[] = [];
  for (const source of SOURCES) {
    const text = readFileSync(join(import.meta.dirname, "../..", source.file), "utf8");
    // Only the declared templates' own bodies: a file also holds component props, which
    // the renderer reads and the agent never writes — `NumTableView`'s `derive` is the
    // app's choice about a table, not a field of one — and scanning the whole file
    // asked the brief to name them. A body runs from the type's `= {` to the `};` at
    // the left margin.
    for (const shape of source.shapes) {
      const body = new RegExp(`\\ntype ${shape} = \\{([\\s\\S]*?)\\n\\};`).exec(text)
        ?? new RegExp(`\\nexport type ${shape} = \\{([\\s\\S]*?)\\n\\};`).exec(text);
      if (!body) continue; // A union, which the generator prints whole.
      // A template's own members are the ones at the type's own indent; a nested literal
      // is printed inline by the generator and appears in the brief as a shape, not a row.
      for (const [, field, mark] of body[1]!.matchAll(/\n {2}readonly (\w+)(\??)/g)) {
        if (!brief.includes(`- \`${field}${mark}\` ·`)) missing.push(`${source.file} · ${field}${mark}`);
      }
    }
  }
  expect(missing).toEqual([]);
});

test("a type the brief names in a payload is a type the brief goes on to define", () => {
  // The hole this closes is the one that ran for months: `Item` listed 24 arms, five of
  // them said they hold a `Code` or a `Scene`, and no section in the file said what
  // either one's fields are — so the agent could choose a node and had to invent its
  // payload. A shape that is referenced and never defined fails here, not in a real call.
  const builtins = new Set(["Record", "ReactNode", "CSSProperties", "Array", "Readonly"]);
  const undefinedNames = printedTypes
    .flatMap((type) => [...type.matchAll(/\b([A-Z][A-Za-z0-9_]{2,})\b/g)].map((match) => match[1]!))
    .filter((name) => !defined.includes(name) && !builtins.has(name));
  expect([...new Set(undefinedNames)].sort()).toEqual([]);
});

test("every arm the agent chooses between states why to choose it", () => {
  // An arm with no rule is a name in a list, and a name in a list is how the whole
  // surface ends up `prose`: the reason to pick a `figure` over a `plate` has to be in
  // the read. An arm may instead lean on the rule of the shape it holds — `Figure`'s
  // four kinds are covered by the catalog entry and by each payload's own line.
  const documented = new Set(
    [...brief.matchAll(/^## (\w+)\n([^\n-][^\n]*)$/gm)].map((match) => match[1]!).filter((name) => name),
  );
  const silent = choices
    .filter((choice) => !choice.doc)
    .filter((choice) => ![...choice.holds.matchAll(/\b([A-Z][A-Za-z0-9_]{2,})\b/g)].some((m) => documented.has(m[1]!)));
  expect(silent.map((choice) => `${choice.shape} · ${choice.tag}`)).toEqual([]);
});

test("the brief states each template by name and the altitude its fields keep", () => {
  const names = SOURCES.flatMap((source) => source.shapes);
  expect(names.length).toBeGreaterThan(0);
  expect(names.filter((name) => !brief.includes(`## ${name}`))).toEqual([]);
  // The rules that a generated answer gets wrong are the ones about who says what, so
  // the sentences that fix them may not silently drop out of the brief.
  // The five passes are the one part of the brief no type can generate — nothing in a
  // TypeScript union says in what order to reason, or that the graph is read before the
  // layout is planned — so they are pinned here rather than trusted to survive an edit
  // to the generator's prose.
  for (const step of [
    "## The order to think in",
    "**Read the question.**",
    "**Read the graph around it, before you plan anything.**",
    "**Plan the canvas, then fill it.**",
    "**Design each part separately, against its own rule.**",
    "**Render it once, as one answer.**",
    // The price of the pipeline, stated with it: the steps are reasoning, not calls.
    "**The steps cost reasoning, not calls.**",
  ]) {
    expect(brief).toContain(step);
  }
  for (const rule of [
    "You never write markup",
    "an item whose provenance you cannot name",
    "The first line is the one demand and takes the arrow",
    "The composition, when the distiller wants it said outright",
  ]) {
    expect(brief).toContain(rule);
  }
});

test("the brief is read before every answer, so it has a byte ceiling", () => {
  // Bytes, not characters: the brief is full of em dashes, and the prompt is charged
  // for the encoded file an agent reads.
  expect(Buffer.byteLength(brief, "utf8")).toBeLessThanOrEqual(CEILING);
});

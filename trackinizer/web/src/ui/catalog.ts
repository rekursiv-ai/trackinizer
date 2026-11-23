// THE NODE CATALOG. What the agent may ask for, and the proof each one renders.
//
// OpenUI's one good idea, taken one step further than the design brief takes it: the
// catalog is the schema AND the test. Each entry names the library, what the node is
// for, when to pick it over its neighbour, and the DOM measure that proves it draws —
// the assertion catalog.test.tsx runs. An entry whose measure does not hold is removed
// from the catalog rather than documented around, because the brief is read as a
// promise and a node that does not render is a promise broken in the agent's output.
//
// The catalog is also the floor under "generate me something custom": what is listed
// here is what a generated surface may be assembled from, so slop is unrepresentable
// rather than discouraged. Nothing outside it reaches the page.

/** One node the agent may compose with. */
export type Node = {
  /** The name the agent asks for it by, and the component it maps to. */
  readonly node: string;
  /** Where it comes from — the app's own library, or the one it wraps. */
  readonly library: string;
  /** What it draws, in one line. */
  readonly renders: string;
  /** When to pick this one and not its neighbour. The distinction a brief has to carry,
   *  because a wrong pick here is an answer that reads badly rather than one that errors. */
  readonly pick: string;
  /** The composition it takes, if it takes one. A node family's own grammar: the graph's
   *  rows and edges are not a strip's axis and count (see LAW.md). */
  readonly composition?: string;
  /** What the DOM holds once it has drawn: the measure catalog.test.tsx asserts. */
  readonly measure: string;
};

/**
 * The catalog, in the order the brief states it: what is read first, then what is
 * measured, then what is written into.
 */
export const CATALOG: readonly Node[] = [
  {
    node: "Prose",
    library: "ui/sections.tsx",
    renders: "A paragraph at answer size, with its `backticked` tokens read as code.",
    pick: "Two to four sentences of what the records say, at answer size, backticked tokens as code. A number, a list or a mechanism has a more specific node — prose is what none of them holds.",
    measure: "1 p.ui-prose; a ticked token is 1 code.ui-tok and the ticks never print",
  },
  {
    node: "Marks",
    library: "ui/sections.tsx",
    renders: "Lines each carrying a glyph: `+` kept, `−` broken, `·` neutral, `→` the one demand.",
    pick: "A list. Prose never holds one — a list inside a paragraph is a list the design cannot put a glyph on, and the glyph is the verdict.",
    measure: "one p.ui-mark per line; hue is on b[aria-hidden] alone, never on a word",
  },
  {
    node: "Section",
    library: "ui/sections.tsx",
    renders: "A section rule: the label, the hairline carrying the width, the measure at its end.",
    pick: "A rule over a group of nodes: the label names the group, the hairline carries its width, and a measure sits at its end in the machine register. What that measure states — a size, a span, a count — is yours to pick for what the group actually holds.",
    measure: "p.ui-sec > b (the label, white) + s (the hairline) + em (the measure, grey)",
  },
  {
    node: "Table",
    library: "ui/table.tsx",
    renders: "A table as its source prints it: the caption whole, the columns in its order.",
    pick: "Figures a source already laid out in a grid. Quote it; never re-shape it, and never re-order its columns.",
    composition: "the caption, the columns in the source's order, one array per row with the row's own name first",
    measure: "1 table, 1 p.ui-cap carrying the caption whole, one tr per row; a derived row is tr.ui-derived and is computed, never written",
  },
  {
    node: "Chart",
    library: "vega-lite + vega-embed",
    renders: "A figure from the answer's own rows, in the design's ink.",
    pick: "Rows you are comparing — a count per kind, a spread across arms. The spec carries data, marks and encoding; a spec that names a colour, a font, a title or a config is refused, because on this surface a hue is a verdict.",
    measure: "1 .ui-chart with 1 svg; Vega's action menu never renders",
  },
  {
    node: "Series",
    library: "uPlot",
    renders: "A metric curve over steps, on a canvas.",
    pick: "An Experiment's metric series — thousands of ordered points. Two or three curves on one x. A curve on its own steps is a second Series, never a second line here.",
    measure: "1 .ui-series with 1 canvas and 2 axes",
  },
  {
    node: "Equation",
    library: "KaTeX",
    renders: "A formula as its source writes it, in TeX.",
    pick: "A formula a paper or a record states. Display is the only block form. A formula that will not parse prints as its own source in machine ink — never as an error, and never dropped.",
    measure: "1 .ui-eq > .katex with .katex-html; 0 KaTeX error spans",
  },
  {
    node: "Register",
    library: "@tanstack/react-virtual",
    renders: "A strip or a list longer than its lane, mounted a window at a time.",
    pick: "Any run of items that can exceed the pane — a replay of commits, a register of shapes. It scrolls on its own axis; it never wraps to a second row and never drops its tail.",
    composition: "the axis (`x` a strip, `y` a list), the item count, and one item's extent",
    measure: "a window of .ui-reg-row/.ui-reg-col mounted, and .ui-reg-rail reserving all of it",
  },
  {
    node: "Paths",
    library: "ui/sections.tsx",
    renders: "Addresses under a sentence about them, two to a row, in machine ink.",
    pick: "The files a finding sits on. Each ends at the line it is about. Print them whole — a reader pastes one into a shell.",
    measure: "one span per path, broken after a slash and nowhere else (wbr)",
  },
  {
    node: "Prose/Markdown",
    library: "react-markdown + remark-gfm",
    renders: "Stored markdown as the record holds it.",
    pick: "Text the store already carries — a record's description, a paper's abstract. Read, never written.",
    measure: "the fold opens to 1 p, 1 strong, 3 code",
  },
  {
    node: "Rich",
    library: "@tiptap/react",
    renders: "The one surface that is written into rather than read.",
    pick: "Text a person is composing. Not for anything the store already holds — that is Prose/Markdown, which costs no editor.",
    measure: "1 .ProseMirror, the prose face, clamped to the 630px measure",
  },
  {
    node: "Flow",
    library: "ui/flow.tsx (React Flow)",
    renders: "Agent-authored knowledge flow: carded blocks in tiers with named, arrowed relations (supports, came from, narrows).",
    pick: "When the answer is how one thing came from another — what formed a claim, what a change broke, how a result was arrived at. The relation is the finding. Pick this over `graph` when the reading is yours.",
    composition: "blocks with slot/tier, relations with label, optional height",
    measure: "1 .ui-flow with React Flow nodes and edges; relations are labelled",
  },
  {
    node: "Graph",
    library: "ui/compose.tsx (React Flow KnowledgeGraph)",
    renders: "Store territory: every record within hops of the focal record, drawn as the fold with labelled wires.",
    pick: "When the weight is in the relations the store already holds. Pick this over `flow` when the territory is the store's and the reader is doing the reading.",
    measure: "1 knowledge graph with claim cards and formation lines",
  },
  {
    node: "Figure",
    library: "ui/figure.tsx (Vega-Lite + uPlot + KaTeX + NumTable)",
    renders: "A figure an answer places: chart, series, equation, or NumTable figure.",
    pick: "Rows you are comparing, a metric curve over steps, a formula a paper states, or a table a source printed. The spec carries data, marks and encoding; a value the code does not produce is never drawn.",
    measure: "1 figure with the selected kind rendered",
  },
  {
    node: "Text",
    library: "ui/compose.tsx (register lines)",
    renders: "A line of text at one of the surface's three registers.",
    pick: "`chrome` is the 10.5px uppercase mono for labels and locations; `machine` is a value read off an instrument, white, in the machine face; `read` is the paragraph a surface is read for.",
    measure: "1 p.ui-t with is-chrome/is-machine/is-read",
  },
  {
    node: "Plotly",
    library: "plotly.js",
    renders: "Interactive scientific figure (scatter, heatmap, treemap, sunburst, embedding, 3D).",
    pick: "When the scientist needs zoom/pan/hover, 40+ Plotly chart types, UMAP/t-SNE scatter colored by cluster, or a 3D scene. Vega-Lite is the static fallback; Plotly is the interactive one.",
    measure: "1 .ui-plotly with canvas and Plotly toolbar",
  },
  {
    node: "LearningCurve",
    library: "uPlot",
    renders: "Multi-arm learning curve over steps, with hover crosshair.",
    pick: "When comparing training runs over steps — loss, accuracy, reward. Two or three curves on one x. Not a table.",
    measure: "1 .ui-series with multiple lines on shared x",
  },
  {
    node: "Confusion",
    library: "Vega-Lite (rect + text)",
    renders: "Confusion matrix, ROC, and PR curves as first-class figures.",
    pick: "Classification evaluation where the matrix or the curve is the verdict. A confusion matrix heatmap with counts, ROC with AUC, PR with average precision.",
    measure: "1 .ui-figure with rect/text or line marks",
  },
  {
    node: "Embedding",
    library: "Plotly (scatter)",
    renders: "UMAP / t-SNE / PCA embedding scatter colored by cluster or label, with hover identity.",
    pick: "High-dimensional records projected to 2D. Points are records; color is cluster/label; hover names the point. Vega-Lite needs a transform; Plotly carries it.",
    measure: "1 .ui-plotly scatter with legend and hover",
  },
  {
    node: "MermaidMindmap",
    library: "mermaid",
    renders: "Mermaid mind map and flowchart diagram from markdown-like text definitions.",
    pick: "When the answer is a hierarchy or a causal chain that reads better as a mind map than as text. Mermaid renders the definition; the app owns ink.",
    measure: "1 .ui-mermaid with svg inside",
  },
  {
    node: "Markmap",
    library: "markmap-lib",
    renders: "Interactive mind map rendered from Markdown (markmap).",
    pick: "When the answer is a hierarchy that can be authored in Markdown and rendered interactively. The mind map is the presentation, Markdown is the source.",
    measure: "1 .ui-markmap with data-markmap attribute",
  },
  {
    node: "Choropleth",
    library: "Plotly (choropleth)",
    renders: "Geospatial choropleth for studies/trials by region or site.",
    pick: "When the finding is spatial: per-region rates, site-level outcomes, or geographic spread. Plotly carries it; Vega-Lite does not geo-project.",
    measure: "1 .ui-plotly choropleth with geo projection",
  },
  {
    node: "Sankey",
    library: "Plotly / Vega-Lite (sankey)",
    renders: "Sankey flow diagram for proportional movement between stages.",
    pick: "When value moves between states — budget to options to cost, tokens to clusters, or cohort attrition. The width is the quantity.",
    measure: "1 .ui-plotly sankey with links and label hover",
  },
  {
    node: "Network",
    library: "React Flow (Gephi-lite)",
    renders: "Read-only network graph with centrality and community as position.",
    pick: "When the answer is about a data/attention network, not the formation graph. Position is centrality; color is community; the layout is derived, not authored.",
    measure: "1 .ui-network with nodes/edges and React Flow controls",
  },
] as const;

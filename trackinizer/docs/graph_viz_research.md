# Knowledge-Graph Visualization: Library & Solution Research

Date: 2026-06-28. Status: research, pre-implementation.

Goal: a trackinizer web page that visualizes the stored inquiry graph — typed
nodes, typed directed edges — as an interactive, document-knowledge-graph
explorer.

## What we're visualizing

The graph already exists in the store (`types/inquiries.py`, `types/edges.py`):

- **Node kinds** (want distinct color/shape per kind): `Issue`, `Belief`,
  `Experiment`, `Paper`, `CodeChange`, `WebResult`, `WebSearch`,
  `AgentSession`, `Artifact`.
- **Edge kinds** (want distinct color/style, directed child -> parent):
  `narrows`, `requires`, `produced_by`, `proves`, `favors`, `supersedes`.
  `proves`/`favors` carry a signed `valence` in `[-1, 1]`.
- **Scale**: hundreds to low-thousands of nodes (not millions).
- **Data already served**: `GET /api/web/get/{id}` returns a node plus its
  `edges` / `backlinks` grouped by edge kind (`server/web.py`) — the exact
  shape a graph view consumes.

## Firm requirements

1. **Hover** a node -> preview card (type + key fields), color-coded per kind.
2. Edges **color-coded per edge kind**, directed (arrowheads).
3. **Click** a node -> navigate to that node's detail page (lib must expose a
   reliable node-click event carrying the node id).
4. Ideally **force-directed OR hierarchical/DAG** layout (the graph is largely
   a DAG by creation time).

## Deployment constraint (load-bearing)

The frontend is a **single self-contained `index.html` vanilla-JS SPA**: no
npm, no bundler, no build step. New deps load via a plain
`<script src="https://cdn...">` tag (the app already loads `marked` and
`dompurify` from jsdelivr this way). An ESM/npm-only lib with no UMD/CDN build
is a near-disqualifier.

## Two tiers evaluated

### Tier A — low-level rendering libraries

Two independent agents (Claude, GPT-5.5) both ranked **vis-network #1**.

| Library | CDN `<script>` | Hover tooltip | Per-edge-type style + arrows | Node-click event w/ id | DAG layout | License / recency |
|---|---|---|---|---|---|---|
| **vis-network** | ✅ `vis-network@10/standalone/umd/vis-network.min.js` | ✅ built-in `title` + `hoverNode` | ✅ per-edge `color`+`arrows`+`dashes` | ✅ `click` -> `params.nodes[0]` | ✅ native hierarchical | Apache-2.0/MIT, current |
| **force-graph** (Vasturiano 2D) | ✅ `force-graph@1.51/dist/force-graph.min.js` | ✅ `nodeLabel` + `onNodeHover` | ✅ `linkColor`/`linkLineDash`/`linkDirectionalArrowLength` | ✅ `onNodeClick(node=>node.id)` | ✅ `dagMode` | MIT, current (npm Feb 2026) |
| **Cytoscape.js** | ✅ `cytoscape@3.34/dist/cytoscape.min.js` | ⚠️ event native, card hand-built (or `cytoscape-popper`) | ✅ stylesheet `edge[type="proves"]` | ✅ `tap` -> `target.id()` | ⚠️ via `cytoscape-dagre`/`-elk` ext | MIT, current |
| D3.js | ✅ | hand-built | hand-built | hand-built | `d3-hierarchy` (trees) | ISC; umbrella >12mo old |
| Sigma.js + graphology | ⚠️ no clean v3 UMD | event only | partial | ✅ `clickNode` | ❌ none native | MIT; Sigma current |
| ECharts graph series | ✅ | ⚠️ awkward data model | ✅ | ✅ | weak multigraph DAG | Apache-2.0, current |
| G6 (AntV) | ⚠️ verify global | ✅ Tooltip plugin | ✅ | ✅ | ✅ Dagre | MIT; heavier v5 API |
| 3d-force-graph | ✅ | ✅ | ✅ | ✅ | ✅ | MIT; 3D hurts readability |
| Cosmos / cosmos.gl | ✅ | ❌ index-based | ❌ no per-type | ⚠️ click -> index not id | ❌ | MIT; GPU-scale, wrong problem |
| ELK.js | ✅ | n/a | n/a | n/a | ✅ Sugiyama | EPL-2.0; layout engine only, no renderer |

**Tier-A recommendation**: **vis-network** — all four firm requirements
built-in with zero glue, native hierarchical DAG, single CDN tag. Both agents
agreed #1; they split only on #2/#3 (GPT: Cytoscape > force-graph for styling
model; Claude: force-graph > Cytoscape after the hover/click requirement,
since Cytoscape's hover *card* is not built-in).

### Tier B — end-to-end / batteries-included explorers

We asked whether a ready-made document-graph **explorer** (search + detail
panel + typed styling + filter already wired) beats assembling Tier A. The two
agents **diverged**, and the divergence is the decision.

- **GPT** ranked Graphistry / AWS Graph Explorer / Gephi Lite — all **full
  apps or hosted services** (server dependency, iframe embed, or React build).
  Optimizes for richest UX, accepts the server/iframe cost.
- **Claude** ranked Memgraph Orb / Gephi Lite / react-force-graph — all
  **embeddable into a no-build SPA**, ingest arbitrary REST JSON. Optimizes
  for our deployment constraint and click-through.

**Key finding**: no single solution is simultaneously (a) fully
batteries-included, (b) embeddable in a no-build SPA, **and** (c)
click-through into our own detail pages. Iframe/full-app options (Gephi Lite,
Graphistry, AWS Explorer) sandbox away requirement 3 (click -> our page).

Embeddable Tier-B candidates that **preserve** click-through:

| Solution | Tier | Integration | Takes our JSON? | License / recency | Note |
|---|---|---|---|---|---|
| **Memgraph Orb** | `<script>` widget | drop-in CDN, `new Orb.Orb(el)` | ✅ `{nodes,edges}` verbatim | Apache-2.0 | ⚠️ last release **Feb 2024** (stale, pre-1.0) |
| **react-force-graph** | React widget via CDN | needs React+ReactDOM globals (still no build) | ✅ native `{nodes,links}` | MIT, **npm Feb 2026** | richest hover/click/typed-edge primitives |
| **Neo4j NVL** | embeddable lib | `new NVL(...)`, heavier | ✅ arbitrary node/rel arrays | npm ~1.2.0 (2026); verify license | fresher Orb-alternative |

**Avoid / poor fit (Tier B)**:

- **Gephi Lite** — polished OSS, but iframe/fork (GPL-3.0) + GEXF/JSON
  transform; iframe sandbox **breaks click-into-our-detail-page**.
- **AWS Graph Explorer** — Apache-2.0 but backend-locked to
  Gremlin/SPARQL/openCypher; won't take arbitrary REST JSON; React app/service.
- **Graphistry** — most batteries-included, but cloud/self-hosted **server
  dependency**; Hub is commercial.
- **Cosmograph** — closed-source, CC-BY-NC (commercial use = paid).
- **Neo4j Bloom** — Neo4j-database-locked.
- **KeyLines / ReGraph, yFiles, Ogma/Linkurious, Tom Sawyer** — commercial
  paid SDKs.
- **Memgraph Lab / GraphXR / Graphlytic** — platform/service, not a static
  embeddable widget.

## Temporal / real-time growth (the "killer demo" axis)

Added requirement: show the graph **growing organically over time** -- nodes
appearing as knowledge is created, the force simulation re-settling live so
existing nodes push apart to make room. This is the decisive axis and it
sharply reorders the ranking.

Both halves already exist in our backend, so no new infra:

- **Replay**: every Inquiry row carries `created` (`types/inquiries.py`);
  `GET /api/web/recent_changes` already `ORDER BY c.created`. Sort by `created`,
  feed nodes in on a timer.
- **Real-time**: `GET /api/web/subscribe` is a live SSE relay emitting
  `{"id": "<uuid>"}` per change (`server/web.py` `web_subscribe`,
  `server/notify.py` `iter_sse_events` over `NOTIFY_CHANNEL`). The SPA already
  consumes it. On each id: fetch the node, append it, re-heat the sim.

### Fit for incremental node addition + warm re-settling sim

| Library | Live add re-heats sim? | Organic "push-apart" growth | Verdict |
|---|---|---|---|
| **force-graph / react-force-graph** | ✅ `graphData()` append keeps the d3-force sim warm; `d3ReheatSimulation()` | ✅ canvas sim always-warm, nodes flow apart on arrival; has an official **dynamic/incremental-add example** | **best fit** |
| vis-network | ⚠️ `nodes.add()` triggers re-stabilization | ⚠️ physics tuned to settle-and-stop; works but less continuous | runner-up |
| Cosmos / cosmos.gl (GPU) | ✅ GPU sim re-runs live | ✅ unmatched for a large-scale live "bloom" | spectacle only (loses click-id + typed styling + DAG) |
| Cytoscape.js | ❌ layouts are run-to-completion batch | ❌ incremental add re-runs layout -> nodes **jump**, not flow | poor fit |
| Sigma.js | ⚠️ drive graphology FA2 worker manually | ⚠️ assembly-heavy | poor fit |
| D3-force raw | ✅ (it IS the engine) | ✅ but hand-build render/hit-test | force-graph already wraps it |

**Temporal-axis recommendation**: **force-graph** is the one option satisfying
*all* of -- organic real-time growth (warm re-heating sim), nodes pushing apart
on arrival, typed-node color, hover card, click-to-detail, directed typed
edges, force-or-DAG layout. Keep **Cosmos** only for a future large-scale GPU
"bloom" view where spectacle outweighs per-node interaction.

## Recommendation

The firm **click-node -> open its detail page** requirement is the tiebreaker:
it rules out the iframe/full-app explorers. Within what's embeddable in a
no-build SPA and click-through-capable:

1. **force-graph** (plain, non-React) or **react-force-graph** — realistic
   "fully-featured, minimal-work" sweet spot; preserves click-through; freshest
   maintenance. Plain `force-graph` avoids React globals entirely and both
   agents already rated it highly.
2. **vis-network** — if a more declarative options object + native hierarchical
   DAG is preferred over canvas force layout; everything built-in, single tag.
3. **Memgraph Orb** — cleanest single `<script>` widget that eats our JSON
   verbatim, **but** verify the 2-year-stale release still loads before
   committing.

We still wire the search box + detail panel ourselves in every embeddable
option — that work is small and is exactly what keeps click-through into our
own pages.

**The temporal/real-time requirement is decisive**: force-graph is the only
candidate that animates organic growth (warm re-heating force sim, nodes
pushing apart on arrival) *and* keeps click-through, hover cards, and typed
edges. It is the firm #1 for this project.

## Open decisions

1. Click-through into our detail pages — firm, or droppable for a demo?
2. Tolerate React globals (react-force-graph) or zero React (plain
   force-graph / vis-network / Orb)?
3. Force-directed vs hierarchical/DAG as the default layout.

## GitHub reference implementations

Two agents searched GitHub (stars/recency verified via `gh` / repo fetch,
2026-06-28). Star counts are organic except where flagged.

### Study first

1. **noworneverev/graphrag-visualizer** — canonical browser GraphRAG explorer:
   typed entity coloring + **search + click-to-detail side panel**, fully
   client-side (react-force-graph). The explorer UX we want. ~420★, MIT.
   <https://github.com/noworneverev/graphrag-visualizer>
2. **the-palindrome/ml-knowledge-graph** — closest architectural twin:
   **no-build static-file SPA**, JSON-fed (2081 nodes/5149 edges), 3D force
   graph + upstream-dependency highlighting. Our exact stack/constraint.
   <https://github.com/the-palindrome/ml-knowledge-graph>
3. **jackyzha0/quartz** — digital-garden SSG with a polished note-link graph
   (`graph.tsx`): **hover-highlight neighbors, local vs global graph,
   click-to-navigate routing** + static JSON graph emission. ~12.6k★, MIT.
   <https://github.com/jackyzha0/quartz>

### Agentic / LLM-agent graphs

- **microsoft/graphrag** — copy its entity/relationship/community **schema ->
  our typed-node/edge JSON shape**. ~34k★, MIT.
  <https://github.com/microsoft/graphrag>
- **neo4j-labs/llm-graph-builder** — LLM doc->graph; copy chunk `produced_by`
  document **provenance edges** (parallels our `produced_by`/`proves`). ~4.9k★,
  Apache-2.0. <https://github.com/neo4j-labs/llm-graph-builder>
- **HKUDS/LightRAG** — GraphRAG engine with built-in sigma.js/graphology web KG
  viewer; server-fed-KG -> browser wiring. ~37k★, MIT.
  <https://github.com/HKUDS/LightRAG>
- **JoeDoesJits/mempalace-viz** — **single-file vanilla SPA** of an agent memory
  graph (D3), semantic clustering + themed tooltips. Our exact frontend
  constraint, small + copyable. ~8★, MIT.
  <https://github.com/JoeDoesJits/mempalace-viz>
- **vbcherepanov/total-agent-memory** — Claude Code/Codex persistent memory ->
  extracted KG + 3D WebGL viz; copy the agent-memory-as-entities data model.
  ~56★, MIT. <https://github.com/vbcherepanov/total-agent-memory>
- **Robbings/chatgpt-graph-navigator** — a ChatGPT session as a navigable graph
  + timeline tree; relevant to our **AgentSession** node type. ~119★.
  <https://github.com/Robbings/chatgpt-graph-navigator>
- **safishamsi/graphify** — repo -> browser KG, emits `graph.html` +
  `graph.json` (near-identical to our deliverable). ⚠️ ~73k★ but YC-S26,
  **star count promoted/inflated**, not organic; substance real.
  <https://github.com/safishamsi/graphify>

### Document / knowledge-base graphs

- **silverbulletmd/silverbullet** — self-hosted web Markdown PKM using
  **`force-graph` + d3-force**; closest reusable *browser* reference for
  docs + force graph + search/editor UX. ~5.6k★, MIT.
  <https://github.com/silverbulletmd/silverbullet>
- **logseq/logseq** — mature PKM graph (d3-force/graphology/pixi.js):
  backlinks, page graph at scale. ~43k★, AGPL-3.0 (copyleft — patterns only).
  <https://github.com/logseq/logseq>
- **foambubble/foam** — Markdown wikilink graph; copy **backlink/forward-link
  toggles, tag filters, graph styling config**. ~17k★, MIT.
  <https://github.com/foambubble/foam>
- **voicetreelab/voicetree** — Obsidian-style canvas for multi-agent
  orchestration; node-detail-on-click in a force layout. ~884★.
  <https://github.com/voicetreelab/voicetree>

### Library reference implementations

- **vasturiano/force-graph** — canonical no-build vanilla-JS force graph; the
  `nodeColor`/`nodeCanvasObject` (typed coloring), `nodeLabel`/`onNodeHover`
  (hover cards), `linkColor`/`linkDirectionalArrow` (edge-type styling)
  patterns. ~2.1k★, MIT. <https://github.com/vasturiano/force-graph>
- **vasturiano/react-force-graph** (~3.2k★) / **3d-force-graph** (~6.2k★) —
  worked hover-preview + click-to-focus + directional-arrow examples. MIT.
- **PR0CK0/PR0CK0.github.io** — Cytoscape KG explorer driven by a single
  YAML/JSON source, GitHub Pages; copy typed-content -> Cytoscape styling/layout.
  Small. <https://github.com/PR0CK0/PR0CK0.github.io>
- **Drfiya/gitnexus-explorer** — Sigma 3 + Graphology + ForceAtlas2 codebase KG
  with AI side panel; clean Sigma reference.
  <https://github.com/Drfiya/gitnexus-explorer>
- **sparna-git/Sparnatural** — config-driven typed-node/edge styling from a
  schema; maps to our taxonomy. ~298★, LGPL-3.0.
  <https://github.com/sparna-git/Sparnatural>

### Caveats

- No clean standalone **Cytoscape.js document-KG app** surfaced above noise
  (sigma/graphology side covered by LightRAG, graphrag-visualizer, gitnexus).
- Stale: `xyjigsaw/Knowledge-Graph-And-Visualization-Demo` (2023),
  `athensresearch/athens` (unmaintained) — UX reference only.

## To verify before implementation

- Exact current CDN URLs + UMD globals for the chosen lib (agents' version
  pins are assertions, not yet confirmed against jsdelivr).
- That Orb's Feb-2024 build still loads cleanly (if chosen).

---

# Implementation Plan

## Goal & success measure

Build a graph page that **populates over time** while
`docs/trax_research_example_3.sh` writes the claim-tree into an ephemeral
trackinizer. **Success = we watch the graph grow live**: as each `trax` command
in example_3 creates a node, it appears in the browser and the force sim
re-settles, nodes pushing apart. No persistence, no auth -- the demo is the
ephemeral server example_3 already boots (`trackinizer --ephemeral --no-auth`,
wiped on Ctrl-C).

## Chosen stack

- **Renderer**: `force-graph` (Vasturiano, 2D canvas), plain -- no React.
  Single CDN `<script>` tag, matching the existing `marked`/`dompurify` pattern
  in `assets/index.html`. Warm re-heating sim = organic growth; `onNodeHover`
  /`nodeLabel` = color-coded preview; `onNodeClick` = click-through;
  `linkColor`/`linkDirectionalArrowLength` = typed directed edges.
- **Live channel**: existing `GET /api/web/subscribe` SSE (`{"id": uuid}` per
  change). No new backend endpoint required for the live path.
- **Bulk load**: existing read routes. The only likely backend addition is a
  whole-graph endpoint (below).

## Backend (small)

1. **`GET /api/web/graph`** -- one-shot whole-graph fetch for initial paint and
   for replay ordering. Returns `{nodes:[{id, kind, seq, title, created}],
   edges:[{from_id, to_id, edge_kind, valence?}]}`, nodes ordered by `created`.
   - Add to `server/web.py` alongside the existing read routes; gate at
     `viewer` like the others. Reuses the `inquiries` + `edges` tables already
     queried by `web_get`/`_edges_for`.
   - Drift test in `server/web_test.py` pinning the node-kind and edge-kind sets
     against `Inquiry.InquiryKind` / `Edge.Kind` (same pattern as
     `routes_drift_test.py`), so the SPA legend can't silently desync.
   - If a whole-graph scan is judged too broad for the read API, the demo can
     instead build the graph purely from the SSE stream + per-id `web_get`
     fetches (no new route) -- decide at implementation time. Prefer the
     endpoint: it makes replay-by-`created` trivial and is reusable beyond the
     demo.

## Frontend (`assets/graph.html` + a `/graph` route)

1. **Route**: register `/graph` in `server/web.py` `attach()` via the existing
   `_add_page_route(app, "/graph", assets / "graph.html")` mechanism (mirrors
   `/console`, `/me`). No new routing concept.
2. **Page**: a new self-contained `assets/graph.html` following `index.html`'s
   conventions exactly -- inline `<style>` (Tokyo Night palette already
   defined there), CDN `<script>` for force-graph, vanilla JS. No build step.
3. **Typed styling** (one source of truth, copied from the domain):
   - Node color per `kind` (9 kinds): map drawn from the palette tokens already
     in `index.html` (`--status-*`, `--judg-*`, accent/purple/orange).
   - Edge color/style per `edge_kind` (6 kinds): e.g. `proves` solid + arrow,
     `favors` dashed, `supersedes` distinct hue; arrowheads via
     `linkDirectionalArrowLength`. `valence` sign tints `proves`/`favors`
     (green positive / red negative).
4. **Interactions** (the firm three):
   - Hover -> `nodeLabel`/`onNodeHover` renders a color-coded preview card
     (kind + title + key field), reusing `index.html`'s detail-rendering helpers
     where possible.
   - Click -> `onNodeClick(node => navigate to the node's detail view)` (the
     existing SPA detail route / `web_get` page).
   - Legend: static color key for node + edge kinds.
5. **Live growth (the demo core)**:
   - Initial paint from `GET /api/web/graph` (empty or partial at demo start).
   - Open `EventSource('/api/web/subscribe')`; on each `{"id"}`: if unseen,
     `GET /api/web/get/{id}`, append node + its edges to `graphData()`, call
     `d3ReheatSimulation()` so the layout re-settles and nodes push apart.
   - Optional "replay" toggle: instead of live SSE, pull `/api/web/graph` and
     add nodes on a timer in `created` order -- same animation, deterministic
     for a recorded demo.

## Demo driver

`docs/trax_research_example_3.sh` is the substrate, unchanged in spirit. To make
growth *visible* rather than instant:

- Watch live: open `${URL}/graph` (printed alongside the existing `${URL}/`)
  before/while the script runs; the ~30 `trax` writes stream in via SSE.
- For a paced demo, a thin wrapper (or an opt-in `SLEEP=` between `trax`
  commands in a copy of the script) spaces the writes so the audience sees each
  node land. Do NOT edit example_3 in place for this -- add pacing via an env
  knob or a sibling demo script, keeping example_3 canonical.

## Verification

- **Success criterion met** when, running example_3 with the `/graph` page open,
  nodes appear incrementally and the sim visibly re-settles (the root belief,
  three gates, searches, papers, experiment populate over time).
- Unit: `web_test.py` covers the new `/api/web/graph` shape + the drift test.
- Manual: `/graph` renders all 9 node kinds and 6 edge kinds with distinct
  styling; hover card is color-coded; click navigates to the detail view.
- Python gates per AGENTS.md: `ruff`, `ty`, `basedpyright`, `pytest` clean.
  (Frontend is static HTML/JS -- no JS toolchain, consistent with the repo.)

## Sequencing

1. `GET /api/web/graph` endpoint + drift/unit tests.
2. `assets/graph.html` static load (initial paint, typed styling, legend).
3. Hover card + click-through.
4. SSE live-append + `d3ReheatSimulation` growth.
5. Paced-demo env knob; run against example_3; confirm the success measure.

## Open decisions (carry from above)

1. Whole-graph endpoint vs SSE-only build (prefer endpoint).
2. Default layout: force-directed for the organic-growth demo; hierarchical/DAG
   as a toggle (the data is a creation-time DAG).
3. Pacing mechanism for the demo (env knob vs sibling script) -- must not
   mutate canonical `example_3`.

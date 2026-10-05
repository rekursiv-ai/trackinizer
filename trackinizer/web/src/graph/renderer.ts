// The only module that imports force-graph (https://github.com/vasturiano/force-graph),
// so it loads with the graph's chunk, and the view's tests replace it: jsdom has no
// canvas.
import ForceGraph from "force-graph";
import { type Box, type Island, islandsOf, placeLabels } from "./clusterLabels";
import { DUO_ZOOM, fillAt } from "./encode";
import { type Layout, layout } from "./layout";
import type { DrawLink, DrawNode } from "./model";

/** What covers the canvas, in pixels: a strip on its right (Peek, the key or the zoom buttons). */
export type Covered = { readonly right: number };

/** The theme's colours for an island's name: its title, what it holds, and the outline that keeps both off the edges. */
export type IslandInk = { readonly title: string; readonly count: string; readonly outline: string };

/** What the view asks of the canvas. */
export type Renderer = {
  /**
   * Draw `nodes` and `links` from now on: a new graph, or the same one with
   * inquiries or edges come or gone. Each node keeps the place the layout gave
   * it. With `frame`, the view frames the visible nodes it keeps once the
   * layout settles; without, a frame already asked for stands.
   */
  setData(nodes: readonly DrawNode[], links: readonly DrawLink[], frame: ((node: DrawNode) => boolean) | null): void;
  /** Draw again: the nodes' and links' looks or hidden flags changed. */
  repaint(): void;
  /**
   * Frame the visible nodes `filter` keeps, every one by default, in what is
   * left of the canvas once what covers it is taken off.
   */
  fit(filter?: (node: DrawNode) => boolean): void;
  /** Frame again what the last fit framed, in what is left of the canvas now: what covers it changed. */
  refit(): void;
  /** Zoom in by `factor` about the middle, or out by one under 1. */
  zoomBy(factor: number): void;
  /**
   * Gather the nodes into one island per group `groupOf` names, links between
   * islands pulling only faintly; null lays the graph out whole again. Once the
   * layout settles, what the last fit or `setData` framed is framed again.
   */
  group(groupOf: ((node: DrawNode) => string | undefined) | null): void;
  /**
   * Name each island while grouped, `name` giving a group's key its title; a key
   * with no name, and every island, goes unnamed when `name` is null. The names
   * draw only while the view is zoomed out far enough that the nodes' own labels
   * would be unreadable, and fade out as it zooms in past that.
   */
  nameIslands(name: ((key: string) => string | undefined) | null, ink: IslandInk): void;
  /**
   * Bring `node` to the middle of what is left of the canvas once what covers it
   * is taken off, zoomed in to at least 2.5x, as v1's jump to a node did.
   */
  centre(node: DrawNode): void;
  /** Where `node` is, in pixels from the host's top left. */
  screenOf(node: DrawNode): { x: number; y: number };
  /** Stop drawing and let the host go. */
  dispose(): void;
};

/** What the pointer does on the canvas. */
export type RendererEvents = {
  /** The pointer came onto `node`, or off every node: null. */
  hover(node: DrawNode | null): void;
  /** A click on `node`, or on the background: null. */
  click(node: DrawNode | null): void;
  /** A double-click on `node`. */
  doubleClick(node: DrawNode): void;
  /** A click on an island's name while grouped. */
  island(key: string): void;
};

/**
 * Make the renderer that draws into `host`, which it fills, telling `events`
 * what the pointer does; `covered` says what covers the canvas, measured each
 * time it frames.
 */
export type CreateRenderer = (host: HTMLElement, events: RendererEvents, covered: () => Covered) => Renderer;

/**
 * Draw with force-graph on a canvas filling `host`, with v1's forces: charge -70
 * up to 280 apart, links 55 long at strength 0.6, a faint pull to the middle so
 * unlinked pieces stay near, and velocity decay 0.58. A double-click on a node
 * is the browser's own, on the node the pointer is over.
 *
 * A fit while the layout runs frames what it asked for now, and again once the
 * layout settles, since the nodes are still moving; a centre on a node drops
 * that. Only a fit and `setData` change what is framed, so a regrouping keeps
 * a focus's frame or a root's island. Each frame leaves out what `covered` says
 * covers the canvas then.
 * Labels draw over everything, the busiest nodes' first, and one that would
 * overlap another is left out.
 */
export const forceGraphRenderer: CreateRenderer = (host, events, covered) => {
  /** What the last fit framed, and whether to frame it again once the layout settles. */
  let framed: (node: DrawNode) => boolean = everything;
  let frameWhenSettled = false;
  let settling = false;
  let pointed: DrawNode | null = null;
  let labelled: DrawNode[] = [];
  /** How the islands are grouped and named now, or null while the graph is laid out whole. */
  let grouped: ((node: DrawNode) => string | undefined) | null = null;
  let named: ((key: string) => string | undefined) | null = null;
  let islandInk: IslandInk = { title: "", count: "", outline: "" };
  /** Where the island names were drawn last frame, in world coordinates, so a click finds them. */
  let islands: { key: string; box: Box }[] = [];
  /** The layout engine, built by `group`: it moves the nodes while the engine coasts. */
  let engine: Layout | null = null;
  /** The grouping the engine was built for, so a live update keeps the layout it had. */
  let lastGrouping: string | null = null;
  const font = getComputedStyle(host).fontFamily;
  const mono = getComputedStyle(host).getPropertyValue("--mono").trim() || font;
  const graph = new ForceGraph<DrawNode, DrawLink>(host)
    .width(host.clientWidth)
    .height(host.clientHeight)
    .nodeVisibility((node) => !node.hidden)
    // The size force-graph reckons a node at (the square root of this times
    // its `nodeRelSize`, 4) places arrowheads on the rim and pads the fit.
    .nodeVal((node) => (node.look.radius / 2) ** 2)
    .nodeCanvasObject(drawNode)
    .nodePointerAreaPaint(paintHitArea)
    .linkVisibility((link) => !link.hidden)
    .linkCanvasObject(drawLink)
    // The arrowheads' colour; force-graph draws them, and `drawLink` the lines.
    .linkColor((link) => link.look.color)
    .linkDirectionalArrowLength(4)
    .linkDirectionalArrowRelPos(1)
    .d3VelocityDecay(0.58)
    // The layout stops once it has cooled, about 250 ticks — enough for the
    // layout engine's own alpha to settle first — rather than after
    // force-graph's 15 s: it is framed then, and an idle canvas costs nothing.
    // Never by the clock: a tab opened in the background gets no frames, and
    // force-graph's 15 s, counted all the same, would stop it at its first
    // frame once shown, every node still where it started (a packed disc).
    .d3AlphaDecay(0.028)
    .d3AlphaMin(0.001)
    .cooldownTime(Infinity)
    // The design's own layout engine (layout.ts) moves the nodes, tick for
    // tick, as the engine coasts: d3's forces are gone.
    .onEngineTick(() => {
      engine?.tick();
    })
    .onNodeHover((node) => {
      pointed = node;
      events.hover(node);
    })
    .onNodeClick((node) => events.click(node))
    .onBackgroundClick(() => events.click(null))
    .onRenderFramePost((ctx, scale) => {
      ctx.globalCompositeOperation = "source-over";
      // The islands' names first: they take the places, and a node's own label
      // gives way to them, since far off the island is what can still be read.
      islands = drawIslandNames(graph.graphData().nodes, grouped, named, islandInk, ctx, scale, font, mono);
      // While the islands name themselves, no root names itself under its node:
      // the island's name already says what the root is, and sixty roots saying
      // it again is the wall of text the naming was meant to replace. What a
      // focus lights still names itself, since that is what was asked for.
      const naming = grouped !== null && named !== null && scale < ISLAND_NAME_ZOOM;
      drawLabels(labelled, ctx, scale, font, islands.map(({ box }) => box), naming);
    })
    .onEngineStop(() => {
      settling = false;
      if (frameWhenSettled) frame(framed);
      frameWhenSettled = false;
    });
  // The canvas's size as laid out now: a panel that opened or narrowed in the same
  // render has moved its edge, and the resize observer hears of it only later.
  const fitHost = () => graph.width(host.clientWidth).height(host.clientHeight);
  const resize = new ResizeObserver(() => fitHost());
  resize.observe(host);
  // On the next frame, as force-graph's own clicks are: it learns what the pointer is over as it draws one.
  const onDoubleClick = () =>
    requestAnimationFrame(() => {
      if (pointed) events.doubleClick(pointed);
    });
  host.addEventListener("dblclick", onDoubleClick);
  // An island's name is canvas-drawn, so no element takes its click: the host
  // hears it first (capture, before force-graph's own canvas listeners) and a
  // hit on a name takes the click from the background the name is drawn over.
  const islandAt = (x: number, y: number): string | null => {
    const at = graph.screen2GraphCoords(x, y);
    for (const { key, box } of islands) {
      if (at.x >= box.left && at.x <= box.right && at.y >= box.top && at.y <= box.bottom) return key;
    }
    return null;
  };
  const hostAt = (clientX: number, clientY: number): { x: number; y: number } => {
    const rect = host.getBoundingClientRect();
    return { x: clientX - rect.left, y: clientY - rect.top };
  };
  const onClick = (event: MouseEvent) => {
    if (event.button !== 0) return;
    const at = hostAt(event.clientX, event.clientY);
    const key = islandAt(at.x, at.y);
    if (key === null) return;
    event.stopPropagation();
    event.preventDefault();
    events.island(key);
  };
  const onPointerMove = (event: PointerEvent) => {
    if (pointed) return;
    const at = hostAt(event.clientX, event.clientY);
    host.style.cursor = islandAt(at.x, at.y) === null ? "" : "pointer";
  };
  host.addEventListener("click", onClick, true);
  host.addEventListener("pointermove", onPointerMove);
  const relabel = () => {
    labelled = graph
      .graphData()
      .nodes.filter((node) => node.look.label !== null)
      .sort((a, b) => b.degree - a.degree);
  };
  const frame = (filter: (node: DrawNode) => boolean) => {
    fitHost();
    const bounds = boundsOf(graph.graphData().nodes, filter);
    if (!bounds) return;
    const { left, right, top, bottom } = bounds;
    const over = covered();
    const [width, height] = [graph.width() - over.right, graph.height()];
    // A padding to suit what is left: beside Peek and a list, a narrow canvas can be under 200 px wide.
    const padding = Math.min(FIT_PADDING_PX, width / 8, height / 8);
    const zoom = Math.min(
      FIT_MAX_ZOOM,
      (width - 2 * padding) / Math.max(right - left, 1),
      (height - 2 * padding) / Math.max(bottom - top, 1),
    );
    graph.centerAt((left + right) / 2 + over.right / 2 / zoom, (top + bottom) / 2, FIT_MS);
    graph.zoom(zoom, FIT_MS);
  };
  return {
    setData(nodes, links, frameAfter) {
      if (frameAfter) {
        framed = frameAfter;
        frameWhenSettled = true;
      }
      settling = true;
      graph.graphData({ nodes: [...nodes], links: [...links] });
      relabel();
    },
    repaint() {
      relabel();
      // Setting an accessor asks for a frame; once the layout has settled,
      // force-graph draws nothing until something does.
      graph.nodeVisibility(graph.nodeVisibility()).linkVisibility(graph.linkVisibility());
    },
    fit(filter = everything) {
      framed = filter;
      frame(filter);
      frameWhenSettled = settling;
    },
    refit() {
      frame(framed);
    },
    zoomBy(factor) {
      graph.zoom(graph.zoom() * factor, ZOOM_MS);
    },
    nameIslands(name, ink) {
      named = name;
      islandInk = ink;
      this.repaint();
    },
    group(groupOf) {
      grouped = groupOf;
      const { nodes, links } = graph.graphData();
      const index = new Map(nodes.map((node, position) => [node, position] as const));
      // A new grouping lays the graph out anew; the same grouping (a live
      // update, a replay step) keeps it, and only the nodes that have nowhere
      // yet take a place, beside their community's others.
      const signature = grouped ? [...new Set(nodes.map((node) => grouped!(node) ?? ""))].sort().join("|") : "";
      if (signature !== lastGrouping) {
        lastGrouping = signature;
        engine = layout({
          nodes,
          communityOf: (node) => grouped?.(node) ?? WHOLE,
          links: links.flatMap((link) =>
            typeof link.source === "string" || typeof link.target === "string"
              ? []
              : [{ source: index.get(link.source as DrawNode)!, target: index.get(link.target as DrawNode)!, kind: link.kind }],
          ),
          radiusOf: (node) => node.look.radius,
        });
      } else {
        const communities = new Map<string, { x: number; y: number; n: number }>();
        nodes.forEach((node, position) => {
          if (Number.isFinite(node.x) && Number.isFinite(node.y)) {
            const key = grouped?.(node) ?? WHOLE;
            const at = communities.get(key) ?? { x: 0, y: 0, n: 0 };
            at.x += node.x!;
            at.y += node.y!;
            at.n += 1;
            communities.set(key, at);
          }
          void position;
        });
        nodes.forEach((node) => {
          if (Number.isFinite(node.x) && Number.isFinite(node.y)) return;
          const at = communities.get(grouped?.(node) ?? WHOLE);
          const angle = Math.random() * Math.PI * 2;
          const distance = 24 + Math.random() * 24;
          node.x = (at && at.n > 0 ? at.x / at.n : 0) + Math.cos(angle) * distance;
          node.y = (at && at.n > 0 ? at.y / at.n : 0) + Math.sin(angle) * distance;
        });
      }
      // The islands form as the layout settles: frame then what was asked for
      // last. Framing every node instead would undo a focus's frame or a root's.
      frameWhenSettled = true;
      settling = true;
      graph.d3ReheatSimulation();
    },
    centre(node) {
      // The user picked a node: a frame waiting for the layout to settle would move the view off it.
      frameWhenSettled = false;
      fitHost();
      const over = covered();
      const zoom = Math.max(graph.zoom(), CLOSE_ZOOM);
      graph.centerAt((node.x ?? 0) + over.right / 2 / zoom, node.y ?? 0, MOVE_MS);
      graph.zoom(zoom, MOVE_MS);
    },
    screenOf(node) {
      return graph.graph2ScreenCoords(node.x ?? 0, node.y ?? 0);
    },
    dispose() {
      resize.disconnect();
      host.removeEventListener("dblclick", onDoubleClick);
      host.removeEventListener("click", onClick, true);
      host.removeEventListener("pointermove", onPointerMove);
      graph._destructor();
      host.replaceChildren();
    },
  };
};

/**
 * The box around the drawn, placed nodes `filter` keeps, or null for none. A
 * loop: spread into `Math.min`, 150,000 places overflowed the call stack.
 */
function boundsOf(
  nodes: readonly DrawNode[],
  filter: (node: DrawNode) => boolean,
): { left: number; right: number; top: number; bottom: number } | null {
  let [left, right, top, bottom] = [Infinity, -Infinity, Infinity, -Infinity];
  for (const node of nodes) {
    if (node.hidden || !Number.isFinite(node.x) || !Number.isFinite(node.y) || !filter(node)) continue;
    left = Math.min(left, node.x!);
    right = Math.max(right, node.x!);
    top = Math.min(top, node.y!);
    bottom = Math.max(bottom, node.y!);
  }
  return left <= right ? { left, right, top, bottom } : null;
}

/** A disc of the node's fill, its wash, its ring and its dot, and its halo (`NodeLook`). */
function drawNode(node: DrawNode, ctx: CanvasRenderingContext2D, scale: number): void {
  const { look } = node;
  if (look.halo) {
    ctx.beginPath();
    ctx.arc(node.x!, node.y!, look.radius + HALO_GAP_PX / scale, 0, 2 * Math.PI);
    ctx.lineWidth = 2 / scale;
    ctx.strokeStyle = look.halo;
    ctx.stroke();
  }
  ctx.beginPath();
  ctx.arc(node.x!, node.y!, look.radius, 0, 2 * Math.PI);
  if (look.fillAlpha > 0) {
    ctx.globalAlpha = look.fillAlpha;
    ctx.fillStyle = fillAt(look, scale);
    ctx.fill();
  }
  if (look.wash) {
    ctx.globalAlpha = look.washAlpha;
    ctx.fillStyle = look.wash;
    ctx.fill();
  }
  ctx.globalAlpha = look.ringAlpha;
  ctx.lineWidth = 1.5 / scale;
  // A disproven Belief is drawn as its fill's hollow ring, so that ring follows
  // the fill into the kind's hue; every other ring is a status colour and stands.
  ctx.strokeStyle = look.ring === look.fill ? fillAt(look, scale) : look.ring;
  ctx.stroke();
  if (look.dot) {
    ctx.beginPath();
    ctx.arc(node.x!, node.y!, look.radius * 0.4, 0, 2 * Math.PI);
    ctx.fillStyle = look.dot;
    ctx.fill();
  }
  ctx.globalAlpha = 1;
}

/**
 * Each of `nodes`' labels, centred under its disc in `font`, the same size on
 * screen at any zoom and outlined so edges never cross it; in order, leaving
 * out any that would overlap one drawn before it. A node whose id is in
 * `namedIslands` stays wordless: its island's name says what it is, and twice
 * would be clutter.
 */
function drawLabels(
  nodes: readonly DrawNode[],
  ctx: CanvasRenderingContext2D,
  scale: number,
  font: string,
  reserved: readonly Box[],
  hideRoots: boolean,
): void {
  const height = LABEL_PX / scale;
  ctx.font = `600 ${height}px ${font}`;
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  ctx.lineJoin = "round";
  ctx.lineWidth = 3 / scale;
  const taken: { left: number; right: number; top: number; bottom: number }[] = [...reserved];
  for (const node of nodes) {
    const label = node.look.label;
    if (!label || node.hidden || (hideRoots && node.root) || !Number.isFinite(node.x) || !Number.isFinite(node.y)) continue;
    const top = node.y! + node.look.radius + LABEL_GAP_PX / scale;
    const half = ctx.measureText(label.text).width / 2;
    const box = { left: node.x! - half, right: node.x! + half, top, bottom: top + height };
    if (taken.some((other) => box.left < other.right && other.left < box.right && box.top < other.bottom && other.top < box.bottom)) continue;
    taken.push(box);
    ctx.strokeStyle = label.outline;
    ctx.strokeText(label.text, node.x!, top);
    ctx.fillStyle = label.color;
    ctx.fillText(label.text, node.x!, top);
  }
}

/**
 * Each island's name over it, with what it holds under that, and the boxes they
 * took so a node's label gives way to them.
 *
 * They draw only below `ISLAND_NAME_ZOOM` and fade over the last of it, since
 * once the view is close enough to read the nodes themselves an island's name is
 * a title over a page already turned. Nothing draws while the graph is laid out
 * whole, or with no names to give.
 */
function drawIslandNames(
  nodes: readonly DrawNode[],
  groupOf: ((node: DrawNode) => string | undefined) | null,
  name: ((key: string) => string | undefined) | null,
  ink: IslandInk,
  ctx: CanvasRenderingContext2D,
  scale: number,
  font: string,
  mono: string,
): { key: string; box: Box }[] {
  if (!groupOf || !name || scale >= ISLAND_NAME_ZOOM) return [];
  const titlePx = ISLAND_TITLE_PX / scale;
  const countPx = ISLAND_COUNT_PX / scale;
  const gap = ISLAND_GAP_PX / scale;
  const titles = new Map<string, string>();
  const islands = islandsOf(nodes, (node) => {
    const key = groupOf(node as DrawNode);
    if (key === undefined) return undefined;
    if (!titles.has(key)) {
      const title = name(key);
      if (title === undefined) return undefined;
      titles.set(key, title);
    }
    return key;
  })
    .filter((island) => island.shown >= ISLAND_MIN_NODES)
    // Only the largest islands are named. A graph of a thousand nodes can hold
    // sixty roots, most of them a handful of records, and naming every one buries
    // the threads worth reading under the ones that are barely threads at all.
    .sort((a, b) => b.shown - a.shown)
    .slice(0, ISLAND_NAME_MAX);
  if (islands.length === 0) return [];
  const countOf = (island: Island) =>
    `${island.shown.toLocaleString("en-US")} records, ${island.beliefs.toLocaleString("en-US")} beliefs`;
  const measure = (text: string, weight: string, px: number, family: string) => {
    ctx.font = `${weight} ${px}px ${family}`;
    return ctx.measureText(text).width;
  };
  const placements = placeLabels(
    islands,
    (island) =>
      Math.max(
        measure(titles.get(island.key)!, "500", titlePx, font),
        measure(countOf(island), "400", countPx, mono),
      ),
    { height: titlePx + countPx + countPx * 0.3, gap },
  ).filter(({ placed }) => placed);
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  ctx.lineJoin = "round";
  ctx.lineWidth = 4 / scale;
  // Over the last fifth of their range the names fade, rather than blink out.
  ctx.globalAlpha = Math.min(1, (ISLAND_NAME_ZOOM - scale) / (ISLAND_NAME_ZOOM * ISLAND_FADE_SHARE));
  for (const { island, x, y } of placements) {
    ctx.strokeStyle = ink.outline;
    ctx.font = `500 ${titlePx}px ${font}`;
    ctx.strokeText(titles.get(island.key)!, x, y);
    ctx.fillStyle = ink.title;
    ctx.fillText(titles.get(island.key)!, x, y);
    ctx.font = `400 ${countPx}px ${mono}`;
    const under = y + titlePx + countPx * 0.3;
    ctx.strokeText(countOf(island), x, under);
    ctx.fillStyle = ink.count;
    ctx.fillText(countOf(island), x, under);
  }
  ctx.globalAlpha = 1;
  return placements.map(({ island, box }) => ({ key: island.key, box }));
}

/**
 * One edge, stroked alone, curved as the design curves them, in its kind's own
 * quiet share of the light, added — the design draws its edges with `lighter`,
 * so crossings glow rather than block. force-graph would stroke all edges of
 * one colour, width and dash as one path, and a wide stroke over one long path
 * rasterises slower than one per edge: in four paired runs of 1,000 nodes on a
 * loaded Mac (2026-10-01), 24-32 fps bundled with 50-58 ms tasks, 32-34 fps
 * alone with none.
 */
function drawLink(link: DrawLink, ctx: CanvasRenderingContext2D, scale: number): void {
  const { source, target, look, kind, valence } = link;
  // The layout swaps a link's ends for their nodes before the first frame.
  if (typeof source === "string" || typeof target === "string") return;
  ctx.beginPath();
  const [ax, ay] = [source.x!, source.y!];
  const [bx, by] = [target.x!, target.y!];
  // The design's quadratic: a bow to the side of the straight line, a tenth of
  // the way round, so bundles of edges read as strands and not a rope.
  const [mx, my] = [(ax + bx) / 2 - (by - ay) * 0.12, (ay + by) / 2 + (bx - ax) * 0.12];
  ctx.moveTo(ax, ay);
  ctx.quadraticCurveTo(mx, my, bx, by);
  // The design's shares: the evidence bright, structure quiet, and by the zoom
  // an island's name draws at, a hairline at most — the islands carry the far
  // view, and the wires get out of their way.
  const base =
    valence !== undefined
      ? Math.abs(valence) >= 0
        ? kind === "proves"
          ? valence < 0
            ? 0.75
            : 0.72
          : valence < 0
            ? 0.48
            : 0.45
        : 0.3
      : (QUIET[kind] ?? 0.3);
  const width = Math.max(0.55, Math.min(1, 0.45 + scale * 0.3)) / scale;
  const bright = 0.75 + 0.45 * Math.min(1, Math.max(0, (scale - 0.5) / 1.2));
  const far = Math.min(1, Math.max(0, (DUO_ZOOM - scale) / FAR_RANGE));
  ctx.globalCompositeOperation = "lighter";
  ctx.globalAlpha = Math.min(1, base * bright) * (1 - far * FAR_DIM);
  ctx.lineWidth = (valence !== undefined ? width * 1.3 : width);
  ctx.strokeStyle = look.color;
  ctx.setLineDash(kind === "favors" ? [3 / scale, 3 / scale] : NO_DASH);
  ctx.stroke();
  ctx.globalCompositeOperation = "source-over";
  ctx.globalAlpha = 1;
  ctx.setLineDash(NO_DASH);
}

/** The node's disc in its hit colour, so pointing and dragging match what is drawn. */
function paintHitArea(node: DrawNode, color: string, ctx: CanvasRenderingContext2D): void {
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(node.x!, node.y!, node.look.radius, 0, 2 * Math.PI);
  ctx.fill();
}

const NO_DASH: number[] = [];
const everything = () => true;
/** The community every node belongs to while the graph is laid out whole: one ball. */
const WHOLE = "whole";
/** Structure edges' shares of the light, by kind: the design's quiet wireweights. */
const QUIET: { [kind: string]: number } = { narrows: 0.3, requires: 0.3, produced_by: 0.26, supersedes: 0.4, cites_paper: 0.18 };
const FIT_MS = 400;
/** A fit to one node, or a few close together, stops here rather than fill the canvas with them. */
const FIT_MAX_ZOOM = 4;
const ZOOM_MS = 200;
/** The halo's gap from the disc, and the label's, in screen pixels. */
const HALO_GAP_PX = 3.5;
const LABEL_GAP_PX = 3;
const LABEL_PX = 11;
/** v1's jump to a node: 600 ms to at least 2.5x. */
const MOVE_MS = 600;
const CLOSE_ZOOM = 2.5;
const FIT_PADDING_PX = 70;
/** Below this zoom an island's name draws over it, and the nodes' tints are the duo's (`encode.ts`'s `DUO_ZOOM`). */
export const ISLAND_NAME_ZOOM = 0.9;
/** Over the last share of the way to `ISLAND_NAME_ZOOM` the names fade, rather than blink out. */
const ISLAND_FADE_SHARE = 0.25;
/** An island's name, in screen pixels: the title, and under it what the island holds. */
const ISLAND_TITLE_PX = 12;
const ISLAND_COUNT_PX = 10.5;
/** The clear space a name keeps from its island and its fellows, in screen pixels. */
const ISLAND_GAP_PX = 16;
/** An island with fewer drawn nodes than this goes unnamed: its name would name a speck. */
const ISLAND_MIN_NODES = 12;
/** The most islands named at once, largest first: beyond this the names crowd out the graph. */
const ISLAND_NAME_MAX = 16;
/** How far below `ISLAND_NAME_ZOOM` the edges' fade takes to reach its full. */
const FAR_RANGE = 0.3;
/** How much of itself an edge keeps at the fade's full: the far view's whisper. */
const FAR_DIM = 0.4;

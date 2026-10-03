// The only module that imports force-graph (https://github.com/vasturiano/force-graph),
// so it loads with the graph's chunk, and the view's tests replace it: jsdom has no
// canvas.
import ForceGraph from "force-graph";
import { clusterForce } from "./clusters";
import type { DrawLink, DrawNode } from "./model";

/** What covers the canvas, in pixels: a strip on its right (Peek, the key or the zoom buttons). */
export type Covered = { readonly right: number };

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
  const font = getComputedStyle(host).fontFamily;
  const graph = new ForceGraph<DrawNode, DrawLink>(host)
    .width(host.clientWidth)
    .height(host.clientHeight)
    .nodeVisibility((node) => !node.hidden)
    // The size force-graph reckons a node at (the square root of this times
    // its `nodeRelSize`, 4) places arrowheads on the rim and pads the fit.
    .nodeVal((node) => (node.look.radius / 4) ** 2)
    .nodeCanvasObject(drawNode)
    .nodePointerAreaPaint(paintHitArea)
    .linkVisibility((link) => !link.hidden)
    .linkCanvasObject(drawLink)
    // The arrowheads' colour; force-graph draws them, and `drawLink` the lines.
    .linkColor((link) => link.look.color)
    .linkDirectionalArrowLength(4)
    .linkDirectionalArrowRelPos(1)
    .d3VelocityDecay(0.58)
    // The layout stops once it has cooled, about 170 ticks, rather than after
    // force-graph's 15 s: it is framed then, and an idle canvas costs nothing.
    // Never by the clock: a tab opened in the background gets no frames, and
    // force-graph's 15 s, counted all the same, would stop it at its first
    // frame once shown, every node still where it started (a packed disc).
    .d3AlphaDecay(0.04)
    .d3AlphaMin(0.001)
    .cooldownTime(Infinity)
    .onNodeHover((node) => {
      pointed = node;
      events.hover(node);
    })
    .onNodeClick((node) => events.click(node))
    .onBackgroundClick(() => events.click(null))
    .onRenderFramePost((ctx, scale) => drawLabels(labelled, ctx, scale, font))
    .onEngineStop(() => {
      settling = false;
      if (frameWhenSettled) frame(framed);
      frameWhenSettled = false;
    });
  graph.d3Force("charge")?.strength(-70).distanceMax(280);
  graph.d3Force("link")?.distance(55).strength(LINK_STRENGTH);
  graph.d3Force("gravity", gravity(0.02));
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
    group(groupOf) {
      graph.d3Force("cluster", groupOf ? clusterForce<DrawNode>(groupOf) : null);
      // Links between islands pull faintly, so each island holds together: with the
      // full 0.6 throughout, 7 islands overlapped on production's graph, against 2.
      graph
        .d3Force("link")
        ?.strength((link: DrawLink) =>
          !groupOf || groupOf(link.source as DrawNode) === groupOf(link.target as DrawNode) ? LINK_STRENGTH : CROSS_ISLAND_STRENGTH,
        );
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
    ctx.fillStyle = look.fill;
    ctx.fill();
  }
  if (look.wash) {
    ctx.globalAlpha = look.washAlpha;
    ctx.fillStyle = look.wash;
    ctx.fill();
  }
  ctx.globalAlpha = look.ringAlpha;
  ctx.lineWidth = 1.5 / scale;
  ctx.strokeStyle = look.ring;
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
 * out any that would overlap one drawn before it.
 */
function drawLabels(nodes: readonly DrawNode[], ctx: CanvasRenderingContext2D, scale: number, font: string): void {
  const height = LABEL_PX / scale;
  ctx.font = `600 ${height}px ${font}`;
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  ctx.lineJoin = "round";
  ctx.lineWidth = 3 / scale;
  const taken: { left: number; right: number; top: number; bottom: number }[] = [];
  for (const node of nodes) {
    const label = node.look.label;
    if (!label || node.hidden || !Number.isFinite(node.x) || !Number.isFinite(node.y)) continue;
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
 * One edge, stroked alone. force-graph would stroke all edges of one colour,
 * width and dash as one path, and a wide stroke over one long path rasterises
 * slower than one per edge: in four paired runs of 1,000 nodes on a loaded Mac
 * (2026-10-01), 24-32 fps bundled with 50-58 ms tasks, 32-34 fps alone with none.
 */
function drawLink(link: DrawLink, ctx: CanvasRenderingContext2D, scale: number): void {
  const { source, target, look } = link;
  // The layout swaps a link's ends for their nodes before the first frame.
  if (typeof source === "string" || typeof target === "string") return;
  ctx.beginPath();
  ctx.moveTo(source.x!, source.y!);
  ctx.lineTo(target.x!, target.y!);
  ctx.lineWidth = look.width / scale;
  ctx.strokeStyle = look.color;
  ctx.setLineDash(look.dash ?? NO_DASH);
  ctx.stroke();
}

/** The node's disc in its hit colour, so pointing and dragging match what is drawn. */
function paintHitArea(node: DrawNode, color: string, ctx: CanvasRenderingContext2D): void {
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(node.x!, node.y!, node.look.radius, 0, 2 * Math.PI);
  ctx.fill();
}

/**
 * A pull of every node toward the middle, `strength` times the layout's heat:
 * too weak to crowd a cluster, enough to keep unlinked pieces from drifting off.
 * d3's `forceX` and `forceY` would do, but force-graph does not export them.
 */
function gravity(strength: number) {
  let nodes: DrawNode[] = [];
  return Object.assign(
    (alpha: number) => {
      for (const node of nodes) {
        node.vx = (node.vx ?? 0) - (node.x ?? 0) * strength * alpha;
        node.vy = (node.vy ?? 0) - (node.y ?? 0) * strength * alpha;
      }
    },
    { initialize: (all: DrawNode[]) => void (nodes = all) },
  );
}

const NO_DASH: number[] = [];
const everything = () => true;
const LINK_STRENGTH = 0.6;
/** Measured best by the grouping's design: 988 of 997 nodes sat nearest their own island, against 966 at 0.6. */
const CROSS_ISLAND_STRENGTH = 0.05;
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

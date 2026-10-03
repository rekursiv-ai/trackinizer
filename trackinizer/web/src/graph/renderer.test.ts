import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { type DrawNode, GraphModel } from "./model";
import { forceGraphRenderer, type Renderer } from "./renderer";
import { node, PALETTE } from "./testing";

// jsdom has no canvas: force-graph is replaced by one that keeps what the
// renderer gives it and where it puts the camera.
const { FakeForceGraph } = vi.hoisted(() => {
  /** A d3 force's settings, each of which chains. */
  const FORCE = { strength: () => FORCE, distance: () => FORCE, distanceMax: () => FORCE };
  /** Settings the renderer makes once and these tests never read; each chains. */
  const CHAINED = [
    "nodeVisibility",
    "nodeVal",
    "nodeCanvasObject",
    "nodePointerAreaPaint",
    "linkVisibility",
    "linkCanvasObject",
    "linkColor",
    "linkDirectionalArrowLength",
    "linkDirectionalArrowRelPos",
    "d3VelocityDecay",
    "d3AlphaDecay",
    "d3AlphaMin",
    "onNodeHover",
    "onNodeClick",
    "onBackgroundClick",
    "onRenderFramePost",
    "d3ReheatSimulation",
    "_destructor",
  ];

  /** force-graph on an 800 by 600 canvas, as far as the renderer drives it. */
  class FakeForceGraph {
    static last: FakeForceGraph | null = null;
    data: { nodes: unknown[]; links: unknown[] } = { nodes: [], links: [] };
    /** Where the camera was last centred, in graph coordinates. */
    centre: readonly [number, number] | null = null;
    scale = 1;
    /** How long force-graph may run its engine, by the clock, in ms; its default is 15 s. */
    cooling = 15_000;
    #engineStop = () => {};

    constructor() {
      for (const name of CHAINED) Reflect.set(this, name, () => this);
      FakeForceGraph.last = this;
    }

    /** The layout settles, as force-graph says once its engine stops. */
    settle(): void {
      this.#engineStop();
    }

    graphData(data?: { nodes: unknown[]; links: unknown[] }) {
      if (!data) return this.data;
      this.data = data;
      return this;
    }

    width(value?: number) {
      return value === undefined ? 800 : this;
    }

    height(value?: number) {
      return value === undefined ? 600 : this;
    }

    zoom(scale?: number) {
      if (scale === undefined) return this.scale;
      this.scale = scale;
      return this;
    }

    centerAt(x: number, y: number) {
      this.centre = [x, y];
      return this;
    }

    cooldownTime(ms: number) {
      this.cooling = ms;
      return this;
    }

    onEngineStop(hook: () => void) {
      this.#engineStop = hook;
      return this;
    }

    d3Force(_name: string, force?: unknown) {
      return force === undefined ? FORCE : this;
    }
  }
  return { FakeForceGraph };
});
vi.mock("force-graph", () => ({ default: FakeForceGraph }));

let renderer: Renderer;

beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  renderer = forceGraphRenderer(document.createElement("div"), { hover: () => {}, click: () => {}, doubleClick: () => {} }, () => ({ right: 0 }));
});

afterEach(() => {
  renderer.dispose();
  vi.unstubAllGlobals();
});

/** The graph's camera. */
function graph(): InstanceType<typeof FakeForceGraph> {
  return FakeForceGraph.last!;
}

/** Drawn nodes at `places`, numbered from 1. */
function placed(places: readonly (readonly [number, number])[]): DrawNode[] {
  const model = new GraphModel(PALETTE);
  model.apply({ nodes: places.map((_, n) => node(n + 1)), edges: [] });
  return model.nodes.map((drawn, n) => Object.assign(drawn, { x: places[n]![0], y: places[n]![1] }));
}

test("the layout stops by cooling alone, never by the clock: a tab opened in the background, given no frames for 15 s, still lays out once shown", () => {
  expect(graph().cooling).toBe(Infinity);
});

test("a fit frames 150,000 nodes, more than a call can take as arguments", () => {
  const [first] = placed([[0, 0]]);
  const nodes = Array.from({ length: 150_000 }, (_, n) => ({ ...first!, id: String(n), x: n, y: -n }));
  renderer.setData(nodes, [], null);
  renderer.fit();
  expect(graph().centre).toEqual([149_999 / 2, -149_999 / 2]);
});

test("grouping keeps what the view asked to frame: a focus's nodes, not every node, are framed once the islands settle", () => {
  const nodes = placed([
    [0, 0],
    [10, 0],
    [1_000, 1_000],
  ]);
  const near = (drawn: DrawNode) => drawn.x! < 100;
  renderer.setData(nodes, [], () => true);
  renderer.fit(near);
  renderer.group(null);
  graph().settle();
  expect([graph().centre, graph().scale]).toEqual([[5, 0], 4]);
  // Grouping a settled graph frames it again once the islands form, as last asked.
  renderer.group(() => "island");
  graph().centerAt(0, 0);
  graph().settle();
  expect(graph().centre).toEqual([5, 0]);
});

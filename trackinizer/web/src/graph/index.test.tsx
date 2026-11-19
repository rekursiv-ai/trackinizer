import { type QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement, ReactNode } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { ALL_NODES, type Graph, type GraphEdge, type GraphNode } from "../api/graph";
import { type Sent, stubFetch } from "../api/testing";
import { MetaContext } from "../app/boot";
import { HighlightContext, HighlightStore } from "../app/highlights";
import { CommandRegistry, CommandRegistryContext, Shortcuts } from "../commands/registry";
import { LiveContext } from "../live";
import { LiveHub } from "../live/hub";
import { testClient } from "../live/testing";
import { RouterProvider } from "../router/router";
import type { Panel } from "../ui/panel";
import { setChatHidden } from "../visuals/chatColumnState";
import { type WorkspaceActions, WorkspaceActionsProvider } from "../visuals/workspaceActions";
import { GraphView } from ".";
import { graphQuery } from "./live";
import { edge, FakeRenderer, META, node, uuid } from "./testing";

// The list's Peek renders a whole detail; this one renders the links a detail
// has: to the row itself (Open), to rows the graph holds, and to one it lacks,
// and Peek's collapse button, saying whether it is collapsed.
vi.mock("../ui/Peek", async (importOriginal) => {
  const { PanelToggle } = await import("../ui/panel");
  return {
    ...(await importOriginal<typeof import("../ui/Peek")>()),
    Peek: ({ row, panel, onClose, body, foot, tools }: { row: { kind: string; seq: number }; panel: Panel; onClose: () => void; body?: ReactNode; foot?: ReactNode; tools?: ReactNode }) => (
      <aside id="peek" aria-label="Peek" data-collapsed={panel.collapsed}>
        {tools}
        {body}
        {/* The real Peek draws the caller's foot under the body; the graph puts its
            record commands there, so a test that presses one needs it rendered. */}
        {foot}
        <h2>
          {row.kind}#{row.seq}
        </h2>
        <a href={`#/ref/${row.kind}/${row.seq}`}>Open</a>
        <a href="#/ref/Issue/1">Parent by ref</a>
        <a href="#/lookup/00000000-0000-4000-8000-000000000003">Paper by id</a>
        <a href="#/ref/Issue/99">Not drawn</a>
        <PanelToggle panel={panel} controls="peek" />
        <button type="button" onClick={onClose}>
          Close peek
        </button>
      </aside>
    ),
  };
});

// The record reads `/api/web/get` and the schema's field tables with it; what the
// graph's own tests follow is which record the panel holds, not what it says. A
// thread's briefing mounts the same two blocks for its root, so it stubs them too.
vi.mock("./GraphRecord", () => ({
  GraphRecord: ({ node }: { node: { kind: string; seq: number } }) => <p>{`${node.kind}#${node.seq} record`}</p>,
  RecordProse: ({ node }: { node: { kind: string; seq: number } }) => <p>{`${node.kind}#${node.seq} summary`}</p>,
  RecordTail: ({ node }: { node: { kind: string; seq: number } }) => <p>{`${node.kind}#${node.seq} tail`}</p>,
}));

let client: QueryClient;
let renderer: FakeRenderer;
let highlights: HighlightStore;
let sent: Sent[];
/** What the server answers for each limit. */
let answers: Map<number, Graph | Promise<Response>>;
let tree: () => ReactElement;
let view: ReturnType<typeof render>;

const GRAPH: Graph = {
  nodes: [node(1), node(2), node(3, { kind: "Paper", title: "A paper" })],
  edges: [edge(2, 1), edge(3, 2, "favors", { valence: 0.5 })],
};

beforeEach(() => {
  client = testClient();
  renderer = new FakeRenderer();
  highlights = new HighlightStore();
  answers = new Map([[1000, GRAPH]]);
  history.replaceState(null, "", "#/graph?group=none");
  // The halos' colours, named for what they mark; jsdom has no stylesheet.
  document.documentElement.style.setProperty("--text", "halo");
  document.documentElement.style.setProperty("--accent-hover", "match");
  document.documentElement.style.setProperty("--amber", "highlight");
  // jsdom has none; the roots list scrolls its marked row into view.
  Element.prototype.scrollIntoView = () => {};
  // Nor pointer capture; the timeline's scrub takes it on the way down.
  Element.prototype.setPointerCapture = () => {};
  Element.prototype.releasePointerCapture = () => {};
  sent = stubFetch(async (request) => {
    const answer = answers.get(Number(new URL(request.url).searchParams.get("limit")))!;
    return answer instanceof Promise ? answer : Response.json(answer);
  });
});

afterEach(() => {
  cleanup();
  // Chat's folded state is one module's, shared by every test that mounts the
  // shell: a full-view test that left it folded would follow the next file in.
  setChatHidden(false);
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  sessionStorage.clear();
  document.documentElement.removeAttribute("style");
  document.documentElement.removeAttribute("data-theme");
  Reflect.deleteProperty(window, "trackinizer");
  history.replaceState(null, "", "/");
  vi.useRealTimers();
});

function show({ hub = null, canvas = null }: { hub?: LiveHub | null; canvas?: WorkspaceActions | null } = {}) {
  tree = () => (
    <QueryClientProvider client={client}>
      <MetaContext value={META}>
        <RouterProvider kinds={META.kinds}>
          <LiveContext value={hub}>
            <CommandRegistryContext value={new CommandRegistry()}>
              <Shortcuts />
              <WorkspaceActionsProvider value={canvas}>
                <HighlightContext value={highlights}>
                  <GraphView createRenderer={renderer.create} />
                </HighlightContext>
              </WorkspaceActionsProvider>
            </CommandRegistryContext>
          </LiveContext>
        </RouterProvider>
      </MetaContext>
    </QueryClientProvider>
  );
  view = render(tree());
  return view;
}

/** Re-read the canvas, as the tab's poll does after an agent writes to it. */

/**
 * The canvas the graph shares its selection with. `asked` and `hidden` are what
 * it was told to show and take away; `send` moves it the way an agent's operation
 * does, and the state it holds answers to the graph's reads.
 */
/** A canvas that records the shows a view sends it. */
function canvasStub() {
  const asked: string[] = [];
  const actions: WorkspaceActions = {
    busy: false,
    writeError: null,
    operate: (operation) => {
      if (operation.kind === "show" && operation.record_id) asked.push(operation.record_id);
    },
    visualTypes: new Set(["trax.record"]),
  };
  return { actions, asked };
}

/** Show the graph and wait for the count its first answer gives. */
async function shown(count = "3 nodes, 2 edges") {
  // Each user here sends its inputs back to back. By default user-event waits a
  // timer turn between them, some 20 ms a test, while React has already applied
  // each input before the next, as Testing Library wraps every event in act.
  const user = userEvent.setup({ delay: null });
  const view = show();
  await screen.findByText(count);
  return { user, ...view };
}

type User = ReturnType<typeof userEvent.setup>;
/** Keep a tab state whose key shows; the view starts with it hidden. */
const keepKey = (over: Record<string, unknown> = {}) =>
  sessionStorage.setItem(
    "trackinizer.v2.graph",
    JSON.stringify({ limit: 1000, hiddenKinds: [], hiddenStatuses: [], skipEdges: [], only: false, key: true, roots: true, ...over }),
  );
const limits = () => sent.map((request) => new URLSearchParams(request.query).get("limit"));
const count = () => document.querySelector(".proto-stat")?.textContent;
const label = (option: Element) => option.querySelector(".lbl")?.textContent;
/** Open the menu behind the button named `name`; returns its options' labels. */
async function openMenu(user: User, name: string | RegExp) {
  // By its text: a query by role computes the name of every button in the view.
  await user.click(screen.getByText(name, { selector: "button" }));
  return screen.getAllByRole("option").map(label);
}
const pick = (user: User, name: string) => user.click(screen.getAllByRole("option").find((option) => label(option) === name)!);

test("the graph draws the newest 1,000 inquiries, framed once settled, with its count in the tools and no native select (S1, S2, S13)", async () => {
  const { container } = await shown();
  expect(limits()).toEqual(["1000"]);
  expect(renderer.shown()).toEqual(["Issue 1", "Issue 2", "A paper"]);
  expect(renderer.links.map((link) => link.kind)).toEqual(["narrows", "favors"]);
  expect(renderer.sets).toEqual([true]);
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Graph");
  expect(container.querySelector(".view-h .graph-count")).toBeNull();
  expect(container.querySelector(".view-tools .proto-stat")?.textContent).toBe("3 nodes, 2 edges");
  expect(container.querySelector("select")).toBeNull();
});

test("the count in the tools is the wires the field draws: a produced_by that only companions a narrows counts twice over", async () => {
  answers.set(1000, { nodes: GRAPH.nodes, edges: [...GRAPH.edges, edge(2, 1, "produced_by")] });
  const { container } = await shown("3 nodes, 2 edges");
  expect(renderer.links.map((link) => link.kind)).toEqual(["narrows", "favors"]);
  expect(container.querySelector(".view-tools .proto-stat")?.textContent).toBe("3 nodes, 2 edges");
});

const key = () => screen.queryByRole("complementary", { name: "Key" });
/** The key's items in its list headed `heading`, as text. */
const keyItems = (heading: string) => within(within(key()!).getByRole("list", { name: heading })).getAllByRole("listitem").map((item) => item.textContent);

test("the key lists the kinds loaded and the links drawn under the design's headings, edges as the detail names them (S8)", async () => {
  answers.set(1000, {
    nodes: [node(1), node(2), node(3, { kind: "Paper", title: "A paper", status: "complete" })],
    edges: [edge(2, 1), edge(3, 2, "produced_by"), edge(3, 1, "favors", { valence: -0.5 })],
  });
  keepKey();
  const { user } = await shown("3 nodes, 3 edges");
  expect(within(key()!).getAllByRole("heading").map((heading) => heading.textContent)).toEqual([
    "beliefs",
    "nodes, full color when zoomed in",
    "between clusters",
    "links",
  ]);
  expect(keyItems("nodes, full color when zoomed in")).toEqual(["Issues2", "Papers1"]);
  expect(keyItems("links")).toEqual(["Narrows1", "Favors1", "Produced by1", "argues against"]);
  await user.click(within(key()!).getByRole("button", { name: /^Issues/ }));
  // A kind hidden stays listed, without a count, to show it again; links are only those drawn.
  expect(keyItems("nodes, full color when zoomed in")).toEqual(["Issues2", "Papers"]);
  expect(keyItems("links")).toEqual(["Narrows1", "argues against"]);
});

/** The key's button for the kind named `plural`, by selector: a role query here costs a test some 5 ms a call. */
const keyKind = (plural: string) =>
  [...document.querySelectorAll<HTMLButtonElement>(".graph-key button")].find((button) => button.textContent?.startsWith(plural))!;
const chips = () => [...document.querySelectorAll(".fchip")].map((chip) => chip.textContent);

test("a kind clicked in the key shows only that kind, a hidden one clicked adds it, and the only one shown clicked shows every kind", async () => {
  answers.set(1000, {
    nodes: [node(1), node(2), node(3, { kind: "Paper", title: "A paper" }), node(4, { kind: "Belief", title: "A belief" })],
    edges: [],
  });
  keepKey();
  const { user } = await shown("4 nodes, 0 edges");
  const pressed = () => ["Issues", "Papers", "Beliefs"].filter((plural) => keyKind(plural).getAttribute("aria-pressed") === "true");
  expect(pressed()).toEqual(["Issues", "Papers", "Beliefs"]);
  expect(keyKind("Papers").title).toBe("Show only Papers");
  await user.click(keyKind("Papers"));
  expect(renderer.shown()).toEqual(["A paper"]);
  expect(pressed()).toEqual(["Papers"]);
  // The chip names what shows when that is the shorter list.
  expect(chips()).toEqual(["Kind is Papers"]);
  expect(keyKind("Beliefs").title).toBe("Show Beliefs too");
  expect(keyKind("Papers").title).toBe("Show every kind");
  await user.click(keyKind("Beliefs"));
  expect(renderer.shown()).toEqual(["A paper", "A belief"]);
  expect(chips()).toEqual(["Kind is not Issues"]);
  expect(keyKind("Beliefs").title).toBe("Show only Beliefs");
  await user.click(keyKind("Beliefs"));
  expect(renderer.shown()).toEqual(["A belief"]);
  expect(chips()).toEqual(["Kind is Beliefs"]);
  await user.click(keyKind("Beliefs"));
  expect(renderer.shown()).toEqual(["Issue 1", "Issue 2", "A paper", "A belief"]);
  expect(pressed()).toEqual(["Issues", "Papers", "Beliefs"]);
  expect(document.querySelector(".fchips")).toBeNull();
});

test("a kind shown only by the key stays so as the graph changes: a kind a live update brings is hidden, and listed to add", async () => {
  keepKey();
  const { user } = await shown();
  await user.click(keyKind("Papers"));
  expect(renderer.shown()).toEqual(["A paper"]);
  const belief = node(4, { kind: "Belief", title: "A belief" });
  act(() => client.setQueryData(graphQuery(1000).queryKey, { ...GRAPH, nodes: [...GRAPH.nodes, belief] }));
  await screen.findByText("4 nodes, 2 edges");
  expect(renderer.shown()).toEqual(["A paper"]);
  expect(keyItems("nodes, full color when zoomed in")).toEqual(["Issues", "Papers1", "Beliefs"]);
  // With no Paper left, nothing is drawn, and the key stays to show the kinds loaded again.
  act(() => client.setQueryData(graphQuery(1000).queryKey, { nodes: [node(1), belief], edges: [] }));
  await screen.findByText("2 nodes, 0 edges");
  expect(keyItems("nodes, full color when zoomed in")).toEqual(["Issues", "Beliefs"]);
});

test("a kind Filter hid first leaves the key's first click as it is: it shows only the kind clicked", async () => {
  sessionStorage.setItem(
    "trackinizer.v2.graph",
    JSON.stringify({ limit: 1000, hiddenKinds: ["Belief"], hiddenStatuses: [], skipEdges: [], only: false, key: true, roots: true }),
  );
  const { user } = await shown();
  await user.click(keyKind("Papers"));
  expect(renderer.shown()).toEqual(["A paper"]);
  expect(chips()).toEqual(["Kind is Papers"]);
  // The key lists the kinds loaded: none is a Belief.
  expect(keyItems("nodes, full color when zoomed in")).toEqual(["Issues", "Papers1"]);
});

test("the legend pill hides and shows the key, pressed while it shows; which is kept for the tab", async () => {
  keepKey();
  const { user } = await shown();
  const pill = () => screen.getByRole("button", { name: /^legend/ });
  expect(pill().getAttribute("aria-pressed")).toBe("true");
  expect(pill().getAttribute("aria-controls")).toBe(key()!.id);
  expect(pill().textContent).toBe("legend ▾");
  expect(within(key()!).queryByRole("button", { name: /key/i })).toBeNull();
  await user.click(pill());
  expect(key()).toBeNull();
  expect(pill().getAttribute("aria-pressed")).toBe("false");
  expect(pill().getAttribute("aria-controls")).toBeNull();
  expect(pill().textContent).toBe("legend ▴");
  cleanup();
  renderer = new FakeRenderer();
  highlights = new HighlightStore();
  const again = await shown();
  expect(key()).toBeNull();
  expect(pill().getAttribute("aria-pressed")).toBe("false");
  await again.user.click(pill());
  expect(key()).not.toBeNull();
});

test("what the tab kept hidden shows as chips; a chip's remove or Clear shows those nodes again", async () => {
  answers.set(1000, { ...GRAPH, nodes: [...GRAPH.nodes, node(4, { status: "invalid", title: "Gone wrong" })] });
  sessionStorage.setItem(
    "trackinizer.v2.graph",
    JSON.stringify({ limit: 1000, hiddenKinds: ["Paper"], hiddenStatuses: ["invalid"], skipEdges: [], only: false, key: true, roots: true }),
  );
  const { user } = await shown("4 nodes, 2 edges");
  expect(renderer.shown()).toEqual(["Issue 1", "Issue 2"]);
  expect([...document.querySelectorAll(".fchip")].map((chip) => chip.textContent)).toEqual(["Kind is not Papers", "Status is not Invalid"]);
  await user.click(screen.getByRole("button", { name: "Remove the Kind filter" }));
  expect(renderer.shown()).toEqual(["Issue 1", "Issue 2", "A paper"]);
  await user.click(screen.getByRole("button", { name: "Clear" }));
  expect(renderer.shown()).toEqual(["Issue 1", "Issue 2", "A paper", "Gone wrong"]);
  expect(document.querySelector(".fchips")).toBeNull();
});

test("the node limit offers 1k, 2.5k and 5k, 1k by default; a pick reads that many, frames them, and is kept for the tab", async () => {
  answers.set(2500, { nodes: [node(1)], edges: [] });
  const { user } = await shown();
  expect(await openMenu(user, "Nodes: 1k")).toEqual(["1k nodes", "2.5k nodes", "5k nodes"]);
  expect(screen.getAllByRole("option").map((option) => option.getAttribute("aria-selected"))).toEqual(["true", "false", "false"]);
  await pick(user, "2.5k nodes");
  expect(await screen.findByText("1 node, 0 edges")).toBeTruthy();
  expect(limits()).toEqual(["1000", "2500"]);
  expect(renderer.shown()).toEqual(["Issue 1"]);
  expect(renderer.sets).toEqual([true, true]);
  cleanup();
  show();
  expect(await screen.findByRole("button", { name: "Nodes: 2.5k" })).toBeTruthy();
});

test("any count can be typed as the node limit; text that is no count offers nothing", async () => {
  answers.set(250, { nodes: [node(1)], edges: [] });
  const { user } = await shown();
  await openMenu(user, "Nodes: 1k");
  await user.keyboard("a few");
  expect(screen.queryAllByRole("option")).toEqual([]);
  await user.clear(screen.getByRole("combobox", { name: "Nodes to show…" }));
  await user.keyboard("250");
  expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual(["250 nodes"]);
  await user.keyboard("{Enter}");
  expect(await screen.findByText("1 node, 0 edges")).toBeTruthy();
  expect(limits()).toEqual(["1000", "250"]);
  // The typed count is offered again beside the presets, ticked.
  expect(await openMenu(user, "Nodes: 250")).toEqual(["1k nodes", "2.5k nodes", "5k nodes", "250 nodes"]);
  expect(screen.getAllByRole("option").at(-1)!.getAttribute("aria-selected")).toBe("true");
});

/** A graph of `size` Issues and no edges, cheap to make. */
function many(size: number): Graph {
  const first = node(1);
  return { nodes: Array.from({ length: size }, (_, n) => ({ ...first, id: uuid(n + 1), seq: n + 1, title: `Issue ${n + 1}` })), edges: [] };
}

/** Keep a tab state that reads every inquiry, as a pick of All would. */
const keepAll = () =>
  sessionStorage.setItem(
    "trackinizer.v2.graph",
    JSON.stringify({ limit: ALL_NODES, hiddenKinds: [], hiddenStatuses: [], skipEdges: [], only: false, key: true, roots: true }),
  );

/** Show every node of a 5,001-node graph; returns the question it asks first, while it shows. */
async function overFiveThousand() {
  keepAll();
  answers.set(ALL_NODES, many(5_001));
  const user = userEvent.setup({ delay: null });
  show();
  // By text, as openMenu finds its button: each query by role here costs as much as a step.
  const asks = () => screen.queryByText("Draw 5,001 nodes?");
  await waitFor(() => expect(asks()).not.toBeNull());
  return { user, asks };
}

test("over 5,000 nodes the view asks before it draws, and Draw them draws", async () => {
  const { user, asks } = await overFiveThousand();
  expect(renderer.counts).toEqual([]);
  await user.click(screen.getByText("Draw them", { selector: "button" }));
  expect(renderer.counts).toEqual([5_001]);
  expect(asks()).toBeNull();
});

test("a focus opened over 5,000 nodes is framed once Draw them draws it, not every node", async () => {
  keepAll();
  answers.set(ALL_NODES, { ...many(5_001), edges: [edge(2, 1)] });
  history.replaceState(null, "", "#/graph?focus=Issue/1&hops=1");
  const user = userEvent.setup({ delay: null });
  show();
  await user.click(await screen.findByRole("button", { name: "Draw them" }));
  expect(renderer.settleFrames.map((titles) => titles.slice(0, 3))).toEqual([["Issue 1", "Issue 2"]]);
});

test("Replay waits for Draw them too: play pressed under the question draws nothing", async () => {
  keepAll();
  answers.set(ALL_NODES, many(5_001));
  show();
  await screen.findByRole("heading", { name: "Draw 5,001 nodes?" });
  fireEvent.click(screen.getByRole("button", { name: "Replay the graph's growth" }));
  expect(renderer.counts).toEqual([]);
  expect(screen.getByRole("heading", { name: "Draw 5,001 nodes?" })).toBeTruthy();
});

test("Show 5k instead, in place of a graph over 5,000 nodes, reads the newest 5,000", async () => {
  keepAll();
  answers.set(ALL_NODES, many(5_001));
  answers.set(5000, many(5_000));
  const user = userEvent.setup({ delay: null });
  show();
  await user.click(await screen.findByRole("button", { name: "Show 5k instead" }));
  expect(await screen.findByText("5,000 nodes, 0 edges")).toBeTruthy();
  expect(renderer.counts).toEqual([5_000]);
});

test("a slow answer for an older limit never replaces the newer one's (FR-04)", async () => {
  let answerSlow!: (response: Response) => void;
  answers.set(5000, new Promise((resolve) => (answerSlow = resolve)));
  answers.set(100, { nodes: [node(7)], edges: [] });
  const { user } = await shown();
  await openMenu(user, "Nodes: 1k");
  await pick(user, "5k nodes");
  await openMenu(user, "Nodes: 5k");
  await user.clear(screen.getByRole("combobox", { name: "Nodes to show…" }));
  await user.keyboard("100");
  await user.keyboard("{Enter}");
  await screen.findByText("1 node, 0 edges");
  await act(async () => answerSlow(Response.json(GRAPH)));
  expect(screen.getByText("1 node, 0 edges")).toBeTruthy();
  expect(renderer.shown()).toEqual(["Issue 7"]);
});

test("every fit leaves out the strip on the canvas's right that Peek or the key covers, whichever reaches furthest in, and what the briefing covers at its left; showing or hiding the key frames again", async () => {
  // jsdom lays nothing out: an 800 by 600 canvas, the key 180 px in from its
  // right, the briefing 260 px wide at its left, and Peek 360 px wide.
  const boxes: [string, { left: number; top: number; right: number; bottom: number }][] = [
    [".graph-canvas", { left: 0, top: 0, right: 800, bottom: 600 }],
    [".graph-key", { left: 620, top: 12, right: 788, bottom: 400 }],
    [".proto-briefing", { left: 0, top: 0, right: 260, bottom: 600 }],
    [".graph-peek > *", { left: 440, top: 0, right: 800, bottom: 600 }],
  ];
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
    const box = boxes.find(([selector]) => this.matches(selector))?.[1] ?? { left: 0, top: 0, right: 0, bottom: 0 };
    return { ...box, x: box.left, y: box.top, width: box.right - box.left, height: box.bottom - box.top } as DOMRect;
  });
  keepKey();
  const { user } = await shown();
  expect(renderer.covered!()).toEqual({ right: 180, left: 260 });
  const pill = () => screen.getByRole("button", { name: /^legend/ });
  await user.click(pill());
  await user.click(pill());
  expect(renderer.refits).toEqual([
    { right: 0, left: 260 },
    { right: 180, left: 260 },
  ]);
  act(() => renderer.events!.click(renderer.node("Issue 1")));
  // One panel at a time: the record takes the corner the briefing held, so only the
  // panel itself is left out of a fit.
  expect(renderer.covered!()).toEqual({ right: 360, left: 0 });
});

test("hiding the briefing hands its column back to the canvas, and its pill in the corner brings it back", async () => {
  // The same lay-out as the test above: the key 180 px in from the right of an
  // 800 px canvas, the briefing 260 px wide at its left.
  const boxes: [string, { left: number; top: number; right: number; bottom: number }][] = [
    [".graph-canvas", { left: 0, top: 0, right: 800, bottom: 600 }],
    [".graph-key", { left: 620, top: 12, right: 788, bottom: 400 }],
    [".proto-briefing", { left: 0, top: 0, right: 260, bottom: 600 }],
  ];
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
    const box = boxes.find(([selector]) => this.matches(selector))?.[1] ?? { left: 0, top: 0, right: 0, bottom: 0 };
    return { ...box, x: box.left, y: box.top, width: box.right - box.left, height: box.bottom - box.top } as DOMRect;
  });
  keepKey();
  const { user } = await shown();
  await user.click(screen.getByRole("button", { name: "Hide panel" }));
  expect(document.querySelector(".proto-briefing.is-hidden")).not.toBeNull();
  // Slid out, it covers nothing, whatever the transition is doing: the frame keeps
  // its left edge, and frames again as the panel goes.
  expect(renderer.covered!()).toEqual({ right: 180, left: 0 });
  expect(renderer.refits.at(-1)).toEqual({ right: 180, left: 0 });
  // The pill takes the corner it left; picked, the column covers it again.
  await user.click(screen.getByRole("button", { name: "Show the briefing" }));
  expect(document.querySelector(".proto-briefing.is-hidden")).toBeNull();
  expect(renderer.covered!()).toEqual({ right: 180, left: 260 });
});

test("an answer that changes only fields repaints; one that changes which inquiries show draws them anew", async () => {
  await shown();
  const before = renderer.repaints;
  act(() => client.setQueryData(graphQuery(1000).queryKey, { ...GRAPH, nodes: [node(1, { title: "Renamed" }), ...GRAPH.nodes.slice(1)] }));
  await waitFor(() => expect(renderer.shown()).toEqual(["Renamed", "Issue 2", "A paper"]));
  expect(renderer.sets).toEqual([true]);
  expect(renderer.repaints).toBeGreaterThan(before);
  act(() => client.setQueryData(graphQuery(1000).queryKey, { nodes: [node(1), node(2)], edges: [edge(2, 1)] }));
  expect(await screen.findByText("2 nodes, 1 edge")).toBeTruthy();
  expect(renderer.sets).toEqual([true, false]);
  expect(renderer.shown()).toEqual(["Issue 1", "Issue 2"]);
});

test("an empty graph says so rather than draw a blank canvas (S14)", async () => {
  answers.set(1000, { nodes: [], edges: [] });
  show();
  expect(await screen.findByRole("heading", { name: "No inquiries yet" })).toBeTruthy();
});

test("the colours follow the theme: switching it reads the tokens again and repaints", async () => {
  const root = document.documentElement;
  root.style.setProperty("--g-kind-Issue", "#101010");
  await shown();
  expect(renderer.nodes[0]!.look.fill).toBe("#101010");
  root.style.setProperty("--g-kind-Issue", "#202020");
  const before = renderer.repaints;
  await act(async () => root.setAttribute("data-theme", "light"));
  expect(renderer.nodes[0]!.look.fill).toBe("#202020");
  expect(renderer.repaints).toBe(before + 1);
});

test("a graph that cannot be read says why, with Retry", async () => {
  const user = userEvent.setup({ delay: null });
  answers.set(1000, Promise.resolve(Response.json({ detail: "The database is down." }, { status: 503 })));
  show();
  expect((await screen.findByRole("alert")).textContent).toContain("The database is down.");
  answers.set(1000, GRAPH);
  await user.click(screen.getByRole("button", { name: "Retry" }));
  expect(await screen.findByText("3 nodes, 2 edges")).toBeTruthy();
});

test("the stream keeps it current: its first open reads the graph again", async () => {
  const hub = new LiveHub(client);
  show({ hub });
  await screen.findByText("3 nodes, 2 edges");
  answers.set(1000, { nodes: [node(1)], edges: [] });
  act(() => hub.open());
  expect(await screen.findByText("1 node, 0 edges")).toBeTruthy();
  expect(limits()).toEqual(["1000", "1000"]);
});

test("the console's trackinizer.graph() reads what is drawn, the selection and the focus while the view is open", async () => {
  const debug: { graph?: () => unknown } = {};
  Object.assign(window, { trackinizer: debug });
  history.replaceState(null, "", "#/graph?focus=Issue/2&hops=1");
  await shown();
  expect(debug.graph?.()).toMatchObject({
    nodes: [{ title: "Issue 1", kind: "Issue", hidden: false }, { title: "Issue 2" }, { title: "A paper", kind: "Paper" }],
    links: [{ kind: "narrows" }, { kind: "favors", valence: 0.5 }],
    // Opened on a focus, the view selects it.
    selected: uuid(2),
    focus: uuid(2),
  });
  cleanup();
  expect(debug.graph).toBeUndefined();
  expect(renderer.disposed).toBe(true);
});

const peek = () => screen.queryByRole("complementary", { name: "Peek" });
// Deselection animates Peek out, so it leaves the DOM a few frames later.
const peekGone = () => waitFor(() => expect(peek()).toBeNull());
/** The nodes drawn dim, outside a light. */
const dimmed = () => renderer.nodes.filter((drawn) => drawn.look.ringAlpha < 0.25).map((drawn) => drawn.title);
const halos = () => renderer.nodes.filter((drawn) => drawn.look.halo !== null).map((drawn) => `${drawn.title}: ${drawn.look.halo}`);

test("hovering a node shows its kind, status, ref and title by it, as text (S9), and lights it and its neighbours", async () => {
  answers.set(1000, { ...GRAPH, nodes: [node(1, { title: "<b>Not bold</b>" }), ...GRAPH.nodes.slice(1)] });
  await shown();
  act(() => renderer.events!.hover(renderer.node("<b>Not bold</b>")));
  const tip = screen.getByRole("tooltip");
  expect([...tip.children].map((part) => part.textContent)).toEqual(["Issue#1", "<b>Not bold</b>", "Click opens Peek · double-click focuses here"]);
  expect(within(tip).getByRole("img", { name: "Active" })).toBeTruthy();
  expect(tip.style.left).toBe(`${10 + 12}px`);
  expect(dimmed()).toEqual(["A paper"]);
  act(() => renderer.events!.hover(null));
  expect(screen.queryByRole("tooltip")).toBeNull();
  expect(dimmed()).toEqual([]);
});

test("a click selects a node: Peek opens on it, it takes the strong halo, and its light holds while the pointer roams; Close clears it", async () => {
  const { user } = await shown();
  act(() => renderer.events!.click(renderer.node("Issue 1")));
  expect(within(peek()!).getAllByRole("heading").at(-1)!.textContent).toBe("Issue#1");
  expect(halos()).toEqual(["Issue 1: halo"]);
  expect(dimmed()).toEqual(["A paper"]);
  act(() => renderer.events!.hover(renderer.node("A paper")));
  expect(dimmed()).toEqual(["A paper"]);
  await user.click(screen.getByRole("button", { name: "Close peek" }));
  await peekGone();
  expect(dimmed()).toEqual(["Issue 1"]);
});

test("a selection made without the pointer holds the canvas's light too, and clearing it hands the field back", async () => {
  const { user } = await shown();
  act(() => renderer.events!.click(renderer.node("Issue 1")));
  // The view hands the field back on its way in, then asks for the node the click
  // picked; a later clear hands it back again.
  expect(renderer.selects).toEqual([null, "Issue 1"]);
  act(() => renderer.events!.click(null));
  await peekGone();
  expect(renderer.selects.at(-1)).toBe(null);
  // A search pick, which the pointer never touched, lights it the same way.
  await user.click(search());
  fireEvent.change(search(), { target: { value: "paper" } });
  await user.keyboard("{Enter}");
  expect(renderer.selects.at(-1)).toBe("A paper");
  expect(within(peek()!).getAllByRole("heading").at(-1)!.textContent).toBe("Paper#3");
});

test("a click on the background, or Esc, clears the selection", async () => {
  const { user } = await shown();
  act(() => renderer.events!.click(renderer.node("Issue 2")));
  act(() => renderer.events!.click(null));
  await peekGone();
  act(() => renderer.events!.click(renderer.node("Issue 2")));
  await user.keyboard("{Escape}");
  await peekGone();
});

test("a pick on the graph goes out to the canvas as the show an agent would write", async () => {
  const canvas = canvasStub();
  show({ canvas: canvas.actions });
  await screen.findByText("3 nodes, 2 edges");
  // Nothing picked yet, so the graph has made no claim on the canvas.
  expect(canvas.asked).toEqual([]);
  act(() => renderer.events!.click(renderer.node("Issue 1")));
  expect(canvas.asked).toEqual([uuid(1)]);
  expect(within(peek()!).getAllByRole("heading").at(-1)!.textContent).toBe("Issue#1");
  // The same record picked again is not written again.
  act(() => renderer.events!.click(renderer.node("Issue 1")));
  expect(canvas.asked).toEqual([uuid(1)]);
  act(() => renderer.events!.click(renderer.node("Issue 2")));
  expect(canvas.asked).toEqual([uuid(1), uuid(2)]);
  // Clearing the selection leaves the pane on the last record: taking a pane down
  // wants its instance id, which the canvas hands to its visuals, not to a route.
  act(() => renderer.events!.click(null));
  await peekGone();
  expect(canvas.asked).toEqual([uuid(1), uuid(2)]);
});

test("a link in Peek to a node the graph draws selects it in place and centres on it; Open and others navigate", async () => {
  await shown();
  act(() => renderer.events!.click(renderer.node("Issue 2")));
  // fireEvent answers false when the click's default, following the link, was prevented.
  expect(fireEvent.click(screen.getByRole("link", { name: "Parent by ref" }))).toBe(false);
  expect(within(peek()!).getAllByRole("heading").at(-1)!.textContent).toBe("Issue#1");
  expect(fireEvent.click(screen.getByRole("link", { name: "Paper by id" }))).toBe(false);
  expect(within(peek()!).getAllByRole("heading").at(-1)!.textContent).toBe("Paper#3");
  expect(renderer.centred).toEqual(["Issue 1", "A paper"]);
  expect(fireEvent.click(screen.getByRole("link", { name: "Open" }))).toBe(true);
  expect(fireEvent.click(screen.getByRole("link", { name: "Not drawn" }))).toBe(true);
  expect(fireEvent.click(screen.getByRole("link", { name: "Parent by ref" }), { metaKey: true })).toBe(true);
});

test("a Peek link to a node the graph loaded but does not draw navigates: one a filter hides, or Only these leaves out", async () => {
  const kept = { limit: 1000, hiddenKinds: ["Paper"], hiddenStatuses: [], skipEdges: [], only: false, key: true, roots: true };
  sessionStorage.setItem("trackinizer.v2.graph", JSON.stringify(kept));
  await shown("3 nodes, 2 edges");
  act(() => renderer.events!.click(renderer.node("Issue 2")));
  expect(fireEvent.click(screen.getByRole("link", { name: "Paper by id" }))).toBe(true);
  cleanup();
  // Within 1 hop of Issue 1 lie Issues 1 and 2; Only these hides the paper.
  sessionStorage.setItem("trackinizer.v2.graph", JSON.stringify({ ...kept, hiddenKinds: [], only: true }));
  history.replaceState(null, "", "#/graph?focus=Issue/1&hops=1");
  await shown("3 nodes, 2 edges");
  expect(within(peek()!).getAllByRole("heading").at(-1)!.textContent).toBe("Issue#1");
  expect(fireEvent.click(screen.getByRole("link", { name: "Paper by id" }))).toBe(true);
});

test("a selected node the graph no longer has is no longer selected (R3-03)", async () => {
  await shown();
  act(() => renderer.events!.click(renderer.node("A paper")));
  act(() => client.setQueryData(graphQuery(1000).queryKey, { nodes: [node(1), node(2)], edges: [edge(2, 1)] }));
  await waitFor(() => expect(peek()).toBeNull());
  act(() => client.setQueryData(graphQuery(1000).queryKey, GRAPH));
  await screen.findByText("3 nodes, 2 edges");
  expect(peek()).toBeNull();
});

const search = () => screen.getByRole("combobox", { name: "Search the graph" });
const titles = (options: readonly Element[]) => options.map((option) => option.querySelector(".row-title")?.textContent);

test("search lights its matches on the canvas and lists them newest first; Enter centres on one, selects it, and keeps the query (G1-G3)", async () => {
  const { user } = await shown();
  await user.click(search());
  fireEvent.change(search(), { target: { value: "issue" } });
  expect(count()).toBe("3 nodes, 2 edges");
  expect(screen.getByText("2 matches")).toBeTruthy();
  expect(titles(screen.getAllByRole("option"))).toEqual(["Issue 2", "Issue 1"]);
  expect(halos()).toEqual(["Issue 1: match", "Issue 2: match"]);
  await user.keyboard("{ArrowDown}{ArrowDown}");
  expect(titles([screen.getByRole("option", { selected: true })])).toEqual(["Issue 1"]);
  await user.keyboard("{ArrowUp}{Enter}");
  expect(within(peek()!).getAllByRole("heading").at(-1)!.textContent).toBe("Issue#2");
  expect(renderer.centred).toEqual(["Issue 2"]);
  expect((search() as HTMLInputElement).value).toBe("issue");
  expect(titles(screen.getAllByRole("option"))).toEqual(["Issue 2", "Issue 1"]);
  // A click picks too.
  await user.click(screen.getAllByRole("option")[1]!);
  expect(within(peek()!).getAllByRole("heading").at(-1)!.textContent).toBe("Issue#1");
  // ...and marks the row it picked, so the list names what stands open beside it,
  // and the keys step on from there rather than from where they left off.
  expect(titles(screen.getAllByRole("option").filter((row) => row.classList.contains("is-focused")))).toEqual(["Issue 1"]);
  await user.keyboard("{ArrowUp}{Enter}");
  expect(within(peek()!).getAllByRole("heading").at(-1)!.textContent).toBe("Issue#2");
});

test("search finds no node a filter hides, so a pick never centres on empty space (G4)", async () => {
  const kept = { limit: 1000, hiddenKinds: ["Paper"], hiddenStatuses: [], skipEdges: [], only: false, key: true, roots: true };
  sessionStorage.setItem("trackinizer.v2.graph", JSON.stringify(kept));
  await shown("3 nodes, 2 edges");
  for (const query of ["paper", "#3"]) {
    fireEvent.change(search(), { target: { value: query } });
    expect(screen.getByRole("listbox", { name: "Matches" }).textContent, query).toContain("No matches");
  }
});

test("what the assistant points at takes the highlight halo, under the selection's and over a match's; an empty list clears it", async () => {
  const { user } = await shown();
  expect(halos()).toEqual([]);
  act(() => highlights.set([uuid(1), uuid(3)]));
  expect(halos()).toEqual(["Issue 1: highlight", "A paper: highlight"]);
  expect(dimmed()).toEqual([]);
  await user.click(search());
  fireEvent.change(search(), { target: { value: "issue" } });
  expect(halos()).toEqual(["Issue 1: highlight", "Issue 2: match", "A paper: highlight"]);
  act(() => highlights.set([]));
  expect(halos()).toEqual(["Issue 1: match", "Issue 2: match"]);
});

test("with a focus, search groups its matches within the focus's hops and elsewhere", async () => {
  history.replaceState(null, "", "#/graph?focus=Issue/1&hops=1");
  answers.set(1000, { ...GRAPH, nodes: [...GRAPH.nodes, node(4)] });
  await shown("4 nodes, 2 edges");
  fireEvent.change(search(), { target: { value: "issue" } });
  const group = (name: string) => within(screen.getByRole("group", { name })).getAllByRole("option");
  expect(titles(group("Within 1 hop of Issue#1"))).toEqual(["Issue 2", "Issue 1"]);
  expect(group("Within 1 hop of Issue#1").map((option) => option.querySelector(".row-meta")?.textContent)).toEqual(["1 hop", "focus"]);
  expect(titles(group("Elsewhere in the graph"))).toEqual(["Issue 4"]);
});

test("Shift+Enter, or the panel's foot, searches every inquiry on the app's search page (G5)", async () => {
  const { user } = await shown();
  await user.click(search());
  fireEvent.change(search(), { target: { value: "what?" } });
  expect(screen.getByRole("link", { name: /Search every inquiry for “what\?”/ }).getAttribute("href")).toBe("#/search/what%3F");
  await user.keyboard("{Shift>}{Enter}{/Shift}");
  await waitFor(() => expect(location.hash).toBe("#/search/what%3F"));
});

test("search by #seq picks the first match on Enter; no match says so; Escape empties it, leaves it, and clears the selection", async () => {
  const { user } = await shown();
  await user.click(search());
  fireEvent.change(search(), { target: { value: "#3" } });
  await user.keyboard("{Enter}");
  expect(within(peek()!).getAllByRole("heading").at(-1)!.textContent).toBe("Paper#3");
  fireEvent.change(search(), { target: { value: "nothing like it" } });
  expect(screen.getByRole("listbox", { name: "Matches" }).textContent).toContain("No matches");
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("listbox", { name: "Matches" })).toBeNull();
  expect((search() as HTMLInputElement).value).toBe("");
  await peekGone();
  expect(document.activeElement).not.toBe(search());
});

test("Enter after a re-read leaves fewer matches than the marked one picks a match that is there (GR-01)", async () => {
  answers.set(1000, { nodes: [node(1), node(2), node(4)], edges: [] });
  const { user } = await shown("3 nodes, 0 edges");
  await user.click(search());
  fireEvent.change(search(), { target: { value: "issue" } });
  await user.keyboard("{ArrowDown}{ArrowDown}{ArrowDown}");
  act(() => client.setQueryData(graphQuery(1000).queryKey, { nodes: [node(1)], edges: [] }));
  await screen.findByText("1 match");
  await user.keyboard("{Enter}");
  expect(within(peek()!).getAllByRole("heading").at(-1)!.textContent).toBe("Issue#1");
});

test("the keys: f fits, / goes to search", async () => {
  const { user } = await shown();
  await user.keyboard("f");
  expect(renderer.fits).toBe(1);
  await user.keyboard("/");
  expect(document.activeElement).toBe(search());
  expect((document.activeElement as HTMLInputElement).value).toBe("");
});

test("the timeline's play sweeps the field as it was made, one record at a time, and an answer that waits for it draws after (F10)", async () => {
  await shown();
  vi.useFakeTimers();
  const drawn = renderer.counts.length;
  fireEvent.click(screen.getByRole("button", { name: "Replay the graph's growth" }));
  // The graph is sent once, when it arrives; playing only shows more of it.
  expect(renderer.counts).toHaveLength(drawn);
  // The oldest record is up first, coming up: a twenty-fourth of its own ink,
  // since the newest 24 of a sweep are what the ramp raises.
  expect(renderer.shown()).toEqual(["Issue 1"]);
  expect(renderer.node("Issue 1").look.fillAlpha).toBeCloseTo(1 / 24, 5);
  // The count in the tools is the cut's own, as the design's `setCut` writes it.
  expect(count()).toBe("1 node, 0 edges");
  // Three records cross in the design's 14 s x 3/2500 + 4 s, so 4.02 s: three
  // quarters through, the second record is up and the paper is not yet.
  act(() => vi.advanceTimersByTime(3000));
  expect(renderer.shown()).toEqual(["Issue 1", "Issue 2"]);
  // A wire draws only with both its ends: Issue 2's to Issue 1 is up, the
  // paper's waits for the record the place has not reached.
  expect(renderer.links.map((link) => link.hidden)).toEqual([false, true]);
  // and the count says so: two records, the one wire between them.
  expect(count()).toBe("2 nodes, 1 edge");
  // While it runs, an answer waits for it.
  act(() => client.setQueryData(graphQuery(1000).queryKey, { nodes: [node(1), node(2)], edges: [edge(2, 1)] }));
  expect(renderer.counts).toHaveLength(drawn);
  // The sweep's end: the whole is shown, and the waiting answer draws.
  act(() => vi.advanceTimersByTime(1200));
  expect(renderer.counts).toHaveLength(drawn + 1);
  expect(renderer.shown()).toEqual(["Issue 1", "Issue 2"]);
  expect(screen.getByRole("button", { name: "Replay the graph's growth" })).toBeTruthy();
  vi.useRealTimers();
});

test("the timeline's scrub draws the graph as it stood at that date, and never re-sends it (F10)", async () => {
  // Three records over a fortnight, so a pointer a third along the track is
  // plainly before the second's date and plainly after the first's.
  answers.set(1000, {
    nodes: [
      node(1, { created: "2026-09-01T00:00:00+00:00" }),
      node(2, { created: "2026-09-08T00:00:00+00:00" }),
      node(3, { kind: "Paper", title: "A paper", created: "2026-09-15T00:00:00+00:00" }),
    ],
    edges: GRAPH.edges,
  });
  await shown();
  const drawn = renderer.counts.length;
  const track = screen.getByRole("slider", { name: "Replay position" });
  // jsdom gives every box a rect of zeros; the track reads the fraction off its own width.
  track.getBoundingClientRect = () =>
    ({ left: 0, right: 200, top: 0, bottom: 24, width: 200, height: 24, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect;
  fireEvent.pointerDown(track, { clientX: 70, pointerId: 1 });
  expect(renderer.shown()).toEqual(["Issue 1"]);
  // A record long made draws outright: the ramp is for arrivals under a moving
  // place, not a wash over the last of a field whose records are all behind it.
  expect(renderer.node("Issue 1").look.fillAlpha).toBe(1);
  fireEvent.pointerMove(track, { clientX: 130, pointerId: 1, buttons: 1 });
  expect(renderer.shown()).toEqual(["Issue 1", "Issue 2"]);
  expect(renderer.links.map((link) => link.hidden)).toEqual([false, true]);
  expect(count()).toBe("2 nodes, 1 edge");
  expect(screen.getByText(/^Sep/).textContent).toBe("Sep 8, 2 nodes");
  expect(renderer.counts).toHaveLength(drawn);
  // The track's far end: every record is behind the stand, so the count is the
  // whole field again and the date line stops naming a part of it.
  fireEvent.pointerMove(track, { clientX: 200, pointerId: 1, buttons: 1 });
  expect(count()).toBe("3 nodes, 2 edges");
  expect(screen.getByText(/^Sep/).textContent).toBe("Sep 15");
});

test("a limit changed during Replay stops it, and the new limit's answer draws at once (GR-02)", async () => {
  answers.set(2500, { nodes: [node(7)], edges: [] });
  const { user } = await shown();
  await user.click(screen.getByRole("button", { name: "Replay the graph's growth" }));
  await openMenu(user, "Nodes: 1k");
  await pick(user, "2.5k nodes");
  expect(await screen.findByText("1 node, 0 edges")).toBeTruthy();
  expect(renderer.shown()).toEqual(["Issue 7"]);
  expect(renderer.sets.at(-1)).toBe(true);
  expect(screen.getByRole("button", { name: "Replay the graph's growth" })).toBeTruthy();
});

// 1 <- 2 <- 3 <- 4 by narrows, 5 produced by 3, 6 apart.
const CHAIN: Graph = {
  nodes: [1, 2, 3, 4, 5, 6].map((n) => node(n)),
  edges: [edge(2, 1), edge(3, 2), edge(4, 3), edge(5, 3, "produced_by")],
};
const hopsButton = (name: RegExp) => within(screen.getByRole("group", { name: "Hops" })).getByRole("button", { name });

test("a double-click selects a node and centres on it, without taking the focus", async () => {
  answers.set(1000, CHAIN);
  await shown("6 nodes, 4 edges");
  act(() => renderer.events!.doubleClick(renderer.node("Issue 2")));
  expect(renderer.centred).toEqual(["Issue 2"]);
  expect(within(peek()!).getAllByRole("heading").at(-1)!.textContent).toBe("Issue#2");
  expect(location.hash).toBe("#/graph?group=none");
});

test("a focus in the hash lights what lies within 1 hop, the focus row counting its hops and what is lit", async () => {
  answers.set(1000, CHAIN);
  history.replaceState(null, "", "#/graph?focus=Issue/2&hops=1");
  await shown("6 nodes, 4 edges");
  const row = within(screen.getByRole("group", { name: "Focus" }));
  expect(row.getByText("Issue#2 · Issue 2")).toBeTruthy();
  expect(within(screen.getByRole("group", { name: "Hops" })).getAllByRole("button").map((button) => button.getAttribute("aria-label"))).toEqual([
    "1 hop, 3 nodes",
    "2 hops, 5 nodes",
    "3 hops, 5 nodes",
    "All hops, 5 nodes",
  ]);
  expect(hopsButton(/^1 hop/).getAttribute("aria-pressed")).toBe("true");
  expect(dimmed()).toEqual(["Issue 4", "Issue 5", "Issue 6"]);
  expect(halos()).toEqual(["Issue 2: halo"]);
  expect(row.getByText("3 nodes · 2 edges")).toBeTruthy();
});

test(". focuses the graph on the selected node, at the hops held", async () => {
  answers.set(1000, CHAIN);
  history.replaceState(null, "", "#/graph?focus=Issue/1&hops=2");
  const { user } = await shown("6 nodes, 4 edges");
  act(() => renderer.events!.click(renderer.node("Issue 4")));
  await user.keyboard(".");
  await waitFor(() => expect(location.hash).toBe("#/graph?focus=Issue/4&hops=2"));
});

test("Hops step the focus 1 to 3 and All, each replacing the hash; what lies two or more hops out fades", async () => {
  answers.set(1000, CHAIN);
  history.replaceState(null, "", "#/graph?focus=Issue/1&hops=1");
  const { user } = await shown("6 nodes, 4 edges");
  const before = history.length;
  expect(dimmed()).toEqual(["Issue 3", "Issue 4", "Issue 5", "Issue 6"]);
  await user.click(hopsButton(/^2 hops/));
  expect(location.hash).toBe("#/graph?focus=Issue/1&hops=2");
  expect(dimmed()).toEqual(["Issue 4", "Issue 5", "Issue 6"]);
  expect(renderer.node("Issue 3").look.ringAlpha).toBe(0.7);
  await user.click(hopsButton(/^3 hops/));
  expect(dimmed()).toEqual(["Issue 6"]);
  await user.click(hopsButton(/^All hops/));
  expect(location.hash).toBe("#/graph?focus=Issue/1&hops=all");
  expect(history.length).toBe(before);
});

test("a focus frames what it lights, and frames again for new hops, but not for a re-read of the same graph", async () => {
  answers.set(1000, CHAIN);
  history.replaceState(null, "", "#/graph?focus=Issue/1&hops=1");
  const { user } = await shown("6 nodes, 4 edges");
  expect(renderer.framed.map((frame) => frame.titles)).toEqual([["Issue 1", "Issue 2"]]);
  await user.click(hopsButton(/^2 hops/));
  expect(renderer.framed.map((frame) => frame.titles).at(-1)).toEqual(["Issue 1", "Issue 2", "Issue 3"]);
  act(() => client.setQueryData(graphQuery(1000).queryKey, { ...CHAIN, nodes: CHAIN.nodes.map((row) => ({ ...row, title: `${row.title} renamed` })) }));
  await screen.findByText("Issue#1 · Issue 1 renamed");
  expect(renderer.framed).toHaveLength(2);
});

test("opened on a focus, as Show in graph opens it, the view selects the focus: Peek opens on it, and what it lights is framed beside Peek", async () => {
  // jsdom lays nothing out: an 800 by 600 canvas, the key 180 px in from its
  // right, and Peek 360 px wide.
  const boxes: [string, { left: number; top: number; right: number; bottom: number }][] = [
    [".graph-canvas", { left: 0, top: 0, right: 800, bottom: 600 }],
    [".graph-key", { left: 620, top: 12, right: 788, bottom: 400 }],
    [".graph-peek > *", { left: 440, top: 0, right: 800, bottom: 600 }],
  ];
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
    const box = boxes.find(([selector]) => this.matches(selector))?.[1] ?? { left: 0, top: 0, right: 0, bottom: 0 };
    return { ...box, x: box.left, y: box.top, width: box.right - box.left, height: box.bottom - box.top } as DOMRect;
  });
  answers.set(1000, CHAIN);
  history.replaceState(null, "", "#/graph?focus=Issue/2&hops=1");
  await shown("6 nodes, 4 edges");
  expect(within(peek()!).getAllByRole("heading").at(-1)!.textContent).toBe("Issue#2");
  expect(renderer.framed).toEqual([{ titles: ["Issue 1", "Issue 2", "Issue 3"], covered: { right: 360, left: 0 } }]);
  // Arriving on a focus selects without centring; a double-click then selects and centres anew.
  expect(renderer.centred).toEqual([]);
  act(() => renderer.events!.doubleClick(renderer.node("Issue 4")));
  expect(within(peek()!).getAllByRole("heading").at(-1)!.textContent).toBe("Issue#4");
  expect(renderer.centred).toEqual(["Issue 4"]);
  expect(location.hash).toBe("#/graph?focus=Issue/2&hops=1");
});

test("a focus by an id in capitals finds its node, and the hash spells the id in lower case", async () => {
  const id = "abcdef12-3456-4789-8abc-def012345678";
  answers.set(1000, { nodes: [node(1), node(2, { id })], edges: [] });
  history.replaceState(null, "", `#/graph?focus=${id.toUpperCase()}&hops=1`);
  await shown("2 nodes, 0 edges");
  expect(screen.getByRole("group", { name: "Focus" }).querySelector(".graph-focus-name")?.textContent).toBe(`${id} · Issue 2`);
  expect(location.hash).toBe(`#/graph?focus=${id}&hops=1`);
});

test("Only these hides what the focus leaves out, and Dim the rest shows it again, dim", async () => {
  answers.set(1000, CHAIN);
  history.replaceState(null, "", "#/graph?focus=Issue/1&hops=2");
  const { user } = await shown("6 nodes, 4 edges");
  await user.click(screen.getByRole("button", { name: "Only these" }));
  expect(screen.getByRole("button", { name: "Only these" }).getAttribute("aria-pressed")).toBe("true");
  expect(renderer.shown()).toEqual(["Issue 1", "Issue 2", "Issue 3"]);
  await user.click(screen.getByRole("button", { name: "Dim the rest" }));
  expect(renderer.shown()).toEqual(["Issue 1", "Issue 2", "Issue 3", "Issue 4", "Issue 5", "Issue 6"]);
});

test("Through walks the focus over the edge kinds ticked, and the hop counts follow", async () => {
  answers.set(1000, CHAIN);
  history.replaceState(null, "", "#/graph?focus=Issue/3&hops=1&group=none");
  const { user } = await shown("6 nodes, 4 edges");
  expect(dimmed()).toEqual(["Issue 1", "Issue 6"]);
  expect(await openMenu(user, "Through: all edges")).toEqual(["Narrows", "Proves", "Favors", "Cites", "Produced by"]);
  await pick(user, "Produced by");
  await user.keyboard("{Escape}");
  expect(dimmed()).toEqual(["Issue 1", "Issue 5", "Issue 6"]);
  expect(hopsButton(/^All hops/).getAttribute("aria-label")).toBe("All hops, 4 nodes");
  expect(screen.getByRole("button", { name: "Through: all but Produced by" })).toBeTruthy();
});

test("Esc clears the selection first, then the focus; the focus chip's close clears it too", async () => {
  answers.set(1000, CHAIN);
  history.replaceState(null, "", "#/graph?focus=Issue/1&hops=1");
  const { user } = await shown("6 nodes, 4 edges");
  act(() => renderer.events!.click(renderer.node("Issue 2")));
  await user.keyboard("{Escape}");
  await peekGone();
  expect(location.hash).toBe("#/graph?focus=Issue/1&hops=1");
  await user.keyboard("{Escape}");
  await waitFor(() => expect(location.hash).toBe("#/graph"));
  expect(screen.queryByRole("group", { name: "Focus" })).toBeNull();
  expect(dimmed()).toEqual([]);
  act(() => renderer.events!.doubleClick(renderer.node("Issue 4")));
  await user.keyboard(".");
  await waitFor(() => expect(location.hash).toBe("#/graph?focus=Issue/4&hops=1"));
  await user.click(await screen.findByRole("button", { name: "Clear the focus" }));
  await waitFor(() => expect(location.hash).toBe("#/graph"));
});

test("a focus on an inquiry no size of the graph holds says so and lights nothing", async () => {
  // The view reaches for the next size up before giving this answer, so the larger
  // graph has to be answered too; without the row there either, it settles on saying so.
  answers.set(5000, GRAPH);
  history.replaceState(null, "", "#/graph?focus=Issue/99&hops=2");
  await shown();
  expect(within(await screen.findByRole("group", { name: "Focus" })).getByText("Issue#99 is not among the nodes loaded")).toBeTruthy();
  expect(dimmed()).toEqual([]);
});

test("a focus outside the newest nodes loads the next size up rather than reporting it cannot see it", async () => {
  // The record someone has just read about is usually not among the newest thousand of a
  // store that holds thousands — the rail's Graph opens on what the canvas is about
  // (`graph/topic`), and that landed on "not among the nodes loaded" while the row was
  // one size setting away. One step up, and the focus lights.
  answers.set(5000, { nodes: [...GRAPH.nodes, node(99)], edges: [edge(99, 1)] });
  history.replaceState(null, "", "#/graph?focus=Issue/99&hops=2");
  // Not `shown`: the first answer is stepped over too quickly to wait on its count.
  show();
  // The larger graph arrives and the focus is in it: the chip names the row it found.
  await screen.findByText("4 nodes, 1 edge");
  expect(within(screen.getByRole("group", { name: "Focus" })).getByText(/^Issue#99 · /)).toBeTruthy();
  // One step, and only one: it reads 1k then 5k, never the whole store.
  expect(limits()).toEqual(["1000", "5000"]);
  expect(screen.getByRole("button", { name: "Nodes: 5k" })).toBeTruthy();
});

// Two trees, 1 <- 2 and 3 <- 4, 3 <- 5, and 6 under no root; the newer tree, 3's, lists first.
const TREES: Graph = { nodes: [1, 2, 3, 4, 5, 6].map((n) => node(n)), edges: [edge(2, 1), edge(4, 3), edge(5, 3)] };

test("the graph opens grouped by root: each root's subgraph is an island with its root labelled and named; ?group=none turns it off", async () => {
  answers.set(1000, TREES);
  history.replaceState(null, "", "#/graph");
  await shown("6 nodes, 3 edges");
  // Six records, three small families: the field gathers them into one ball.
  expect(Object.fromEntries(renderer.groups!)).toEqual({
    "Issue 1": uuid(3),
    "Issue 2": uuid(3),
    "Issue 3": uuid(3),
    "Issue 4": uuid(3),
    "Issue 5": uuid(3),
    "Issue 6": uuid(3),
  });
  expect(renderer.nodes.map((drawn) => drawn.look.label?.text ?? null)).toEqual(["Issue 1", null, "Issue 3", null, null, null]);
  expect(Object.fromEntries(renderer.islandNames!)).toEqual({ [uuid(3)]: "Issue 3" });
  cleanup();
  renderer = new FakeRenderer();
  history.replaceState(null, "", "#/graph?group=none");
  await shown("6 nodes, 3 edges");
  // Ungrouped, the islands lose their names and their roots' labels. (The layout
  // still gets island homes: see the note in the report.)
  expect(renderer.islandNames).toBeNull();
  expect(renderer.nodes.map((drawn) => drawn.look.label)).toEqual(Array(6).fill(null));
});

/** A record made `min` minutes into the design's day. */
const madeAt = (min: number) => new Date(Date.UTC(2026, 8, 20, 0, min)).toISOString().replace("Z", "+00:00");

/** An inquiry: `head` and `size` - 1 records that narrow it, all made from `at` on. */
function inquiry(head: number, size: number, at: number): [GraphNode[], GraphEdge[]] {
  const nodes = Array.from({ length: size }, (_, i) => node(head + i, { created: madeAt(at + i) }));
  return [nodes, nodes.slice(1).map((child) => edge(child.seq, head))];
}

// Two inquiries of 30, an hour and ten hours into the day, and 25 records the feed
// left unlinked late after both: no inquiry was alive when they happened, so the
// field has no ball to hand them to and gathers them into one of their own.
const [MORN, MORN_LINKS] = inquiry(1, 30, 60);
const [NOON, NOON_LINKS] = inquiry(40, 30, 600);
const DUST = Array.from({ length: 25 }, (_, i) => node(100 + i, { created: madeAt(2000 + i) }));
const DUSTY: Graph = { nodes: [...MORN, ...NOON, ...DUST], edges: [...MORN_LINKS, ...NOON_LINKS] };

test("a ball gathered from records nobody built on wears no name: it is not a line of inquiry", async () => {
  answers.set(1000, DUSTY);
  history.replaceState(null, "", "#/graph");
  await shown("85 nodes, 58 edges");
  // The unlinked late-comers keep balls of their own instead of a tail on either inquiry.
  const grouped = new Map([...renderer.groups!].flatMap(([title, key]) => (key === undefined ? [] : [[title, key] as const])));
  const dustKeys = Array.from({ length: 25 }, (_, i) => grouped.get(`Issue ${100 + i}`)!);
  expect(dustKeys.filter((key) => key === uuid(1) || key === uuid(40))).toEqual([]);
  const held = new Map<string, number>();
  for (const key of grouped.values()) held.set(key, (held.get(key) ?? 0) + 1);
  // They hold the 25 between them and nothing else: neither inquiry gives a record up.
  const balls = [...new Set(dustKeys)];
  expect(balls.reduce((all, key) => all + held.get(key)!, 0)).toEqual(25);
  // And each is a ball at this window's scale rather than a speck. What is too
  // small to read is a share of the window (islands.ts), so the 25 stand as balls
  // of their own here, where in a five-thousand-record view they would be one.
  for (const key of balls) expect(held.get(key)).toBeGreaterThanOrEqual(7);
  // Each inquiry is named for the record at its head; the ball of dust has no head,
  // so none of their titles may be worn as one.
  expect(Object.fromEntries(renderer.islandNames!)).toEqual({ [uuid(1)]: "Issue 1", [uuid(40)]: "Issue 40" });
});

test("an island clicked on the canvas opens its root's briefing in Peek and frames the island; Unrooted is only framed", async () => {
  answers.set(1000, TREES);
  history.replaceState(null, "", "#/graph");
  await shown("6 nodes, 3 edges");
  act(() => renderer.clickIsland(uuid(1)));
  // The briefing of the cluster the root tops, above the detail, in Peek.
  expect(within(peek()!).getByRole("heading", { name: "Issue 1" })).toBeTruthy();
  expect(within(peek()!).getByRole("heading", { name: "Issue#1" })).toBeTruthy();
  expect(renderer.framed.map((frame) => frame.titles)).toEqual([["Issue 1", "Issue 2"]]);
  expect(renderer.centred).toEqual([]);
  // Unrooted has no thread to brief, so its pick only frames it.
  act(() => renderer.clickIsland("unrooted"));
  await peekGone();
  expect(renderer.framed.at(-1)).toEqual({ titles: ["Issue 6"], covered: { right: 0, left: 0 } });
});

test("a root's frame takes its island, the nodes laid out nearest it, not every node under it", async () => {
  // 5 is under root 1, two hops down through 2, and under root 3, one hop down: its island is 3's.
  answers.set(1000, { nodes: [1, 2, 3, 4, 5].map((n) => node(n)), edges: [edge(2, 1), edge(4, 3), edge(5, 2), edge(5, 3, "produced_by")] });
  history.replaceState(null, "", "#/graph");
  await shown("5 nodes, 4 edges");
  act(() => renderer.clickIsland(uuid(1)));
  expect(renderer.framed.at(-1)!.titles).toEqual(["Issue 1", "Issue 2"]);
});

test("while grouped, a search's matches take the left column, and the briefing comes back once the query is emptied", async () => {
  answers.set(1000, TREES);
  history.replaceState(null, "", "#/graph");
  const { user } = await shown("6 nodes, 3 edges");
  fireEvent.change(search(), { target: { value: "issue 5" } });
  expect(results()).not.toBeNull();
  expect(document.querySelector(".proto-briefing")).toBeNull();
  expect(titles(screen.getAllByRole("option"))).toEqual(["Issue 5"]);
  await user.click(search());
  await user.keyboard("{Escape}");
  expect(results()).toBeNull();
  expect(document.querySelector(".proto-briefing")).not.toBeNull();
});

/** A Belief two Experiments prove against, and its two citers: the briefing's first flag counts it. */
const FLAGGED: Graph = {
  nodes: [
    node(1),
    node(4, { kind: "Belief", title: "Warm cache reads win", judgement: "proven", confidence: 0.8 }),
    node(5, { kind: "Experiment", title: "Cache sweep" }),
    node(6, { kind: "Experiment", title: "Seed repeat" }),
  ],
  edges: [edge(5, 4, "proves", { valence: -0.9 }), edge(6, 4, "proves", { valence: -0.6 })],
};

test("a needs-attention row lights the beliefs it counts, names them under itself and fits to them; a belief opens in the briefing's place, and Escape hands the canvas back", async () => {
  answers.set(1000, FLAGGED);
  history.replaceState(null, "", "#/graph");
  const { user } = await shown("4 nodes, 2 edges");
  const flagged = () => document.querySelector<HTMLElement>(".proto-flagged");
  expect(flagged()).toBeNull();
  await user.click(screen.getByRole("button", { name: /1 verdict out of date/ }));
  // The set holds the light: the belief ringed and named, everything else dimmed, the frame on it alone.
  expect(halos()).toEqual(["Warm cache reads win: match"]);
  expect(dimmed()).toEqual(expect.arrayContaining(["Issue 1", "Cache sweep", "Seed repeat"]));
  expect(renderer.framed.at(-1)!.titles).toEqual(["Warm cache reads win"]);
  expect(flagged()!.textContent).toContain("2 against");
  // Reading one of them pushes the record onto the panel: one view, never two, as
  // the prototype's #side stack does. The lit set still holds the canvas.
  await user.click(within(flagged()!).getByRole("button", { name: /Warm cache reads win/ }));
  expect(peek()!.textContent).toContain("Belief#4");
  expect(flagged()).toBeNull();
  // The one being read wears the selection's ring; the set still holds the light.
  expect(halos()).toEqual(["Warm cache reads win: halo"]);
  expect(dimmed()).toEqual(expect.arrayContaining(["Cache sweep", "Seed repeat"]));
  await user.keyboard("{Escape}");
  await peekGone();
  expect(flagged()).not.toBeNull();
  expect(halos()).toEqual(["Warm cache reads win: match"]);
  await user.keyboard("{Escape}");
  expect(flagged()).toBeNull();
  expect(halos()).toEqual([]);
});

const collapsed = () => peek()?.getAttribute("data-collapsed");

test("Peek's button, or ], collapses it and frames the canvas again; the selection holds, and a click on another node selects it with Peek still collapsed, for the tab", async () => {
  const { user } = await shown();
  // Opening the panel frames the field into the room it leaves (the panel takes a
  // third of the stage), so a record is one frame before anything is collapsed.
  act(() => renderer.events!.click(renderer.node("Issue 1")));
  expect(renderer.refits).toHaveLength(1);
  await user.click(screen.getByRole("button", { name: "Collapse Peek" }));
  expect(collapsed()).toBe("true");
  expect(renderer.refits).toHaveLength(2);
  expect(halos()).toEqual(["Issue 1: halo"]);
  act(() => renderer.events!.click(renderer.node("Issue 2")));
  expect(within(peek()!).getAllByRole("heading").at(-1)!.textContent).toBe("Issue#2");
  expect(collapsed()).toBe("true");
  await user.keyboard("]");
  expect(collapsed()).toBe("false");
  expect(renderer.refits).toHaveLength(3);
  await user.keyboard("]");
  cleanup();
  renderer = new FakeRenderer();
  highlights = new HighlightStore();
  const again = await shown();
  act(() => renderer.events!.click(renderer.node("Issue 1")));
  expect(collapsed()).toBe("true");
  // With no selection, Peek is gone and ] does nothing.
  await again.user.keyboard("{Escape}]");
  await peekGone();
  expect(renderer.refits).toHaveLength(0);
});

const results = () => screen.queryByRole("complementary", { name: "Search results" });

test("the search results' button, or [, collapses them to a strip with their count and frames the canvas again; the box keeps its query and keys", async () => {
  const { user } = await shown();
  await user.click(search());
  fireEvent.change(search(), { target: { value: "issue" } });
  expect(search().getAttribute("aria-controls")).toBe(screen.getByRole("listbox", { name: "Matches" }).id);
  await user.click(screen.getByRole("button", { name: "Collapse search results" }));
  expect(results()!.classList).toContain("panel-strip");
  expect(results()!.textContent).toBe("2 matches");
  expect(screen.queryByRole("listbox", { name: "Matches" })).toBeNull();
  expect(search().getAttribute("aria-expanded")).toBe("false");
  expect(search().hasAttribute("aria-controls")).toBe(false);
  expect(renderer.refits).toHaveLength(1);
  expect(halos()).toEqual(["Issue 1: match", "Issue 2: match"]);
  await user.click(search());
  // Enter opens the match, and the panel over the canvas frames the field again.
  await user.keyboard("{Enter}");
  expect(within(peek()!).getAllByRole("heading").at(-1)!.textContent).toBe("Issue#2");
  expect(renderer.refits).toHaveLength(2);
  search().blur();
  // user-event spells the [ key "[[".
  await user.keyboard("[[");
  expect(screen.getByRole("listbox", { name: "Matches" })).toBeTruthy();
  expect(renderer.refits).toHaveLength(3);
});


// The same promise, kept by the view itself rather than by the functions under
// it (robust.test.ts): a graph from another producer draws what it can and
// never puts a crash screen over the app. The shapes are the ones that did.
test.each([
  ["a record with no date", (g: Graph): Graph => ({ ...g, nodes: g.nodes.map((n, at) => (at === 0 ? { ...n, created: undefined as unknown as string } : n)) })],
  ["a record with an unreadable date", (g: Graph): Graph => ({ ...g, nodes: g.nodes.map((n, at) => (at === 0 ? { ...n, created: "yesterday" } : n)) })],
  ["no dates at all", (g: Graph): Graph => ({ ...g, nodes: g.nodes.map((n) => ({ ...n, created: undefined as unknown as string })) })],
  ["an edge naming a record outside the window", (g: Graph): Graph => ({
    ...g,
    edges: [...g.edges, { from_id: uuid(9_999), to_id: g.nodes[0]!.id, edge_kind: "proves", valence: 0.6,
      asserted: "2026-09-20T01:00:00+00:00", actor: "ada@example.com" } as GraphEdge],
  })],
  ["a record with no title", (g: Graph): Graph => ({ ...g, nodes: g.nodes.map((n, at) => (at === 0 ? { ...n, title: undefined as unknown as string } : n)) })],
  ["a kind this build has never heard of", (g: Graph): Graph => ({ ...g, nodes: g.nodes.map((n, at) => (at === 0 ? { ...n, kind: "Hypothesis" } : n)) })],
  ["an edge kind this build has never heard of", (g: Graph): Graph => ({
    ...g,
    edges: [...g.edges, { from_id: g.nodes[1]!.id, to_id: g.nodes[0]!.id, edge_kind: "contradicts", valence: 0.6,
      asserted: "2026-09-20T01:00:00+00:00", actor: "ada@example.com" } as GraphEdge],
  })],
  ["no edges at all", (g: Graph): Graph => ({ ...g, edges: [] })],
  ["one record and nothing else", (g: Graph): Graph => ({ nodes: [g.nodes[0]!], edges: [] })],
  ["no records at all", (): Graph => ({ nodes: [], edges: [] })],
])("the graph draws a graph with %s instead of crashing", async (_name, bend) => {
  answers.set(1000, bend(GRAPH));
  history.replaceState(null, "", "#/graph");
  show();
  // The view settles on something: the field, or its own empty state. Never the
  // app's crash screen, which is what each of these used to produce.
  await waitFor(() => expect(screen.queryByText(/stopped on an error/i)).toBeNull());
  await act(async () => { await Promise.resolve(); });
  expect(screen.queryByText(/stopped on an error/i)).toBeNull();
});

test("full view gives the field the window: Chat folds, and what stands on the field comes along", async () => {
  // Half is Chat beside the graph; full is the graph with the window to itself —
  // not the panel over the field, which would be reading a record INSTEAD of
  // watching the graph rather than as well as.
  const { user } = await shown();
  expect(document.querySelector(".chat-strip")).toBeNull();
  await user.click(screen.getByRole("button", { name: "full" }));
  expect(screen.getByRole("button", { name: "half" })).toBeTruthy();
  // A record opened in full view keeps the field beside it, not under it.
  act(() => renderer.events!.click(renderer.node("Issue 1")));
  expect(peek()).not.toBeNull();
  expect(document.querySelector(".graph-peek")!.className).toBe("graph-peek");
  await user.click(screen.getByRole("button", { name: "half" }));
  expect(screen.getByRole("button", { name: "full" })).toBeTruthy();
});

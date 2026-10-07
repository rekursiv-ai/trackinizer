import { type QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { ALL_NODES, type Graph } from "../api/graph";
import { type Sent, stubFetch } from "../api/testing";
import { MetaContext } from "../app/boot";
import { HighlightContext, HighlightStore } from "../app/highlights";
import { CommandRegistry, CommandRegistryContext, Shortcuts } from "../commands/registry";
import { LiveContext } from "../live";
import { LiveHub } from "../live/hub";
import { testClient } from "../live/testing";
import { RouterProvider } from "../router/router";
import { SIDEBAR } from "../app/Sidebar";
import { type Panel, usePanel } from "../ui/panel";
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
    Peek: ({ row, panel, onClose }: { row: { kind: string; seq: number }; panel: Panel; onClose: () => void }) => (
      <aside id="peek" aria-label="Peek" data-collapsed={panel.collapsed}>
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

let client: QueryClient;
let renderer: FakeRenderer;
let highlights: HighlightStore;
let sent: Sent[];
/** What the server answers for each limit. */
let answers: Map<number, Graph | Promise<Response>>;

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
  sent = stubFetch(async (request) => {
    const answer = answers.get(Number(new URL(request.url).searchParams.get("limit")))!;
    return answer instanceof Promise ? answer : Response.json(answer);
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  sessionStorage.clear();
  document.documentElement.removeAttribute("style");
  document.documentElement.removeAttribute("data-theme");
  Reflect.deleteProperty(window, "trackinizer");
  history.replaceState(null, "", "/");
  vi.useRealTimers();
});

function show({ hub = null }: { hub?: LiveHub | null } = {}) {
  return render(
    <QueryClientProvider client={client}>
      <MetaContext value={META}>
        <RouterProvider kinds={META.kinds}>
          <LiveContext value={hub}>
            <CommandRegistryContext value={new CommandRegistry()}>
              <Shortcuts />
              <HighlightContext value={highlights}>
                <GraphView createRenderer={renderer.create} />
              </HighlightContext>
            </CommandRegistryContext>
          </LiveContext>
        </RouterProvider>
      </MetaContext>
    </QueryClientProvider>,
  );
}

/** Show the graph and wait for the count its first answer gives. */
async function shown(count = "3 nodes") {
  // Each user here sends its inputs back to back. By default user-event waits a
  // timer turn between them, some 20 ms a test, while React has already applied
  // each input before the next, as Testing Library wraps every event in act.
  const user = userEvent.setup({ delay: null });
  const view = show();
  await screen.findByText(count);
  return { user, ...view };
}

type User = ReturnType<typeof userEvent.setup>;
const limits = () => sent.map((request) => new URLSearchParams(request.query).get("limit"));
const count = () => document.querySelector(".graph-count")?.textContent;
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
  expect(container.querySelector(".view-tools .graph-count")?.textContent).toBe("3 nodes");
  expect(container.querySelector("select")).toBeNull();
});

const key = () => screen.queryByRole("complementary", { name: "Key" });
/** The key's items in its list headed `heading`, as text. */
const keyItems = (heading: string) => within(within(key()!).getByRole("list", { name: heading })).getAllByRole("listitem").map((item) => item.textContent);

test("the key lists the kinds loaded, and the links and statuses drawn, under Kinds, Links and Status, edges as the detail names them (S8)", async () => {
  answers.set(1000, {
    nodes: [node(1), node(2), node(3, { kind: "Paper", title: "A paper", status: "complete" })],
    edges: [edge(2, 1), edge(3, 2, "produced_by"), edge(3, 1, "favors", { valence: -0.5 })],
  });
  const { user } = await shown();
  expect(within(key()!).getAllByRole("heading").map((heading) => heading.textContent)).toEqual(["Kinds", "Links", "Status"]);
  expect(keyItems("Kinds")).toEqual(["Issues2", "Papers1"]);
  expect(keyItems("Links")).toEqual(["Narrows", "Produced by", "Against"]);
  expect(keyItems("Status")).toEqual(["Active", "Complete"]);
  await user.click(within(key()!).getByRole("button", { name: /^Issues/ }));
  // A kind hidden stays listed, without a count, to show it again; Links and Status are only what is drawn.
  expect(keyItems("Kinds")).toEqual(["Issues2", "Papers"]);
  expect(keyItems("Links")).toEqual(["Narrows"]);
  expect(keyItems("Status")).toEqual(["Active"]);
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
  const { user } = await shown("4 nodes");
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
  const { user } = await shown();
  await user.click(keyKind("Papers"));
  expect(renderer.shown()).toEqual(["A paper"]);
  const belief = node(4, { kind: "Belief", title: "A belief" });
  act(() => client.setQueryData(graphQuery(1000).queryKey, { ...GRAPH, nodes: [...GRAPH.nodes, belief] }));
  await screen.findByText(/ of 4 nodes$/);
  expect(renderer.shown()).toEqual(["A paper"]);
  expect(keyItems("Kinds")).toEqual(["Issues", "Papers1", "Beliefs"]);
  // With no Paper left, nothing is drawn, and the key stays to show the kinds loaded again.
  act(() => client.setQueryData(graphQuery(1000).queryKey, { nodes: [node(1), belief], edges: [] }));
  await screen.findByText("0 of 2 nodes");
  expect(keyItems("Kinds")).toEqual(["Issues", "Beliefs"]);
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
  expect(keyItems("Kinds")).toEqual(["Issues", "Papers1"]);
});

test("the tools' Key button hides and shows the key, pressed while it shows; which is kept for the tab", async () => {
  const { user } = await shown();
  const toggle = () => screen.getByRole("button", { name: "Key" });
  expect(toggle().getAttribute("aria-pressed")).toBe("true");
  expect(toggle().getAttribute("aria-controls")).toBe(key()!.id);
  expect(within(key()!).queryByRole("button", { name: /key/i })).toBeNull();
  await user.click(toggle());
  expect(key()).toBeNull();
  expect(toggle().getAttribute("aria-pressed")).toBe("false");
  cleanup();
  renderer = new FakeRenderer();
  highlights = new HighlightStore();
  const again = await shown();
  expect(key()).toBeNull();
  expect(toggle().getAttribute("aria-pressed")).toBe("false");
  await again.user.click(toggle());
  expect(key()).not.toBeNull();
});

test("Filter hides kinds through the lists' menu, each with its count, and shows them as a chip (S7)", async () => {
  const { user } = await shown();
  expect(await openMenu(user, "Filter")).toEqual(["Kind", "Status"]);
  await pick(user, "Kind");
  const kinds = screen.getAllByRole("option");
  expect(kinds.map((option) => [label(option), option.querySelector(".hint")?.textContent])).toEqual([
    ["Issues", "2"],
    ["Papers", "1"],
    ["Beliefs", "0"],
  ]);
  expect(kinds.map((option) => option.getAttribute("aria-selected"))).toEqual(["true", "true", "true"]);
  await pick(user, "Papers");
  expect(renderer.shown()).toEqual(["Issue 1", "Issue 2"]);
  expect(renderer.links.map((link) => link.hidden)).toEqual([false, true]);
  expect(count()).toBe("2 of 3 nodes");
  expect([...document.querySelectorAll(".fchip")].map((chip) => chip.textContent)).toEqual(["Kind is not Papers"]);
  expect(screen.getByRole("button", { name: /^Filter/ }).textContent).toBe("Filter1");
});

test("Filter hides statuses too; what it hides is kept for the tab, and a chip's close or Clear shows it again", async () => {
  answers.set(1000, { ...GRAPH, nodes: [...GRAPH.nodes, node(4, { status: "invalid", title: "Gone wrong" })] });
  const { user } = await shown("4 nodes");
  await openMenu(user, "Filter");
  await pick(user, "Status");
  await pick(user, "Invalid");
  expect(renderer.shown()).toEqual(["Issue 1", "Issue 2", "A paper"]);
  cleanup();
  renderer = new FakeRenderer();
  highlights = new HighlightStore();
  sessionStorage.setItem("trackinizer.v2.graph", JSON.stringify({ ...JSON.parse(sessionStorage.getItem("trackinizer.v2.graph")!), hiddenKinds: ["Paper"] }));
  const again = await shown("2 of 4 nodes");
  expect([...document.querySelectorAll(".fchip")].map((chip) => chip.textContent)).toEqual(["Kind is not Papers", "Status is not Invalid"]);
  await again.user.click(screen.getByRole("button", { name: "Remove the Kind filter" }));
  expect(renderer.shown()).toEqual(["Issue 1", "Issue 2", "A paper"]);
  await again.user.click(screen.getByRole("button", { name: "Clear" }));
  expect(renderer.shown()).toEqual(["Issue 1", "Issue 2", "A paper", "Gone wrong"]);
  expect(document.querySelector(".fchips")).toBeNull();
});

test("the node limit offers 100, 1k, 5k and All, 1k by default; a pick reads that many, frames them, and is kept for the tab", async () => {
  answers.set(100, { nodes: [node(1)], edges: [] });
  answers.set(ALL_NODES, GRAPH);
  const { user } = await shown();
  expect(await openMenu(user, "Nodes: 1k")).toEqual(["100 nodes", "1k nodes", "5k nodes", "All nodes"]);
  expect(screen.getAllByRole("option").map((option) => option.getAttribute("aria-selected"))).toEqual(["false", "true", "false", "false"]);
  await pick(user, "100 nodes");
  expect(await screen.findByText("1 node")).toBeTruthy();
  expect(limits()).toEqual(["1000", "100"]);
  expect(renderer.shown()).toEqual(["Issue 1"]);
  expect(renderer.sets).toEqual([true, true]);
  await openMenu(user, "Nodes: 100");
  await pick(user, "All nodes");
  expect(await screen.findByText("3 nodes")).toBeTruthy();
  expect(limits().at(-1)).toBe(String(ALL_NODES));
  cleanup();
  show();
  expect(await screen.findByRole("button", { name: "Nodes: All" })).toBeTruthy();
});

test("any count can be typed as the node limit, as 2500 or 2.5k; text that is no count offers nothing", async () => {
  answers.set(2500, { nodes: [node(1)], edges: [] });
  const { user } = await shown();
  await openMenu(user, "Nodes: 1k");
  await user.keyboard("a few");
  expect(screen.queryAllByRole("option")).toEqual([]);
  await user.clear(screen.getByRole("combobox", { name: "Nodes to show…" }));
  await user.keyboard("2.5k");
  expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual(["2,500 nodes"]);
  await user.keyboard("{Enter}");
  expect(await screen.findByText("1 node")).toBeTruthy();
  expect(limits()).toEqual(["1000", "2500"]);
  // The typed count is offered again beside the presets, ticked.
  expect(await openMenu(user, "Nodes: 2,500")).toEqual(["100 nodes", "1k nodes", "5k nodes", "All nodes", "2,500 nodes"]);
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

// Over 100 ms on x86: it reads and lays out 5,001 nodes twice.
test("over 5,000 nodes drawn once, a limit picked again asks again", { tags: ["manual"] }, async () => {
  const { user, asks } = await overFiveThousand();
  await user.click(screen.getByText("Draw them", { selector: "button" }));
  expect(renderer.counts).toEqual([5_001]);
  await openMenu(user, "Nodes: All");
  await pick(user, "1k nodes");
  await openMenu(user, "Nodes: 1k");
  await pick(user, "All nodes");
  await waitFor(() => expect(asks()).not.toBeNull());
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

test("Replay waits for Draw them too: a speed picked under the question draws nothing", async () => {
  keepAll();
  answers.set(ALL_NODES, many(5_001));
  const user = userEvent.setup({ delay: null });
  show();
  await screen.findByRole("heading", { name: "Draw 5,001 nodes?" });
  await openMenu(user, "Replay");
  vi.useFakeTimers();
  fireEvent.click(screen.getAllByRole("option")[2]!);
  act(() => vi.advanceTimersByTime(1_000));
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
  expect(await screen.findByText("5,000 nodes")).toBeTruthy();
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
  await pick(user, "100 nodes");
  await screen.findByText("1 node");
  await act(async () => answerSlow(Response.json(GRAPH)));
  expect(screen.getByText("1 node")).toBeTruthy();
  expect(renderer.shown()).toEqual(["Issue 7"]);
});

test("the zoom buttons zoom in, out and fit (F5)", async () => {
  const { user } = await shown();
  const zoom = within(screen.getByRole("group", { name: "Zoom" }));
  await user.click(zoom.getByRole("button", { name: "Zoom in" }));
  await user.click(zoom.getByRole("button", { name: "Zoom out" }));
  await user.click(zoom.getByRole("button", { name: "Fit" }));
  expect(renderer.zooms).toEqual([1.5, 1 / 1.5]);
  expect(renderer.fits).toBe(1);
});

test("every fit leaves out the strip on the canvas's right that Peek, the key while it shows, or the zoom buttons cover, whichever reaches furthest in; showing or hiding the key frames again", async () => {
  // jsdom lays nothing out: an 800 by 600 canvas, the zoom buttons 44 px in from
  // its right, the key above them 180 px in, and Peek 360 px wide.
  const boxes: [string, { left: number; top: number; right: number; bottom: number }][] = [
    [".graph-canvas", { left: 0, top: 0, right: 800, bottom: 600 }],
    [".graph-zoom", { left: 756, top: 480, right: 788, bottom: 588 }],
    [".graph-key", { left: 620, top: 12, right: 788, bottom: 400 }],
    [".graph-peek > *", { left: 440, top: 0, right: 800, bottom: 600 }],
  ];
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
    const box = boxes.find(([selector]) => this.matches(selector))?.[1] ?? { left: 0, top: 0, right: 0, bottom: 0 };
    return { ...box, x: box.left, y: box.top, width: box.right - box.left, height: box.bottom - box.top } as DOMRect;
  });
  const { user } = await shown();
  expect(renderer.covered!()).toEqual({ right: 180 });
  await user.click(screen.getByRole("button", { name: "Key" }));
  await user.click(screen.getByRole("button", { name: "Key" }));
  expect(renderer.refits).toEqual([
    { right: 44 },
    { right: 180 },
  ]);
  act(() => renderer.events!.click(renderer.node("Issue 1")));
  expect(renderer.covered!()).toEqual({ right: 360 });
});

test("an answer that changes only fields repaints; one that changes which inquiries show draws them anew", async () => {
  await shown();
  const before = renderer.repaints;
  act(() => client.setQueryData(graphQuery(1000).queryKey, { ...GRAPH, nodes: [node(1, { title: "Renamed" }), ...GRAPH.nodes.slice(1)] }));
  await waitFor(() => expect(renderer.shown()).toEqual(["Renamed", "Issue 2", "A paper"]));
  expect(renderer.sets).toEqual([true]);
  expect(renderer.repaints).toBeGreaterThan(before);
  act(() => client.setQueryData(graphQuery(1000).queryKey, { nodes: [node(1), node(2)], edges: [edge(2, 1)] }));
  expect(await screen.findByText("2 nodes")).toBeTruthy();
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
  expect(await screen.findByText("3 nodes")).toBeTruthy();
});

test("the stream keeps it current: its first open reads the graph again", async () => {
  const hub = new LiveHub(client);
  show({ hub });
  await screen.findByText("3 nodes");
  answers.set(1000, { nodes: [node(1)], edges: [] });
  act(() => hub.open());
  expect(await screen.findByText("1 node")).toBeTruthy();
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
  expect(within(peek()!).getByRole("heading").textContent).toBe("Issue#1");
  expect(halos()).toEqual(["Issue 1: halo"]);
  expect(dimmed()).toEqual(["A paper"]);
  act(() => renderer.events!.hover(renderer.node("A paper")));
  expect(dimmed()).toEqual(["A paper"]);
  await user.click(screen.getByRole("button", { name: "Close peek" }));
  expect(peek()).toBeNull();
  expect(dimmed()).toEqual(["Issue 1"]);
});

test("a click on the background, or Esc, clears the selection", async () => {
  const { user } = await shown();
  act(() => renderer.events!.click(renderer.node("Issue 2")));
  act(() => renderer.events!.click(null));
  expect(peek()).toBeNull();
  act(() => renderer.events!.click(renderer.node("Issue 2")));
  await user.keyboard("{Escape}");
  expect(peek()).toBeNull();
});

test("a link in Peek to a node the graph draws selects it in place and centres on it; Open and others navigate", async () => {
  await shown();
  act(() => renderer.events!.click(renderer.node("Issue 2")));
  // fireEvent answers false when the click's default, following the link, was prevented.
  expect(fireEvent.click(screen.getByRole("link", { name: "Parent by ref" }))).toBe(false);
  expect(within(peek()!).getByRole("heading").textContent).toBe("Issue#1");
  expect(fireEvent.click(screen.getByRole("link", { name: "Paper by id" }))).toBe(false);
  expect(within(peek()!).getByRole("heading").textContent).toBe("Paper#3");
  expect(renderer.centred).toEqual(["Issue 1", "A paper"]);
  expect(fireEvent.click(screen.getByRole("link", { name: "Open" }))).toBe(true);
  expect(fireEvent.click(screen.getByRole("link", { name: "Not drawn" }))).toBe(true);
  expect(fireEvent.click(screen.getByRole("link", { name: "Parent by ref" }), { metaKey: true })).toBe(true);
});

test("a Peek link to a node the graph loaded but does not draw navigates: one a filter hides, or Only these leaves out", async () => {
  const kept = { limit: 1000, hiddenKinds: ["Paper"], hiddenStatuses: [], skipEdges: [], only: false, key: true, roots: true };
  sessionStorage.setItem("trackinizer.v2.graph", JSON.stringify(kept));
  await shown("2 of 3 nodes");
  act(() => renderer.events!.click(renderer.node("Issue 2")));
  expect(fireEvent.click(screen.getByRole("link", { name: "Paper by id" }))).toBe(true);
  cleanup();
  // Within 1 hop of Issue 1 lie Issues 1 and 2; Only these hides the paper.
  sessionStorage.setItem("trackinizer.v2.graph", JSON.stringify({ ...kept, hiddenKinds: [], only: true }));
  history.replaceState(null, "", "#/graph?focus=Issue/1&hops=1");
  await shown("2 of 3 nodes");
  expect(within(peek()!).getByRole("heading").textContent).toBe("Issue#1");
  expect(fireEvent.click(screen.getByRole("link", { name: "Paper by id" }))).toBe(true);
});

test("a selected node the graph no longer has is no longer selected (R3-03)", async () => {
  await shown();
  act(() => renderer.events!.click(renderer.node("A paper")));
  act(() => client.setQueryData(graphQuery(1000).queryKey, { nodes: [node(1), node(2)], edges: [edge(2, 1)] }));
  await waitFor(() => expect(peek()).toBeNull());
  act(() => client.setQueryData(graphQuery(1000).queryKey, GRAPH));
  await screen.findByText("3 nodes");
  expect(peek()).toBeNull();
});

const search = () => screen.getByRole("combobox", { name: "Search the graph" });
const titles = (options: readonly Element[]) => options.map((option) => option.querySelector(".row-title")?.textContent);

test("search lights its matches on the canvas and lists them newest first; Enter centres on one, selects it, and keeps the query (G1-G3)", async () => {
  const { user } = await shown();
  await user.click(search());
  fireEvent.change(search(), { target: { value: "issue" } });
  expect(count()).toBe("2 of 3 nodes match");
  expect(titles(screen.getAllByRole("option"))).toEqual(["Issue 2", "Issue 1"]);
  expect(halos()).toEqual(["Issue 1: match", "Issue 2: match"]);
  await user.keyboard("{ArrowDown}{ArrowDown}");
  expect(titles([screen.getByRole("option", { selected: true })])).toEqual(["Issue 1"]);
  await user.keyboard("{ArrowUp}{Enter}");
  expect(within(peek()!).getByRole("heading").textContent).toBe("Issue#2");
  expect(renderer.centred).toEqual(["Issue 2"]);
  expect((search() as HTMLInputElement).value).toBe("issue");
  expect(titles(screen.getAllByRole("option"))).toEqual(["Issue 2", "Issue 1"]);
  // A click picks too.
  await user.click(screen.getAllByRole("option")[1]!);
  expect(within(peek()!).getByRole("heading").textContent).toBe("Issue#1");
});

test("search finds no node a filter hides, so a pick never centres on empty space (G4)", async () => {
  const kept = { limit: 1000, hiddenKinds: ["Paper"], hiddenStatuses: [], skipEdges: [], only: false, key: true, roots: true };
  sessionStorage.setItem("trackinizer.v2.graph", JSON.stringify(kept));
  await shown("2 of 3 nodes");
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
  await shown("4 nodes");
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
  expect(within(peek()!).getByRole("heading").textContent).toBe("Paper#3");
  fireEvent.change(search(), { target: { value: "nothing like it" } });
  expect(screen.getByRole("listbox", { name: "Matches" }).textContent).toContain("No matches");
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("listbox", { name: "Matches" })).toBeNull();
  expect((search() as HTMLInputElement).value).toBe("");
  expect(peek()).toBeNull();
  expect(document.activeElement).not.toBe(search());
});

test("Enter after a re-read leaves fewer matches than the marked one picks a match that is there (GR-01)", async () => {
  answers.set(1000, { nodes: [node(1), node(2), node(4)], edges: [] });
  const { user } = await shown();
  await user.click(search());
  fireEvent.change(search(), { target: { value: "issue" } });
  await user.keyboard("{ArrowDown}{ArrowDown}{ArrowDown}");
  act(() => client.setQueryData(graphQuery(1000).queryKey, { nodes: [node(1)], edges: [] }));
  await screen.findByText("1 of 1 node match");
  await user.keyboard("{Enter}");
  expect(within(peek()!).getByRole("heading").textContent).toBe("Issue#1");
});

test("the keys: f fits, / goes to search", async () => {
  const { user } = await shown();
  await user.keyboard("f");
  expect(renderer.fits).toBe(1);
  await user.keyboard("/");
  expect(document.activeElement).toBe(search());
  expect((document.activeElement as HTMLInputElement).value).toBe("");
});

test("Replay keeps its speeds in its own menu (F10): a pick grows the graph from nothing at that speed, and answers wait for it", async () => {
  const { user } = await shown();
  expect(await openMenu(user, "Replay")).toEqual(["0.25x", "0.5x", "1x", "2x", "4x", "10x"]);
  vi.useFakeTimers();
  fireEvent.click(screen.getAllByRole("option")[0]!);
  expect(renderer.counts).toEqual([3, 0]);
  act(() => vi.advanceTimersByTime(280));
  expect(renderer.counts).toEqual([3, 0, 1]);
  // While it runs, a pick changes its speed, and Stop is offered.
  fireEvent.click(screen.getByRole("button", { name: "Replaying at 0.25x" }));
  expect(screen.getAllByRole("option").map(label)).toContain("Stop");
  fireEvent.click(screen.getAllByRole("option")[5]!);
  // An answer that comes during the replay waits for it.
  act(() => client.setQueryData(graphQuery(1000).queryKey, { nodes: [node(1), node(2)], edges: [edge(2, 1)] }));
  // The wait after the first node was set at 0.25x; the next is a frame at 10x. Then the waiting answer draws.
  act(() => vi.advanceTimersByTime(280 + 16));
  expect(renderer.counts).toEqual([3, 0, 1, 2, 3, 2]);
  expect(renderer.sets).toEqual([true, false, false, false, true, false]);
  expect(renderer.shown()).toEqual(["Issue 1", "Issue 2"]);
  vi.useRealTimers();
});

test("a limit changed during Replay stops it, and the new limit's answer draws at once (GR-02)", async () => {
  answers.set(100, { nodes: [node(7)], edges: [] });
  const { user } = await shown();
  await openMenu(user, "Replay");
  await pick(user, "0.25x");
  await openMenu(user, "Nodes: 1k");
  await pick(user, "100 nodes");
  await screen.findByText("1 node");
  expect(renderer.shown()).toEqual(["Issue 7"]);
  expect(renderer.sets.at(-1)).toBe(true);
  expect(screen.getByRole("button", { name: "Replay" })).toBeTruthy();
});

// 1 <- 2 <- 3 <- 4 by narrows, 5 produced by 3, 6 apart.
const CHAIN: Graph = {
  nodes: [1, 2, 3, 4, 5, 6].map((n) => node(n)),
  edges: [edge(2, 1), edge(3, 2), edge(4, 3), edge(5, 3, "produced_by")],
};
const hopsButton = (name: RegExp) => within(screen.getByRole("group", { name: "Hops" })).getByRole("button", { name });

test("a double-click focuses a node: the hash keeps it, the focus row counts its hops, and what lies within 1 hop is lit", async () => {
  answers.set(1000, CHAIN);
  await shown("6 nodes");
  act(() => renderer.events!.doubleClick(renderer.node("Issue 2")));
  await waitFor(() => expect(location.hash).toBe("#/graph?focus=Issue/2&hops=1&group=none"));
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
  const { user } = await shown("6 nodes");
  act(() => renderer.events!.click(renderer.node("Issue 4")));
  await user.keyboard(".");
  await waitFor(() => expect(location.hash).toBe("#/graph?focus=Issue/4&hops=2"));
});

test("Hops step the focus 1 to 3 and All, each replacing the hash; what lies two or more hops out fades", async () => {
  answers.set(1000, CHAIN);
  history.replaceState(null, "", "#/graph?focus=Issue/1&hops=1");
  const { user } = await shown("6 nodes");
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
  const { user } = await shown("6 nodes");
  expect(renderer.framed.map((frame) => frame.titles)).toEqual([["Issue 1", "Issue 2"]]);
  await user.click(hopsButton(/^2 hops/));
  expect(renderer.framed.map((frame) => frame.titles).at(-1)).toEqual(["Issue 1", "Issue 2", "Issue 3"]);
  act(() => client.setQueryData(graphQuery(1000).queryKey, { ...CHAIN, nodes: CHAIN.nodes.map((row) => ({ ...row, title: `${row.title} renamed` })) }));
  await screen.findByText("Issue#1 · Issue 1 renamed");
  expect(renderer.framed).toHaveLength(2);
});

test("opened on a focus, as Show in graph opens it, the view selects the focus: Peek opens on it, and what it lights is framed beside Peek", async () => {
  // jsdom lays nothing out: an 800 by 600 canvas, the zoom buttons 44 px in from
  // its right, the key above them 180 px in, and Peek 360 px wide.
  const boxes: [string, { left: number; top: number; right: number; bottom: number }][] = [
    [".graph-canvas", { left: 0, top: 0, right: 800, bottom: 600 }],
    [".graph-zoom", { left: 756, top: 480, right: 788, bottom: 588 }],
    [".graph-key", { left: 620, top: 12, right: 788, bottom: 400 }],
    [".graph-peek > *", { left: 440, top: 0, right: 800, bottom: 600 }],
  ];
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
    const box = boxes.find(([selector]) => this.matches(selector))?.[1] ?? { left: 0, top: 0, right: 0, bottom: 0 };
    return { ...box, x: box.left, y: box.top, width: box.right - box.left, height: box.bottom - box.top } as DOMRect;
  });
  answers.set(1000, CHAIN);
  history.replaceState(null, "", "#/graph?focus=Issue/2&hops=1");
  await shown("6 nodes");
  expect(within(peek()!).getByRole("heading").textContent).toBe("Issue#2");
  expect(renderer.framed).toEqual([{ titles: ["Issue 1", "Issue 2", "Issue 3"], covered: { right: 360 } }]);
  expect(renderer.centred).toEqual([]);
  // A focus moved within the view selects nothing new.
  act(() => renderer.events!.doubleClick(renderer.node("Issue 4")));
  await waitFor(() => expect(location.hash).toBe("#/graph?focus=Issue/4&hops=1"));
  expect(within(peek()!).getByRole("heading").textContent).toBe("Issue#2");
});

test("a focus by an id in capitals finds its node, and the hash spells the id in lower case", async () => {
  const id = "abcdef12-3456-4789-8abc-def012345678";
  answers.set(1000, { nodes: [node(1), node(2, { id })], edges: [] });
  history.replaceState(null, "", `#/graph?focus=${id.toUpperCase()}&hops=1`);
  await shown("2 nodes");
  expect(screen.getByRole("group", { name: "Focus" }).querySelector(".graph-focus-name")?.textContent).toBe(`${id} · Issue 2`);
  expect(location.hash).toBe(`#/graph?focus=${id}&hops=1`);
});

test("Only these hides what the focus leaves out, and Dim the rest shows it again, dim", async () => {
  answers.set(1000, CHAIN);
  history.replaceState(null, "", "#/graph?focus=Issue/1&hops=2");
  const { user } = await shown("6 nodes");
  await user.click(screen.getByRole("button", { name: "Only these" }));
  expect(screen.getByRole("button", { name: "Only these" }).getAttribute("aria-pressed")).toBe("true");
  expect(renderer.shown()).toEqual(["Issue 1", "Issue 2", "Issue 3"]);
  await user.click(screen.getByRole("button", { name: "Dim the rest" }));
  expect(renderer.shown()).toEqual(["Issue 1", "Issue 2", "Issue 3", "Issue 4", "Issue 5", "Issue 6"]);
});

test("Through walks the focus over the edge kinds ticked, and the hop counts follow", async () => {
  answers.set(1000, CHAIN);
  history.replaceState(null, "", "#/graph?focus=Issue/3&hops=1&group=none");
  const { user } = await shown("6 nodes");
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
  const { user } = await shown("6 nodes");
  act(() => renderer.events!.click(renderer.node("Issue 2")));
  await user.keyboard("{Escape}");
  expect(peek()).toBeNull();
  expect(location.hash).toBe("#/graph?focus=Issue/1&hops=1");
  await user.keyboard("{Escape}");
  await waitFor(() => expect(location.hash).toBe("#/graph"));
  expect(screen.queryByRole("group", { name: "Focus" })).toBeNull();
  expect(dimmed()).toEqual([]);
  act(() => renderer.events!.doubleClick(renderer.node("Issue 4")));
  await user.click(await screen.findByRole("button", { name: "Clear the focus" }));
  await waitFor(() => expect(location.hash).toBe("#/graph"));
});

test("a focus on an inquiry the graph has not loaded says so and lights nothing", async () => {
  history.replaceState(null, "", "#/graph?focus=Issue/99&hops=2");
  await shown();
  expect(within(screen.getByRole("group", { name: "Focus" })).getByText("Issue#99 is not among the nodes loaded")).toBeTruthy();
  expect(dimmed()).toEqual([]);
});

// Two trees, 1 <- 2 and 3 <- 4, 3 <- 5, and 6 under no root; the newer tree, 3's, lists first.
const TREES: Graph = { nodes: [1, 2, 3, 4, 5, 6].map((n) => node(n)), edges: [edge(2, 1), edge(4, 3), edge(5, 3)] };
const rootsList = () => screen.queryByRole("listbox", { name: "Roots" });

test("the graph opens grouped by root: each root's subgraph is an island, the roots labelled and listed; Group by root turns it off and on, kept in the hash, back on with the keyboard in the roots", async () => {
  answers.set(1000, TREES);
  history.replaceState(null, "", "#/graph");
  const { user } = await shown("6 nodes");
  const button = () => screen.getByRole("button", { name: "Group by root" });
  expect(button().getAttribute("aria-pressed")).toBe("true");
  expect(Object.fromEntries(renderer.groups!)).toEqual({
    "Issue 1": uuid(1),
    "Issue 2": uuid(1),
    "Issue 3": uuid(3),
    "Issue 4": uuid(3),
    "Issue 5": uuid(3),
    "Issue 6": "unrooted",
  });
  expect(renderer.nodes.map((drawn) => drawn.look.label?.text ?? null)).toEqual(["Issue 1", null, "Issue 3", null, null, null]);
  expect(rootsList()).not.toBeNull();
  await user.click(button());
  await waitFor(() => expect(location.hash).toBe("#/graph?group=none"));
  expect(button().getAttribute("aria-pressed")).toBe("false");
  expect(renderer.groups).toBeNull();
  expect(rootsList()).toBeNull();
  expect(renderer.nodes.map((drawn) => drawn.look.label)).toEqual(Array(6).fill(null));
  await user.click(button());
  await waitFor(() => expect(location.hash).toBe("#/graph"));
  expect(renderer.groups?.get("Issue 4")).toBe(uuid(3));
  expect(document.activeElement).toBe(rootsList());
});

test("a root picked in the list opens in Peek and its island is framed beside Peek; Unrooted is only framed", async () => {
  answers.set(1000, TREES);
  history.replaceState(null, "", "#/graph");
  const { user } = await shown("6 nodes");
  rootsList()!.focus();
  await user.keyboard("jj{Enter}");
  expect(within(peek()!).getByRole("heading").textContent).toBe("Issue#1");
  expect(renderer.framed.map((frame) => frame.titles)).toEqual([["Issue 1", "Issue 2"]]);
  expect(renderer.centred).toEqual([]);
  // Beside Peek the list is a strip of glyphs, each named by its root.
  await user.click(screen.getByRole("option", { name: "Unrooted" }));
  expect(peek()).toBeNull();
  expect(renderer.framed.at(-1)).toEqual({ titles: ["Issue 6"], covered: { right: 0 } });
});

test("while grouped, Peek narrows the roots list to a strip that keeps its marked root and its keys; closing Peek widens it again", async () => {
  answers.set(1000, TREES);
  history.replaceState(null, "", "#/graph");
  const { user } = await shown("6 nodes");
  rootsList()!.focus();
  await user.keyboard("jj{Enter}");
  const panel = () => screen.getByRole("complementary", { name: "Roots" });
  expect(panel().classList.contains("is-compact")).toBe(true);
  expect(screen.queryByRole("searchbox", { name: "Filter roots" })).toBeNull();
  expect(within(rootsList()!).getByRole("option", { selected: true }).getAttribute("aria-label")).toBe("Issue#1 Issue 1");
  await user.keyboard("k{Enter}");
  expect(within(peek()!).getByRole("heading").textContent).toBe("Issue#3");
  await user.click(screen.getByRole("button", { name: "Close peek" }));
  expect(panel().classList.contains("is-compact")).toBe(false);
  expect(screen.getByRole("searchbox", { name: "Filter roots" })).toBeTruthy();
});

test("the graph's keys work from the roots list: . focuses on the root picked, / searches, and Esc clears the selection, then the focus", async () => {
  answers.set(1000, TREES);
  history.replaceState(null, "", "#/graph");
  const { user } = await shown("6 nodes");
  rootsList()!.focus();
  await user.keyboard("jj{Enter}.");
  await waitFor(() => expect(location.hash).toBe("#/graph?focus=Issue/1&hops=1"));
  expect(document.activeElement).toBe(rootsList());
  await user.keyboard("/");
  expect(document.activeElement).toBe(search());
  rootsList()!.focus();
  await user.keyboard("{Escape}");
  expect(peek()).toBeNull();
  await user.keyboard("{Escape}");
  await waitFor(() => expect(location.hash).toBe("#/graph"));
});

test("a root's frame takes its island, the nodes laid out nearest it, not every node under it", async () => {
  // 5 is under root 1, two hops down through 2, and under root 3, one hop down: its island is 3's.
  answers.set(1000, { nodes: [1, 2, 3, 4, 5].map((n) => node(n)), edges: [edge(2, 1), edge(4, 3), edge(5, 2), edge(5, 3, "produced_by")] });
  history.replaceState(null, "", "#/graph");
  const { user } = await shown("5 nodes");
  await user.click(screen.getByText("Issue 1"));
  expect(renderer.framed.at(-1)!.titles).toEqual(["Issue 1", "Issue 2"]);
});

test("while grouped, a search's matches take the left column, and the roots come back once it is emptied", async () => {
  answers.set(1000, TREES);
  history.replaceState(null, "", "#/graph");
  const { user } = await shown("6 nodes");
  fireEvent.change(search(), { target: { value: "issue 5" } });
  expect(rootsList()).toBeNull();
  expect(titles(screen.getAllByRole("option"))).toEqual(["Issue 5"]);
  await user.click(search());
  await user.keyboard("{Escape}");
  expect(rootsList()).not.toBeNull();
});

test("while grouped, the Roots list button, or [, hides the roots list and frames the canvas again in the width it gives back; the grouping stays, and the choice is kept for the tab", async () => {
  answers.set(1000, TREES);
  history.replaceState(null, "", "#/graph");
  const { user } = await shown("6 nodes");
  const toggle = () => screen.getByRole("button", { name: "Roots list" });
  const panel = () => screen.queryByRole("complementary", { name: "Roots" });
  expect(toggle().getAttribute("aria-pressed")).toBe("true");
  expect(toggle().getAttribute("aria-controls")).toBe(panel()!.id);
  await user.click(toggle());
  expect(panel()).toBeNull();
  expect(toggle().getAttribute("aria-pressed")).toBe("false");
  expect(renderer.refits).toHaveLength(1);
  expect(location.hash).toBe("#/graph");
  expect(renderer.groups?.get("Issue 4")).toBe(uuid(3));
  cleanup();
  renderer = new FakeRenderer();
  highlights = new HighlightStore();
  const again = await shown("6 nodes");
  expect(panel()).toBeNull();
  // user-event spells the [ key "[[".
  await again.user.keyboard("[[");
  expect(rootsList()).not.toBeNull();
  expect(renderer.refits).toHaveLength(1);
  await again.user.keyboard("[[");
  expect(rootsList()).toBeNull();
  // Ungrouped, there is no list to show, so neither the button nor [ does anything.
  await again.user.click(screen.getByRole("button", { name: "Group by root" }));
  await waitFor(() => expect(location.hash).toBe("#/graph?group=none"));
  expect(screen.queryByRole("button", { name: "Roots list" })).toBeNull();
  await again.user.keyboard("[[");
  expect(renderer.refits).toHaveLength(2);
});

const collapsed = () => peek()?.getAttribute("data-collapsed");

test("Peek's button, or ], collapses it and frames the canvas again; the selection holds, and a click on another node selects it with Peek still collapsed, for the tab", async () => {
  const { user } = await shown();
  act(() => renderer.events!.click(renderer.node("Issue 1")));
  expect(renderer.refits).toHaveLength(0);
  await user.click(screen.getByRole("button", { name: "Collapse Peek" }));
  expect(collapsed()).toBe("true");
  expect(renderer.refits).toHaveLength(1);
  expect(halos()).toEqual(["Issue 1: halo"]);
  act(() => renderer.events!.click(renderer.node("Issue 2")));
  expect(within(peek()!).getByRole("heading").textContent).toBe("Issue#2");
  expect(collapsed()).toBe("true");
  await user.keyboard("]");
  expect(collapsed()).toBe("false");
  expect(renderer.refits).toHaveLength(2);
  await user.keyboard("]");
  cleanup();
  renderer = new FakeRenderer();
  highlights = new HighlightStore();
  const again = await shown();
  act(() => renderer.events!.click(renderer.node("Issue 1")));
  expect(collapsed()).toBe("true");
  // With no selection, Peek is gone and ] does nothing.
  await again.user.keyboard("{Escape}]");
  expect(peek()).toBeNull();
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
  await user.keyboard("{Enter}");
  expect(within(peek()!).getByRole("heading").textContent).toBe("Issue#2");
  search().blur();
  // user-event spells the [ key "[[".
  await user.keyboard("[[");
  expect(screen.getByRole("listbox", { name: "Matches" })).toBeTruthy();
  expect(renderer.refits).toHaveLength(2);
});

test("while grouped and searching, [ collapses the search results and leaves the roots list as it was", async () => {
  answers.set(1000, TREES);
  history.replaceState(null, "", "#/graph");
  const { user } = await shown("6 nodes");
  fireEvent.change(search(), { target: { value: "issue 5" } });
  await user.keyboard("[[");
  expect(results()!.classList).toContain("panel-strip");
  fireEvent.change(search(), { target: { value: "" } });
  expect(rootsList()).not.toBeNull();
});

test("beside a collapsed Peek, the roots list keeps its width", async () => {
  answers.set(1000, TREES);
  history.replaceState(null, "", "#/graph");
  const { user } = await shown("6 nodes");
  rootsList()!.focus();
  await user.keyboard("jj{Enter}");
  const panel = () => screen.getByRole("complementary", { name: "Roots" });
  expect(panel().classList.contains("is-compact")).toBe(true);
  await user.click(screen.getByRole("button", { name: "Collapse Peek" }));
  expect(panel().classList.contains("is-compact")).toBe(false);
});

/** The app's sidebar button, as the shell holds it. */
function SidebarButton() {
  return (
    <button type="button" onClick={usePanel(SIDEBAR).toggle}>
      Sidebar
    </button>
  );
}

test("collapsing or expanding the app's sidebar frames the graph again in the width it changes", async () => {
  const { user } = await shown();
  render(<SidebarButton />);
  await user.click(screen.getByRole("button", { name: "Sidebar" }));
  expect(renderer.refits).toHaveLength(1);
  await user.click(screen.getByRole("button", { name: "Sidebar" }));
  expect(renderer.refits).toHaveLength(2);
});

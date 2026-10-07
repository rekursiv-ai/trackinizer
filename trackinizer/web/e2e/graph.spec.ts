import type { APIRequestContext, Page } from "@playwright/test";
import { expect, test, streamOpened, allowStreamErrors, STREAM_ROUTES } from "./fixtures";
import { expectControlSeen, longTasks, watchLongTasks } from "./longTasks";

// The graph view, `#/graph`. Its canvas cannot be read, so checks read what it
// draws through the console's `trackinizer.graph()`. The e2e server holds every
// spec file's inquiries, so each check finds its own by a title made unique
// here, and counts only what the page itself reports.

const TAG = crypto.randomUUID().slice(0, 8);

/** What the view draws, as `trackinizer.graph()` reports it. */
type Drawn = {
  nodes: { id: string; kind: string; seq: number; title: string; hidden: boolean; screen: { x: number; y: number } | null }[];
  links: { from: string; to: string; kind: string; valence?: number; hidden: boolean }[];
  selected: string | null;
  focus: string | null;
};

function drawn(page: Page): Promise<Drawn> {
  return page.evaluate(() => (window as unknown as { trackinizer: { graph(): Drawn } }).trackinizer.graph());
}

/**
 * Open the graph, at `hash`, once the live stream is connected and the graph has
 * drawn. By default ungrouped, one web: the graph opens grouped by root, and a
 * test of grouping turns it on itself.
 */
async function openGraph(page: Page, hash = "#/graph?group=none") {
  const subscribed = streamOpened(page);
  const read = page.waitForResponse((response) => response.url().includes("/api/web/graph"));
  await page.goto(`/app/${hash}`);
  await Promise.all([subscribed, read]);
  await expect(page.locator(".graph-count")).toHaveText(/\d nodes?$/);
}

/** Create `items` (kind and title) joined by `edges` (by index), in one batch; returns their ids. */
async function create(
  request: APIRequestContext,
  items: [string, string][],
  edges: { from_index: number; to_index: number; edge_kind: string; valence?: number }[] = [],
): Promise<string[]> {
  const response = await request.post("/api/inquiries/batch", {
    data: { items: items.map(([kind, title]) => ({ kind, title, idempotency_key: crypto.randomUUID() })), edges },
  });
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()).ids;
}

/**
 * Draw only the inquiries `ids` (the server's answer, cut down), so a check can
 * point at one: the shared server holds every spec file's, and they overlap.
 */
async function drawOnly(page: Page, ids: readonly string[]) {
  const kept = new Set(ids);
  await page.route("**/api/web/graph?*", async (route) => {
    const answer = await route.fetch();
    const graph = (await answer.json()) as { nodes: { id: string }[]; edges: { from_id: string; to_id: string }[] };
    const nodes = graph.nodes.filter((node) => kept.has(node.id));
    const edges = graph.edges.filter((edge) => kept.has(edge.from_id) && kept.has(edge.to_id));
    await route.fulfill({ response: answer, json: { nodes, edges } });
  });
}

/** What is drawn once the layout has stopped and the view has framed it: no node moved for a second. */
async function settled(page: Page): Promise<Drawn> {
  let last = "";
  await expect
    .poll(
      async () => {
        const now = JSON.stringify((await drawn(page)).nodes.map((node) => node.screen));
        const still = now === last;
        last = now;
        return still;
      },
      { intervals: [1_000], timeout: 20_000 },
    )
    .toBe(true);
  return drawn(page);
}

async function ok(response: Promise<{ ok(): boolean; text(): Promise<string> }>) {
  const answer = await response;
  expect(answer.ok(), await answer.text()).toBe(true);
}

test("the empty hash opens the graph, which draws the newest inquiries with their edges; the key counts each kind under Kinds, Links and Status, and Filter hides one", async ({
  page,
  request,
}) => {
  const [root, child, paper, belief] = await create(
    request,
    [
      ["Issue", `Graph root ${TAG}`],
      ["Issue", `Graph child ${TAG}`],
      ["Paper", `Graph paper ${TAG}`],
      ["Belief", `Graph belief ${TAG}`],
    ],
    [
      { from_index: 1, to_index: 0, edge_kind: "narrows" },
      { from_index: 2, to_index: 3, edge_kind: "proves", valence: -0.6 },
    ],
  );
  await openGraph(page, "");
  expect(new URL(page.url()).hash).toBe("#/graph");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Graph");
  // The home view opens grouped by root.
  await expect(page.getByRole("button", { name: "Group by root" })).toHaveAttribute("aria-pressed", "true");
  const graph = await drawn(page);
  const ours = graph.nodes.filter((node) => node.title.endsWith(TAG));
  expect(ours.map((node) => node.id).toSorted()).toEqual([root, child, paper, belief].toSorted());
  expect(graph.links).toContainEqual({ from: child, to: root, kind: "narrows", hidden: false });
  expect(graph.links).toContainEqual({ from: paper, to: belief, kind: "proves", valence: -0.6, hidden: false });
  await expect(page.locator(".graph-count")).toHaveText(`${graph.nodes.length.toLocaleString("en-US")} nodes`);

  const papers = graph.nodes.filter((node) => node.kind === "Paper").length;
  const key = page.getByRole("complementary", { name: "Key" });
  await expect(key.getByRole("heading")).toHaveText(["Kinds", "Links", "Status"]);
  await expect(key.getByRole("list", { name: "Kinds" }).getByRole("listitem").filter({ hasText: /^Papers/ })).toHaveText(`Papers${papers}`);
  await page.getByRole("button", { name: "Filter" }).click();
  await page.getByRole("option", { name: "Kind" }).click();
  await page.getByRole("option", { name: /^Papers/ }).click();
  await page.keyboard.press("Escape");
  await expect(page.locator(".fchip")).toHaveText("Kind is not Papers");
  const hidden = await drawn(page);
  expect(hidden.nodes.filter((node) => node.hidden).map((node) => node.kind)).toEqual(Array(papers).fill("Paper"));
  expect(hidden.links.find((link) => link.from === paper)?.hidden).toBe(true);
  await page.getByRole("button", { name: "Clear" }).click();
  expect((await drawn(page)).nodes.some((node) => node.hidden)).toBe(false);
});

test("another client's changes show within the interval: a new node, a rename, a valence, an edge removed, a purge", async ({
  page,
  request,
}) => {
  const [issue, paper, belief] = await create(
    request,
    [
      ["Issue", `Live issue ${TAG}`],
      ["Paper", `Live paper ${TAG}`],
      ["Belief", `Live belief ${TAG}`],
    ],
    [
      { from_index: 1, to_index: 2, edge_kind: "proves", valence: 0.5 },
      { from_index: 1, to_index: 0, edge_kind: "produced_by" },
    ],
  );
  await openGraph(page);
  const within = { timeout: 8_000 };
  const node = async (id: string) => (await drawn(page)).nodes.find((drawnNode) => drawnNode.id === id);
  const link = async (from: string, kind: string) => (await drawn(page)).links.find((drawnLink) => drawnLink.from === from && drawnLink.kind === kind);

  const [added] = await create(request, [["Issue", `Live added ${TAG}`]]);
  await expect.poll(async () => (await node(added!))?.title, within).toBe(`Live added ${TAG}`);

  await ok(request.put(`/api/inquiries/${issue}/title`, { data: { value: `Live renamed ${TAG}` } }));
  await expect.poll(async () => (await node(issue!))?.title, within).toBe(`Live renamed ${TAG}`);

  await ok(request.put(`/api/edges/${paper}/proves/${belief}/valence`, { data: { value: -0.9 } }));
  await expect.poll(async () => (await link(paper!, "proves"))?.valence, within).toBe(-0.9);

  await ok(request.delete(`/api/edges/${paper}/produced_by/${issue}`, { data: {} }));
  await expect.poll(async () => await link(paper!, "produced_by"), within).toBeUndefined();

  await ok(request.delete(`/api/inquiries/${belief}`, { data: {} }));
  await expect.poll(async () => await node(belief!), within).toBeUndefined();
  expect(await link(paper!, "proves")).toBeUndefined();
});

test("a change made while the stream was down shows once it reconnects (FR-02)", async ({ page, request, allowErrors }) => {
  // The stream is this test's own. Each connect opens and ends at once, so the
  // browser connects again every few seconds, and each open recovers what the
  // stream may have missed; while it is down, each connect fails.
  allowStreamErrors(allowErrors);
  let down = false;
  for (const stream of STREAM_ROUTES) {
    await page.route(stream, (route) =>
      down ? route.abort() : route.fulfill({ contentType: "text/event-stream", body: ": open\n\n" }),
    );
  }
  const [id] = await create(request, [["Issue", `Dropped issue ${TAG}`]]);
  await page.goto("/app/#/graph");
  await expect(page.locator(".graph-count")).toHaveText(/\d nodes?$/);
  const title = async () => (await drawn(page)).nodes.find((node) => node.id === id)?.title;
  expect(await title()).toBe(`Dropped issue ${TAG}`);
  down = true;
  await ok(request.put(`/api/inquiries/${id}/title`, { data: { value: `Renamed while down ${TAG}` } }));
  // With no frame and no connect, nothing reads the graph again: past the 2 s interval, it still shows the old title.
  await page.waitForTimeout(4_000);
  expect(await title()).toBe(`Dropped issue ${TAG}`);
  down = false;
  await expect.poll(title, { timeout: 15_000 }).toBe(`Renamed while down ${TAG}`);
});

test("hover shows what a node is; a click opens Peek on it; a Parents link to a drawn node selects it in place; Esc clears", async ({
  page,
  request,
}) => {
  const [root, child] = await create(
    request,
    [
      ["Issue", `Peek root ${TAG}`],
      ["Issue", `Peek child ${TAG}`],
    ],
    [{ from_index: 1, to_index: 0, edge_kind: "narrows" }],
  );
  await drawOnly(page, [root!, child!]);
  await openGraph(page);
  const at = (await settled(page)).nodes.find((node) => node.id === child)!.screen!;
  await page.mouse.move(at.x, at.y);
  await expect(page.getByRole("tooltip")).toHaveText(new RegExp(`^Issue#\\d+Peek child ${TAG}Click opens Peek`));
  await page.mouse.click(at.x, at.y);
  const peek = page.getByRole("complementary", { name: "Peek" });
  await expect(peek.locator(".d-title")).toHaveText(`Peek child ${TAG}`);

  await peek.getByRole("region", { name: "Parents", exact: true }).getByRole("link", { name: new RegExp(`Peek root ${TAG}`) }).click();
  await expect(peek.locator(".d-title")).toHaveText(`Peek root ${TAG}`);
  expect(new URL(page.url()).hash).toBe("#/graph?group=none");
  expect((await drawn(page)).selected).toBe(root);
  await page.keyboard.press("Escape");
  await expect(peek).toHaveCount(0);
});

test("/ goes to search; a title finds a drawn node and Enter selects it, keeping the query; a click on the background clears", async ({
  page,
  request,
}) => {
  const [id] = await create(request, [["Issue", `Searchable ${TAG}`]]);
  await drawOnly(page, [id!]);
  await openGraph(page);
  await settled(page);
  await page.keyboard.press("/");
  const search = page.getByRole("combobox", { name: "Search the graph" });
  await expect(search).toBeFocused();
  await page.keyboard.type(`searchable ${TAG}`);
  await expect(page.locator(".graph-count")).toHaveText("1 of 1 node match");
  await expect(page.getByRole("listbox", { name: "Matches" }).getByRole("option")).toHaveText([new RegExp(`Searchable ${TAG}`)]);
  await page.keyboard.press("Enter");
  const peek = page.getByRole("complementary", { name: "Peek" });
  await expect(peek.locator(".d-title")).toHaveText(`Searchable ${TAG}`);
  await expect(search).toHaveValue(`searchable ${TAG}`);
  // Centred in what Peek leaves of the canvas, not under it.
  const left = (await peek.boundingBox())!.x;
  await expect.poll(async () => (await settled(page)).nodes.find((node) => node.id === id)!.screen!.x).toBeLessThan(left);
  const box = (await page.locator(".graph-canvas").boundingBox())!;
  await page.mouse.click(box.x + 20, box.y + 20);
  await expect(peek).toHaveCount(0);
});

test("Replay grows the graph again from nothing, at the speed chosen", async ({ page, request }) => {
  const ids = await create(
    request,
    [
      ["Issue", `Replay first ${TAG}`],
      ["Issue", `Replay second ${TAG}`],
      ["Paper", `Replay third ${TAG}`],
    ],
    [{ from_index: 1, to_index: 0, edge_kind: "narrows" }],
  );
  await drawOnly(page, ids);
  await openGraph(page);
  await page.getByRole("button", { name: "Replay" }).click();
  await page.getByRole("option", { name: "0.25x" }).click();
  // 280 ms a node at 0.25x: the first read comes before the third node is back.
  expect((await drawn(page)).nodes.length).toBeLessThan(3);
  await expect.poll(async () => (await drawn(page)).nodes.map((node) => node.title)).toEqual(ids.map(() => expect.stringContaining(TAG)));
  expect((await drawn(page)).links.map((link) => link.kind)).toContain("narrows");
});

test("a double-click focuses a node, kept in the hash; hops 1 to 3 widen the light, and Only these hides the rest", async ({ page, request }) => {
  // A chain, each narrowing the one before: a <- b <- c <- d.
  const ids = await create(
    request,
    ["a", "b", "c", "d"].map((name): [string, string] => ["Issue", `Hop ${name} ${TAG}`]),
    [1, 2, 3].map((n) => ({ from_index: n, to_index: n - 1, edge_kind: "narrows" })),
  );
  await drawOnly(page, ids);
  await openGraph(page);
  const b = (await settled(page)).nodes.find((node) => node.id === ids[1])!;
  await page.mouse.dblclick(b.screen!.x, b.screen!.y);
  await expect.poll(() => new URL(page.url()).hash).toBe(`#/graph?focus=Issue/${b.seq}&hops=1&group=none`);
  expect((await drawn(page)).focus).toBe(ids[1]);
  const hops = page.getByRole("group", { name: "Hops" });
  await expect(hops.getByRole("button")).toHaveCount(4);
  expect(await hops.getByRole("button").evaluateAll((buttons) => buttons.map((button) => button.getAttribute("aria-label")))).toEqual([
    "1 hop, 3 nodes",
    "2 hops, 4 nodes",
    "3 hops, 4 nodes",
    "All hops, 4 nodes",
  ]);
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Only these" }).click();
  const hiddenTitles = async () => (await drawn(page)).nodes.filter((node) => node.hidden).map((node) => node.title);
  await expect.poll(hiddenTitles).toEqual([`Hop d ${TAG}`]);
  await hops.getByRole("button", { name: /^2 hops/ }).click();
  await expect.poll(hiddenTitles).toEqual([]);
  await hops.getByRole("button", { name: /^3 hops/ }).click();
  await expect.poll(() => new URL(page.url()).hash).toBe(`#/graph?focus=Issue/${b.seq}&hops=3&group=none`);
  await page.getByRole("button", { name: "Dim the rest" }).click();
});

test("a node limit typed above 5,000 reads that many", async ({ page }) => {
  await openGraph(page);
  await page.getByRole("button", { name: "Nodes: 1k" }).click();
  await page.keyboard.type("6000");
  const read = page.waitForResponse((response) => response.url().includes("/api/web/graph?limit=6000"));
  await page.getByRole("option", { name: "6,000 nodes" }).click();
  expect((await read).status()).toBe(200);
  await expect(page.getByRole("button", { name: "Nodes: 6,000" })).toBeVisible();
  await expect(page.locator(".graph-count")).toHaveText(/\d nodes?$/);
});

test("Group by root lists the roots with the keyboard in them; j to the second and Enter frames its island beside Peek, the list a strip", async ({
  page,
  request,
}) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  // Two trees made one after the other, so the second, newer one lists first.
  const ids = await create(
    request,
    [
      ["Issue", `Older root ${TAG}`],
      ["Issue", `Older leaf one ${TAG}`],
      ["Issue", `Older leaf two ${TAG}`],
      ["Issue", `Newer root ${TAG}`],
      ["Issue", `Newer leaf ${TAG}`],
    ],
    [
      { from_index: 1, to_index: 0, edge_kind: "narrows" },
      { from_index: 2, to_index: 0, edge_kind: "narrows" },
      { from_index: 4, to_index: 3, edge_kind: "narrows" },
    ],
  );
  await drawOnly(page, ids);
  await openGraph(page);
  await settled(page);
  await page.getByRole("button", { name: "Group by root" }).click();
  await expect.poll(() => new URL(page.url()).hash).toBe("#/graph");
  const roots = page.getByRole("listbox", { name: "Roots" });
  await expect(roots).toBeFocused();
  await page.keyboard.press("j");
  await page.keyboard.press("j");
  await expect(roots.getByRole("option", { selected: true })).toContainText(`Older root ${TAG}`);
  await page.keyboard.press("Enter");
  const peek = page.getByRole("complementary", { name: "Peek" });
  await expect(peek.locator(".d-title")).toHaveText(`Older root ${TAG}`);
  const canvas = (await page.locator(".graph-canvas").boundingBox())!;
  const right = (await peek.boundingBox())!.x;
  const island = (await settled(page)).nodes.filter((node) => ids.slice(0, 3).includes(node.id)).map((node) => node.screen!);
  for (const at of island) {
    expect(at.x).toBeGreaterThan(canvas.x);
    expect(at.x).toBeLessThan(right);
    expect(at.y).toBeGreaterThan(canvas.y);
    expect(at.y).toBeLessThan(canvas.y + canvas.height);
  }
  // Framed, the island spans much of what Peek leaves of the canvas, one way or the other.
  expect(extentOf(island)).toBeGreaterThan(Math.min(right - canvas.x, canvas.height) / 4);
  // Beside Peek the roots list is a strip, and the graph keeps at least half the view's width.
  const view = (await page.locator(".view.graph").boundingBox())!;
  expect((await roots.boundingBox())!.width).toBeLessThan(60);
  expect(right - canvas.x).toBeGreaterThanOrEqual(view.width / 2);
  await page.screenshot({ path: test.info().outputPath("grouped-peek-1280.png") });
  // The strip keeps its keys: k back to the newer root, and Enter opens it.
  await expect(roots).toBeFocused();
  await page.keyboard.press("k");
  await page.keyboard.press("Enter");
  await expect(peek.locator(".d-title")).toHaveText(`Newer root ${TAG}`);
  await peek.getByRole("button", { name: "Close peek" }).click();
  await expect.poll(async () => (await roots.boundingBox())!.width).toBeGreaterThan(300);

  // Roots list hides the list and keeps the islands; the canvas takes the list's
  // width, and the island last framed, the newer root's, is framed again in the
  // middle of what the key leaves of it. [ shows the list again.
  const toggle = page.getByRole("button", { name: "Roots list" });
  await expect(toggle).toHaveAttribute("aria-pressed", "true");
  const narrow = (await page.locator(".graph-canvas").boundingBox())!;
  await toggle.click();
  await expect(page.getByRole("complementary", { name: "Roots" })).toHaveCount(0);
  await expect(toggle).toHaveAttribute("aria-pressed", "false");
  expect(new URL(page.url()).hash).toBe("#/graph");
  const wide = (await page.locator(".graph-canvas").boundingBox())!;
  expect(wide.width).toBeGreaterThan(narrow.width + 300);
  const middle = wide.x + ((await page.getByRole("complementary", { name: "Key" }).boundingBox())!.x - wide.x) / 2;
  await expect
    .poll(async () => {
      const newer = (await settled(page)).nodes.filter((node) => ids.slice(3).includes(node.id)).map((node) => node.screen!.x);
      return Math.abs((Math.min(...newer) + Math.max(...newer)) / 2 - middle);
    })
    .toBeLessThan(20);
  await page.screenshot({ path: test.info().outputPath("grouped-list-hidden-1280.png") });
  await page.keyboard.press("[");
  await expect(roots).toBeVisible();
  await expect(toggle).toHaveAttribute("aria-pressed", "true");
});

/** How far `points` spread, along whichever of x or y they spread further. */
function extentOf(points: readonly { x: number; y: number }[]): number {
  const extent = (along: (at: { x: number; y: number }) => number) => Math.max(...points.map(along)) - Math.min(...points.map(along));
  return Math.max(extent((at) => at.x), extent((at) => at.y));
}

test("a fit keeps every node clear of the key; the tools' Key button hides it, and the graph is framed again in the room it leaves", async ({ page, request }) => {
  // Narrow and short: the key scrolls in the canvas's top right, and the frame is bound by the width it leaves.
  await page.setViewportSize({ width: 900, height: 500 });
  const ids = await create(
    request,
    [
      ["Issue", `Key root ${TAG}`],
      ["Issue", `Key child one ${TAG}`],
      ["Issue", `Key child two ${TAG}`],
      ["Belief", `Key belief ${TAG}`],
      ["Paper", `Key paper for ${TAG}`],
      ["Paper", `Key paper against ${TAG}`],
      ["Experiment", `Key experiment ${TAG}`],
      ["Issue", `Key grandchild ${TAG}`],
    ],
    [
      { from_index: 1, to_index: 0, edge_kind: "narrows" },
      { from_index: 2, to_index: 0, edge_kind: "narrows" },
      { from_index: 3, to_index: 0, edge_kind: "produced_by" },
      { from_index: 4, to_index: 3, edge_kind: "proves", valence: 0.6 },
      { from_index: 5, to_index: 3, edge_kind: "proves", valence: -0.6 },
      { from_index: 6, to_index: 1, edge_kind: "produced_by" },
      { from_index: 7, to_index: 1, edge_kind: "narrows" },
    ],
  );
  await drawOnly(page, ids);
  await openGraph(page);
  const key = page.getByRole("complementary", { name: "Key" });
  const box = (await key.boundingBox())!;
  const screens = (await settled(page)).nodes.map((node) => node.screen!);
  expect(screens.filter((at) => at.x >= box.x && at.x <= box.x + box.width && at.y >= box.y && at.y <= box.y + box.height)).toEqual([]);
  const toggle = page.getByRole("button", { name: "Key", exact: true });
  await expect(toggle).toHaveAttribute("aria-pressed", "true");
  await toggle.click();
  await expect(key).toHaveCount(0);
  await expect(toggle).toHaveAttribute("aria-pressed", "false");
  const right = Math.max(...screens.map((at) => at.x));
  await expect.poll(async () => Math.max(...(await settled(page)).nodes.map((node) => node.screen!.x))).toBeGreaterThan(right + 20);
  // Kept for the tab: a reload keeps it hidden.
  await page.reload();
  await expect(page.locator(".graph-count")).toHaveText(/\d nodes?$/);
  await expect(toggle).toHaveAttribute("aria-pressed", "false");
  await expect(key).toHaveCount(0);
});

// A measurement more than a check: frame rates swing with what else the machine
// runs (on a loaded Mac, 14 to 47 fps for the same build), so it runs only when
// asked, on a quiet machine: GRAPH_FPS=1, or 4 for a 4x slower CPU, which only
// reports (v1 drew 25 fps there).
test("1,000 nodes draw at 60 fps while the layout runs, with no long task after the answer", async ({ page }) => {
  test.skip(!process.env.GRAPH_FPS, "A measurement: run with GRAPH_FPS=1 (or 4) on a quiet machine.");
  const cpu = Number(process.env.GRAPH_FPS);
  await (await page.context().newCDPSession(page)).send("Emulation.setCPUThrottlingRate", { rate: cpu });
  await page.route("**/api/web/graph?*", (route) => route.fulfill({ json: syntheticGraph(1_000) }));
  await watchLongTasks(page);
  await page.addInitScript(() => {
    const frames: number[] = [];
    const tick = (time: number) => {
      frames.push(time);
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
    Object.assign(window, { frames: () => frames });
  });
  await page.goto("/app/#/graph");
  await expect(page.locator(".graph-count")).toHaveText("1,000 nodes");
  const answered = await page.evaluate(
    () =>
      performance
        .getEntriesByType("resource")
        .find((entry): entry is PerformanceResourceTiming => entry.name.includes("/api/web/graph"))!.responseEnd,
  );
  await page.waitForTimeout(SAMPLE_MS);
  const frames = (await page.evaluate(() => (window as unknown as { frames: () => number[] }).frames())).filter(
    (time) => time > answered && time <= answered + SAMPLE_MS,
  );
  const gaps = frames.slice(1).map((time, index) => time - frames[index]!).toSorted((a, b) => a - b);
  const tasks = (await longTasks(page)).filter((task) => task.start + task.ms > answered);
  const fps = (frames.length * 1000) / SAMPLE_MS;
  console.log(`graph 1k at ${cpu}x: ${fps.toFixed(1)} fps, p95 frame ${gaps[Math.floor(gaps.length * 0.95)]?.toFixed(1)} ms, long tasks ${JSON.stringify(tasks)}`);
  if (cpu > 1) return;
  expect(fps).toBeGreaterThanOrEqual(55);
  expect(tasks).toEqual([]);
  await expectControlSeen(page);
});

/** How long frames are counted after the answer: the layout runs about 170 ticks, near 3 s at 60 fps. */
const SAMPLE_MS = 2_500;

/**
 * A graph of `count` nodes shaped as graph.md's benchmark: a random forest over
 * the nine kinds, plus 0.6 extra edges per node, citations with a valence.
 */
function syntheticGraph(count: number) {
  let seed = 7;
  const random = () => (seed = (seed * 1_103_515_245 + 12_345) % 2_147_483_648) / 2_147_483_648;
  const kinds = ["Issue", "Belief", "Experiment", "Paper", "CodeChange", "WebResult", "WebSearch", "AgentSession", "Artifact"];
  const nodes = Array.from({ length: count }, (_, n) => ({
    id: `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`,
    kind: kinds[n % kinds.length]!,
    seq: n,
    title: `Synthetic ${n}`,
    status: n % 11 === 0 ? "complete" : "active",
    created: new Date(Date.UTC(2026, 0, 1, 0, 0, n)).toISOString(),
    ...(n % kinds.length === 1 && { judgement: "unproven", confidence: 0.6 }),
  }));
  const edge = (from: number, to: number, citation: boolean) => ({
    from_id: nodes[from]!.id,
    to_id: nodes[to]!.id,
    edge_kind: citation ? "proves" : "produced_by",
    ...(citation && { valence: random() * 2 - 1 }),
  });
  const pairs = new Set<string>();
  const edges = [];
  for (let n = 1; n < count; n++) edges.push(edge(n, Math.floor(random() * n), false));
  for (let n = 0; n < count * 0.6; n++) {
    const [from, to] = [Math.floor(random() * count), Math.floor(random() * count)];
    if (from !== to && !pairs.has(`${from} ${to}`)) edges.push(edge(from, to, true));
    pairs.add(`${from} ${to}`);
  }
  return { nodes, edges };
}

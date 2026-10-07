import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { APIRequestContext, Page } from "@playwright/test";
import { expect, test } from "../fixtures";
import { resetCanvas } from "../canvasState";

// The canvas's push bar: from the server accepting an operation to the visual
// painted in the browser, under 100 ms at p50 and p90 for every visual type, and
// from a navigation to the page drawn with its data. A
// script holding no special rights (the e2e server runs --no-auth) applies 20
// operations per visual type through POST /api/workspaces/{id}/operations; the
// page records when each pushed frame arrived and when the visuals it changed painted
// (`trackinizer.timings()`, src/debug/timings.ts). Both clocks are the one
// machine's, so `paint wall time - frame t` is exact.
//
// A navigation is an agent's operation, and the server refuses it to anyone but
// an API key, which the --no-auth server this suite runs against has none of. So
// the script stands in for the server on that one leg: it hands the page's
// stream a `navigate` frame stamped with its own clock, and the page's side
// (frame to the target drawn with its data) is what the number measures, not
// the server's.

test.use({ canvas: true });

/** What a spec leaves on the shared canvas would change the layout of the next one. */
test.afterEach(async ({ request }) => {
  await resetCanvas(request);
});

const OPERATIONS = 20;
const BAR_MS = 100;
const OUT = "/opt/scratch/artifacts/trackinizer-web/canvas-chat/timings";

type Mark = { type: string; kind: "paint" | "data" | "page"; wall: number };
type Frame = { frame: "workspace" | "navigate"; revision: number | null; route: string | null; t: number; receivedWall: number; marks: Mark[] };
type Placement = "main" | "side" | "floating";
type Operation =
  | { kind: "show"; visual_type: string; record_id?: string; placement?: Placement }
  | { kind: "navigate"; route: string };
type Cell = { n: number; paint: Stats | null; data: Stats | null; page: Stats | null; first: { paint: number | null; data: number | null; page: number | null } };
type Stats = { p50: number; p90: number; max: number };

/** Nearest-rank percentile of `values`, as milliseconds to one decimal. */
function percentile(values: readonly number[], fraction: number): number {
  const sorted = [...values].sort((left, right) => left - right);
  return Math.round(sorted[Math.max(0, Math.ceil(fraction * sorted.length) - 1)]! * 10) / 10;
}

const stats = (values: readonly number[]): Stats =>
  ({ p50: percentile(values, 0.5), p90: percentile(values, 0.9), max: Math.round(Math.max(...values) * 10) / 10 });

async function ok<T>(response: { ok(): boolean; text(): Promise<string>; json(): Promise<unknown> }): Promise<T> {
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()) as T;
}

/** One Issue and one Artifact per operation, so no visual shows a record its data is already cached for. */
async function seed(request: APIRequestContext): Promise<{ issues: string[]; artifacts: string[] }> {
  const tag = crypto.randomUUID().slice(0, 8);
  const { ids } = await ok<{ ids: string[] }>(await request.post("/api/inquiries/batch", {
    data: {
      items: Array.from({ length: OPERATIONS }, (_, n) => ({ kind: "Issue", title: `Canvas push ${tag} ${n}`, idempotency_key: crypto.randomUUID() })),
      edges: [],
    },
  }));
  const artifacts: string[] = [];
  for (const [n, issue] of ids.entries()) {
    const published = await ok<{ artifact_id: string }>(await request.post("/api/artifacts/content", {
      headers: { "Idempotency-Key": crypto.randomUUID() },
      data: {
        issue_id: issue, title: `Canvas push artifact ${tag} ${n}`, summary: "A report.", format: "structured",
        sections: [{ title: "Finding", summary: "One finding.", details: "Details.", findings: [] }],
        citations: [{ record_id: issue }],
      },
    }));
    artifacts.push(published.artifact_id);
  }
  return { issues: ids, artifacts };
}

/** Keep the page's event sources where the script can reach them. */
function exposeEventSources() {
  const Original = window.EventSource;
  const held: EventSource[] = [];
  Object.assign(window, { heldEventSources: held });
  window.EventSource = class extends Original {
    constructor(url: string | URL, init?: EventSourceInit) {
      super(url, init);
      held.push(this);
    }
  };
}

/** Hand the page's workspace stream a `navigate` frame to `route`, stamped now. */
async function pushNavigate(page: Page, route: string): Promise<void> {
  await page.evaluate((route) => {
    const sources = (window as unknown as { heldEventSources: EventSource[] }).heldEventSources;
    const stream = sources.find((source) => source.url.includes("/events"))!;
    stream.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ type: "navigate", route, t: Date.now() }) }));
  }, route);
}

/** Wait until the page holds the frame `pick` finds, with the marks `kinds` for `type`. */
async function frameWith(page: Page, pick: { revision: number } | { route: string }, type: string, kinds: readonly string[]): Promise<Frame> {
  const find = ({ pick, type, kinds }: { pick: { revision?: number; route?: string }; type: string; kinds: readonly string[] }) => {
    const timings = (window as unknown as { trackinizer: { timings(): Frame[] } }).trackinizer.timings();
    const frame = timings.findLast((entry) => (pick.route !== undefined ? entry.frame === "navigate" && entry.route === pick.route
      : entry.frame === "workspace" && entry.revision === pick.revision));
    return frame && kinds.every((kind) => frame.marks.some((mark) => mark.type === type && mark.kind === kind)) ? frame : null;
  };
  const handle = await page.waitForFunction(find, { pick, type, kinds }, { timeout: 15_000 });
  return handle.jsonValue() as Promise<Frame>;
}

test("with the canvas on the tab holds one stream, the canvas's, through Settings and back", async ({ page, request }) => {
  await page.addInitScript(exposeEventSources);
  await ok(await request.put("/api/me/visual-workspace", { data: { enabled: true } }));
  const open = () => page.evaluate(() => (window as unknown as { heldEventSources: EventSource[] }).heldEventSources
    .filter((source) => source.readyState !== 2).map((source) => new URL(source.url).pathname.replace(/[0-9a-f-]{36}/, "<id>")));
  await page.goto("/app/#/list/Issue");
  await expect(page.getByRole("toolbar", { name: "Canvas controls" })).toBeVisible();
  await page.waitForFunction(() => (window as unknown as { trackinizer: { timings(): unknown[] } }).trackinizer.timings().length > 0);
  expect(await open()).toEqual(["/api/workspaces/<id>/events"]);
  await page.goto("/app/#/settings");
  await expect(page.getByRole("heading", { name: /settings/i }).first()).toBeVisible();
  expect(await open()).toEqual(["/api/workspaces/<id>/events"]);
  await page.goto("/app/#/list/Issue");
  await expect(page.getByRole("toolbar", { name: "Canvas controls" })).toBeVisible();
  expect(await open()).toEqual(["/api/workspaces/<id>/events"]);
});

test("a visual of every type paints within 100 ms of the server accepting its operation (p50 and p90)", async ({ page, request }) => {
  test.setTimeout(180_000);
  await page.addInitScript(exposeEventSources);
  const { issues, artifacts } = await seed(request);
  await ok(await request.put("/api/me/visual-workspace", { data: { enabled: true } }));
  let workspace = await ok<{ id: string; revision: number; visuals: { id: string; type: string }[] }>(await request.post("/api/workspaces"));
  // Start from the page alone, whatever an earlier spec left on the canvas: an
  // operation that changes nothing pushes no frame to time.
  for (const visual of workspace.visuals.filter((held) => held.type !== "trax.browse")) {
    workspace = await ok(await request.post(`/api/workspaces/${workspace.id}/operations`, {
      headers: { "Idempotency-Key": crypto.randomUUID() },
      data: { revision: workspace.revision, operation: { kind: "hide", instance_id: visual.id } },
    }));
  }
  // The page must not see a stale browse pane from a previous run's canvas.
  await page.goto("/app/#/list/Issue");
  await expect(page.getByRole("toolbar", { name: "Canvas controls" })).toBeVisible();
  // The stream's first frame is the canvas as it stands: the page is listening.
  await page.waitForFunction(() => (window as unknown as { trackinizer: { timings(): unknown[] } }).trackinizer.timings().length > 0);
  let revision = (await ok<{ revision: number }>(await request.get(`/api/workspaces/${workspace.id}`))).revision;

  /** Apply one operation to the server; the canvas's revision after it. */
  const apply = async (operation: Operation): Promise<number> => {
    const state = await ok<{ revision: number }>(await request.post(`/api/workspaces/${workspace.id}/operations`, {
      headers: { "Idempotency-Key": crypto.randomUUID() },
      data: { revision, operation },
    }));
    revision = state.revision;
    return revision;
  };

  // The page is not a visual: an agent moves it with a navigation, whose mark is
  // the target's page drawn with its data (`page`).
  const types: { type: string; mark: string; operation: (n: number) => Operation; kinds: ("paint" | "data" | "page")[] }[] = [
    { type: "page", mark: "page", operation: (n) => ({ kind: "navigate", route: `#/lookup/${issues[n]!}` }), kinds: ["page"] },
    { type: "trax.chat", mark: "trax.chat", operation: (n) => ({ kind: "show", visual_type: "trax.chat", placement: n % 2 ? "side" : "main" }), kinds: ["paint"] },
    { type: "trax.subgraph", mark: "trax.subgraph", operation: (n) => ({ kind: "show", visual_type: "trax.subgraph", record_id: issues[n]! }), kinds: ["paint", "data"] },
    { type: "trax.timeline", mark: "trax.timeline", operation: (n) => ({ kind: "show", visual_type: "trax.timeline", record_id: issues[n]! }), kinds: ["paint", "data"] },
    { type: "trax.artifact", mark: "trax.artifact", operation: (n) => ({ kind: "show", visual_type: "trax.artifact", record_id: artifacts[n]! }), kinds: ["paint", "data"] },
  ];
  const table: { [type: string]: Cell } = {};
  for (const { type, mark, operation, kinds } of types) {
    const paints: number[] = [];
    const loads: number[] = [];
    const pages: number[] = [];
    for (let n = 0; n < OPERATIONS; n++) {
      const op = operation(n);
      let frame: Frame;
      if (op.kind === "navigate") {
        await pushNavigate(page, op.route);
        frame = await frameWith(page, { route: op.route }, mark, kinds);
      } else {
        frame = await frameWith(page, { revision: await apply(op) }, mark, kinds);
      }
      const wall = (kind: Mark["kind"]) => frame.marks.find((held) => held.type === mark && held.kind === kind)!.wall - frame.t;
      if (kinds.includes("paint")) paints.push(wall("paint"));
      if (kinds.includes("data")) loads.push(wall("data"));
      if (kinds.includes("page")) pages.push(wall("page"));
    }
    const first = (values: number[]) => (values.length ? Math.round(values[0]! * 10) / 10 : null);
    table[type] = {
      n: OPERATIONS,
      paint: paints.length ? stats(paints) : null,
      data: loads.length ? stats(loads) : null,
      page: pages.length ? stats(pages) : null,
      first: { paint: first(paints), data: first(loads), page: first(pages) },
    };
  }

  mkdirSync(OUT, { recursive: true });
  writeFileSync(join(OUT, "canvas-push.json"), `${JSON.stringify({ unit: "ms", bar: BAR_MS, operations: OPERATIONS, page: "navigate frame handed to the page by the script (no agent key on --no-auth); the page side only", cells: table }, null, 2)}\n`);
  for (const [type, cell] of Object.entries(table)) {
    for (const kind of ["paint", "data", "page"] as const) {
      const held = cell[kind];
      if (!held) continue;
      expect(held.p50, `${type} ${kind} p50`).toBeLessThan(BAR_MS);
      expect(held.p90, `${type} ${kind} p90`).toBeLessThan(BAR_MS);
    }
  }
});

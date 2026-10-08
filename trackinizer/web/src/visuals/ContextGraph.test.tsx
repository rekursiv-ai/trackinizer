import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { FocusGraph, FocusNode } from "../api/graph";
import { type Sent, stubFetch } from "../api/testing";
import type { WorkspaceState } from "../api/workspaces";
import { MetaContext } from "../app/boot";
import { edge, FakeRenderer, META, node, uuid } from "../graph/testing";
import { testClient } from "../live/testing";
import { CONTEXT_LIMIT, ContextGraph, contextLens, contextParams } from "./ContextGraph";
import { canvasActions } from "./testing";
import { type WorkspaceActions, WorkspaceActionsProvider } from "./workspaceActions";

/** Inquiry `n`, `hops` from the focus, as the focus read sends it. */
function at(n: number, hops: number, fields: Partial<FocusNode> = {}): FocusNode {
  return { ...node(n), hops, ...fields };
}

// Issue 1 is the record; 2 and 3 are a hop out, 4 two, 5 three: the dimmed ring.
const AROUND: FocusGraph = {
  nodes: [at(1, 0), at(2, 1), at(3, 1), at(4, 2), at(5, 3)],
  edges: [edge(2, 1), edge(1, 3), edge(4, 2), edge(5, 4)],
};

let renderer: FakeRenderer;
let sent: Sent[];
let answer: () => Response;

beforeEach(() => {
  renderer = new FakeRenderer();
  answer = () => Response.json(AROUND);
  sent = stubFetch(() => answer());
  history.replaceState(null, "", "#/ref/Issue/1");
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  history.replaceState(null, "", "/");
});

const graphReads = () => sent.filter((request) => request.path === "/api/web/graph");

function instance(recordId: string | null, params: WorkspaceState["visuals"][number]["params"] = {}): WorkspaceState["visuals"][number] {
  return { id: "graph-instance", type: "trax.subgraph", version: 1, placement: "main", record_id: recordId, params };
}

function actions(fields: Partial<WorkspaceActions> = {}): WorkspaceActions {
  return canvasActions({ visualTypes: new Set(["trax.subgraph"]), ...fields });
}

function show(pane: WorkspaceState["visuals"][number], workspace: WorkspaceActions | null = actions()) {
  const client = testClient();
  const tree = (shown: WorkspaceState["visuals"][number]) => (
    <QueryClientProvider client={client}>
      <MetaContext value={META}>
        <WorkspaceActionsProvider value={workspace}>
          <ContextGraph instance={shown} workspace={null} onWorkspaceChanged={() => {}} focused createRenderer={renderer.create} />
        </WorkspaceActionsProvider>
      </MetaContext>
    </QueryClientProvider>
  );
  const view = render(tree(pane));
  return { ...view, show: (next: WorkspaceState["visuals"][number]) => view.rerender(tree(next)) };
}

test("one read of the record's neighbourhood, a hop past what it lights, draws the record in context", async () => {
  show(instance(uuid(1)));
  await waitFor(() => expect(renderer.shown()).toHaveLength(5));
  expect(graphReads().map((request) => request.query)).toEqual([`?focus=${uuid(1)}&hops=3&limit=${CONTEXT_LIMIT}`]);
  // Nothing else is read: no detail per node, as the old drawing made.
  expect(sent).toHaveLength(1);
  // It frames what it lights, the record and two hops out, once laid out.
  expect(renderer.settleFrames).toEqual([["Issue 1", "Issue 2", "Issue 3", "Issue 4"]]);
  expect(screen.getByText("Issue#1")).toBeTruthy();
  expect(screen.getByText("4 nodes within 2 hops")).toBeTruthy();
});

test("lights what lies within its hops, dims the ring past them, and halos the record and what it highlights", () => {
  const lens = contextLens(AROUND, uuid(1), 2, [uuid(4)]);
  expect([...lens.lit!.keys()]).toEqual([uuid(1), uuid(2), uuid(3), uuid(4)]);
  expect(lens.lit!.has(uuid(5))).toBe(false);
  expect([...lens.strong]).toEqual([uuid(1), uuid(4)]);
  expect([...lens.labelled]).toEqual([uuid(1), uuid(4)]);
  // A highlight the read did not bring stays out of the drawing.
  expect([...contextLens(AROUND, uuid(1), 2, [uuid(99)]).strong]).toEqual([uuid(1)]);
});

test("its parameters fall back to two hops and no highlight, and keep only the ids a highlight names", () => {
  expect(contextParams({})).toEqual({ hops: 2, highlight: [] });
  expect(contextParams({ hops: 9, highlight: 3 })).toEqual({ hops: 2, highlight: [] });
  expect(contextParams({ hops: 1, highlight: ` ${uuid(4)},not-an-id,${uuid(5).toUpperCase()} ` })).toEqual({
    hops: 1,
    highlight: [uuid(4), uuid(5)],
  });
});

test("at three hops it reads three and lights them all", async () => {
  show(instance(uuid(1), { hops: 3 }));
  await waitFor(() => expect(renderer.shown()).toHaveLength(5));
  expect(graphReads().map((request) => request.query)).toEqual([`?focus=${uuid(1)}&hops=3&limit=${CONTEXT_LIMIT}`]);
  expect(renderer.settleFrames).toEqual([["Issue 1", "Issue 2", "Issue 3", "Issue 4", "Issue 5"]]);
});

test("a click on another node moves the page to it and re-centres the window there", async () => {
  const workspace = actions();
  show(instance(uuid(1), { hops: 2, highlight: uuid(4) }), workspace);
  await waitFor(() => expect(renderer.shown()).toHaveLength(5));
  act(() => renderer.events!.click(renderer.node("Issue 4")));
  expect(location.hash).toBe("#/ref/Issue/4");
  // The window keeps its hops and highlight: only its record changes.
  expect(workspace.operate).toHaveBeenCalledWith({ kind: "show", visual_type: "trax.subgraph", record_id: uuid(4) });
  // The record itself, or the background, moves nothing.
  act(() => renderer.events!.click(renderer.node("Issue 1")));
  act(() => renderer.events!.click(null));
  expect(workspace.operate).toHaveBeenCalledTimes(1);
});

test("outside a canvas a click still moves the page", async () => {
  show(instance(uuid(1)), null);
  await waitFor(() => expect(renderer.shown()).toHaveLength(5));
  act(() => renderer.events!.click(renderer.node("Issue 2")));
  expect(location.hash).toBe("#/ref/Issue/2");
});

test("a new record reads its own neighbourhood and frames it, keeping the nodes both share in place", async () => {
  const view = show(instance(uuid(1)));
  await waitFor(() => expect(renderer.shown()).toHaveLength(5));
  const kept = renderer.node("Issue 2");
  answer = () => Response.json({ nodes: [at(2, 0), at(1, 1), at(4, 1), at(6, 2)], edges: [edge(2, 1), edge(4, 2), edge(6, 4)] });
  view.show(instance(uuid(2)));
  await waitFor(() => expect(renderer.shown()).toEqual(["Issue 2", "Issue 1", "Issue 4", "Issue 6"]));
  expect(graphReads().map((request) => request.query)).toEqual([
    `?focus=${uuid(1)}&hops=3&limit=${CONTEXT_LIMIT}`,
    `?focus=${uuid(2)}&hops=3&limit=${CONTEXT_LIMIT}`,
  ]);
  expect(renderer.node("Issue 2")).toBe(kept);
  expect(renderer.settleFrames.at(-1)).toEqual(["Issue 2", "Issue 1", "Issue 4", "Issue 6"]);
  expect(screen.getByText("Issue#2")).toBeTruthy();
});

test("its hops buttons ask the canvas for the window at that reach, with its highlight kept", async () => {
  const workspace = actions();
  show(instance(uuid(1), { hops: 2, highlight: uuid(4) }), workspace);
  await waitFor(() => expect(renderer.shown()).toHaveLength(5));
  expect(screen.getByRole("button", { name: "2 hops" }).getAttribute("aria-pressed")).toBe("true");
  fireEvent.click(screen.getByRole("button", { name: "1 hop" }));
  expect(workspace.operate).toHaveBeenCalledWith({
    kind: "show",
    visual_type: "trax.subgraph",
    record_id: uuid(1),
    params: { hops: 1, highlight: uuid(4) },
  });
});

test("Open in graph leads to the graph page focused there", async () => {
  show(instance(uuid(1), { hops: 3 }));
  const link = await screen.findByRole("link", { name: "Open in graph" });
  expect(link.getAttribute("href")).toBe(`#/graph?focus=${uuid(1)}&hops=3`);
});

test("without a record it says to open one, and reads nothing", () => {
  show(instance(null));
  expect(screen.getByText("Open a record to see it in context.")).toBeTruthy();
  expect(sent).toHaveLength(0);
});

test("a failed read says so, and Retry reads again", async () => {
  answer = () => Response.json({ detail: "boom" }, { status: 500 });
  show(instance(uuid(1)));
  const retry = await screen.findByRole("button", { name: "Retry" });
  answer = () => Response.json(AROUND);
  fireEvent.click(retry);
  await waitFor(() => expect(renderer.shown()).toHaveLength(5));
});

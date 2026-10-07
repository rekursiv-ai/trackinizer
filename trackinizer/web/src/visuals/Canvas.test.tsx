import { focusManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { Profiler, useEffect } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { getVisualCatalog } from "../api/visuals";
import { findRef } from "../api/detail";
import {
  applyWorkspaceOperation,
  createDefaultWorkspace,
  getWorkspace,
  type WorkspaceState,
} from "../api/workspaces";
import { createWorkspacePreset, listWorkspacePresets, openWorkspacePreset } from "../api/presets";
import { createQueryClient } from "../app/queryClient";
import { newerWorkspace } from "../app/canvasStream";
import { Canvas } from "./Canvas";

vi.mock("../api/visuals", () => ({ getVisualCatalog: vi.fn() }));
vi.mock("../api/workspaces", () => ({
  createDefaultWorkspace: vi.fn(),
  getWorkspace: vi.fn(),
  applyWorkspaceOperation: vi.fn(),
  sendWorkspaceMessage: vi.fn(),
}));
vi.mock("../api/presets", () => ({
  createWorkspacePreset: vi.fn(),
  listWorkspacePresets: vi.fn(),
  openWorkspacePreset: vi.fn(),
}));
vi.mock("../api/detail", () => ({
  findRef: vi.fn(),
  getDetail: vi.fn().mockResolvedValue({
    self: { id: "record-id", kind: "Issue", seq: 42, title: "Context record" },
    edges: {}, backlinks: {}, changes: [],
  }),
}));

const workspace: WorkspaceState = {
  id: "c5286865-67b6-4bd8-ab51-e06e10c326c5",
  revision: 3,
  focused_instance: null,
  visuals: [{ id: "889ffcb2-cf44-43e7-9806-eb08428c6203", type: "trax.browse", version: 1,
    placement: "floating", record_id: null, params: {} }],
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.useRealTimers();
  focusManager.setFocused(undefined);
  window.innerWidth = 1024;
  history.replaceState(null, "", "#/");
  sessionStorage.clear();
});

test("saves the current canvas with workflow guidance and reopens it", async () => {
  const legacyStateKey = `trackinizer.v2.${location.origin}.ada@example.com`;
  const legacyState = JSON.stringify({
    stars: [],
    views: [{ id: "legacy-view", name: "Issue list", request: { kinds: ["Issue"], filters: [] } }],
    aliases: [], people: [], notification: { boundary: null, marks: [] }, ui: { collapsed: [], lens: null },
  });
  localStorage.setItem(legacyStateKey, legacyState);
  const saved = {
    id: "preset-id",
    name: "Issue triage",
    agent_instructions: "Summarize the open questions",
    continuation_record_id: "record-id",
    state: { visuals: workspace.visuals, focused_instance: null, agent_instructions: "Summarize the open questions", continuation_record_id: "record-id" },
    created_at: "2026-09-29T08:00:00Z",
    modified_at: "2026-09-29T08:00:00Z",
  };
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [{ type: "trax.browse", version: 1, title: "Browse", description: "Browse records",
      default_size: "wide", requires: [], parameter_schema: {} }],
  });
  vi.mocked(createDefaultWorkspace).mockResolvedValue(workspace);
  vi.mocked(getWorkspace).mockResolvedValue(workspace);
  vi.mocked(listWorkspacePresets).mockResolvedValue([saved]);
  vi.mocked(createWorkspacePreset).mockResolvedValue(saved);
  vi.mocked(openWorkspacePreset).mockResolvedValue({
    ...workspace,
    revision: 4,
    visuals: workspace.visuals.map((visual) => ({ ...visual,
      floating_rect: { left: 72, top: 64, width: 440, height: 320 } })),
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Canvas><div>Browse content</div></Canvas></QueryClientProvider>);

  fireEvent.click(await screen.findByRole("button", { name: "Configure" }));
  fireEvent.change(await screen.findByRole("textbox", { name: "Saved view name" }), { target: { value: "Issue triage" } });
  fireEvent.change(screen.getByRole("textbox", { name: "Agent instructions" }), { target: { value: "Summarize the open questions" } });
  fireEvent.change(screen.getByRole("textbox", { name: "Continuation record ID" }), { target: { value: "record-id" } });
  const stage = document.querySelector<HTMLElement>(".visual-stage");
  const tile = document.querySelector<HTMLElement>(`[data-visual-instance="${workspace.visuals[0]!.id}"]`);
  vi.spyOn(stage!, "getBoundingClientRect").mockReturnValue({ left: 100, top: 200 } as DOMRect);
  vi.spyOn(tile!, "getBoundingClientRect").mockReturnValue({
    left: 150, top: 250, width: 420, height: 300,
  } as DOMRect);
  fireEvent.click(screen.getByRole("button", { name: "Save canvas" }));

  await waitFor(() => expect(createWorkspacePreset).toHaveBeenCalledWith(
    workspace.id,
    workspace.revision,
    { name: "Issue triage", agentInstructions: "Summarize the open questions", continuationRecordId: "record-id",
      floatingRects: { [workspace.visuals[0]!.id]: { left: 50, top: 50, width: 420, height: 300 } } },
    expect.any(String),
  ));
  const openButton = await screen.findByRole("button", { name: "Open Issue triage" });
  fireEvent.click(openButton);
  await waitFor(() => expect(openWorkspacePreset).toHaveBeenCalledWith(
    "preset-id", workspace.id, workspace.revision, expect.any(String)));
  expect((await screen.findByRole("status")).textContent).toBe("Opened “Issue triage”.");
  const restoredTile = document.querySelector<HTMLElement>(`[data-visual-instance="${workspace.visuals[0]!.id}"]`);
  expect(restoredTile?.style.left).toBe("72px");
  expect(restoredTile?.style.width).toBe("440px");
  expect(localStorage.getItem(legacyStateKey)).toBe(legacyState);
  localStorage.removeItem(legacyStateKey);
});

test("shows save and open failures in the saved views panel", async () => {
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [{ type: "trax.browse", version: 1, title: "Browse", description: "Browse records",
      default_size: "wide", requires: [], parameter_schema: {} }],
  });
  vi.mocked(createDefaultWorkspace).mockResolvedValue(workspace);
  vi.mocked(getWorkspace).mockResolvedValue(workspace);
  vi.mocked(listWorkspacePresets).mockResolvedValue([{
    id: "preset-id", name: "Issue triage", agent_instructions: null, continuation_record_id: null,
    state: { visuals: workspace.visuals, focused_instance: null, agent_instructions: null, continuation_record_id: null },
    created_at: "2026-09-29T08:00:00Z", modified_at: "2026-09-29T08:00:00Z",
  }]);
  vi.mocked(createWorkspacePreset).mockRejectedValue(new Error("save unavailable"));
  vi.mocked(openWorkspacePreset).mockRejectedValue(new Error("open unavailable"));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Canvas><div>Browse content</div></Canvas></QueryClientProvider>);

  fireEvent.click(await screen.findByRole("button", { name: "Configure" }));
  fireEvent.change(await screen.findByRole("textbox", { name: "Saved view name" }), { target: { value: "Issue triage" } });
  fireEvent.click(screen.getByRole("button", { name: "Save canvas" }));
  expect((await screen.findByRole("alert")).textContent).toContain("Could not save this canvas.");
  fireEvent.click(screen.getByRole("button", { name: "Save canvas" }));
  await waitFor(() => expect(createWorkspacePreset).toHaveBeenCalledTimes(2), { interval: 1 });
  expect(vi.mocked(createWorkspacePreset).mock.calls[1]?.[3]).toBe(vi.mocked(createWorkspacePreset).mock.calls[0]?.[3]);
  fireEvent.click(screen.getByRole("button", { name: "Open Issue triage" }));
  expect((await screen.findByRole("alert")).textContent).toContain("Could not open this saved view.");
  fireEvent.click(screen.getByRole("button", { name: "Open Issue triage" }));
  await waitFor(() => expect(openWorkspacePreset).toHaveBeenCalledTimes(2), { interval: 1 });
  expect(vi.mocked(openWorkspacePreset).mock.calls[1]?.slice(1)).toEqual(
    vi.mocked(openWorkspacePreset).mock.calls[0]?.slice(1));
});

test("untouched workflow guidance follows a newer canvas in another browser", async () => {
  const initial: WorkspaceState = {
    ...workspace, agent_instructions: "Original guidance", continuation_record_id: "original-record",
  };
  const changed: WorkspaceState = {
    ...initial, revision: 4, agent_instructions: "Restored guidance", continuation_record_id: "restored-record",
  };
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [{ type: "trax.browse", version: 1, title: "Browse", description: "Browse records",
      default_size: "wide", requires: [], parameter_schema: {} }],
  });
  vi.mocked(createDefaultWorkspace).mockResolvedValue(initial);
  vi.mocked(getWorkspace).mockResolvedValue(initial);
  vi.mocked(listWorkspacePresets).mockResolvedValue([]);
  vi.mocked(createWorkspacePreset).mockResolvedValue({
    id: "saved-id", name: "Continued", agent_instructions: "Restored guidance",
    continuation_record_id: "restored-record",
    state: { visuals: changed.visuals, focused_instance: null, agent_instructions: "Restored guidance",
      continuation_record_id: "restored-record" },
    created_at: "2026-09-29T08:00:00Z", modified_at: "2026-09-29T08:00:00Z",
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Canvas><div>Browse content</div></Canvas></QueryClientProvider>);
  fireEvent.click(await screen.findByRole("button", { name: "Configure" }));
  expect((screen.getByRole("textbox", { name: "Agent instructions" }) as HTMLTextAreaElement).value)
    .toBe("Original guidance");

  await act(async () => { client.setQueryData(["workspace", workspace.id], changed); });
  await waitFor(() => expect((screen.getByRole("textbox", { name: "Agent instructions" }) as HTMLTextAreaElement).value)
    .toBe("Restored guidance"));
  expect((screen.getByRole("textbox", { name: "Continuation record ID" }) as HTMLInputElement).value)
    .toBe("restored-record");
  fireEvent.change(screen.getByRole("textbox", { name: "Saved view name" }), { target: { value: "Continued" } });
  fireEvent.click(screen.getByRole("button", { name: "Save canvas" }));
  // A mock's call changes nothing on screen, so only the interval checks again.
  await waitFor(() => expect(createWorkspacePreset).toHaveBeenCalledWith(
    workspace.id, 4,
    expect.objectContaining({ agentInstructions: "Restored guidance", continuationRecordId: "restored-record" }),
    expect.any(String),
  ), { interval: 1 });
  await waitFor(() => expect((screen.getByRole("button", { name: "Save canvas" }) as HTMLButtonElement).disabled)
    .toBe(true));
  fireEvent.change(screen.getByRole("textbox", { name: "Agent instructions" }),
    { target: { value: "My local guidance" } });
  fireEvent.change(screen.getByRole("textbox", { name: "Continuation record ID" }),
    { target: { value: "my-local-record" } });
  fireEvent.change(screen.getByRole("textbox", { name: "Saved view name" }),
    { target: { value: "Local pass" } });
  fireEvent.click(screen.getByRole("button", { name: "Save canvas" }));
  await waitFor(() => expect(createWorkspacePreset).toHaveBeenCalledTimes(2), { interval: 1 });
  await waitFor(() => expect((screen.getByRole("button", { name: "Save canvas" }) as HTMLButtonElement).disabled)
    .toBe(true));
  await act(async () => { client.setQueryData(["workspace", workspace.id], {
    ...changed, revision: 5, agent_instructions: "Newest guidance", continuation_record_id: "newest-record",
  }); });
  await waitFor(() => expect((screen.getByRole("textbox", { name: "Agent instructions" }) as HTMLTextAreaElement).value)
    .toBe("Newest guidance"));
  expect((screen.getByRole("textbox", { name: "Continuation record ID" }) as HTMLInputElement).value)
    .toBe("newest-record");
});

/** The Browse visual alone, as the server's catalog describes it. */
function browseOnly() {
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [{ type: "trax.browse", version: 1, title: "Browse", description: "Browse records",
      default_size: "wide", requires: [], parameter_schema: {} }],
  });
}

test("the browse pane keeps its content mounted while the server's canvas arrives", async () => {
  browseOnly();
  let open: (state: WorkspaceState) => void = () => { throw new Error("No canvas pending"); };
  vi.mocked(createDefaultWorkspace).mockImplementation(() => new Promise((resolve) => { open = resolve; }));
  vi.mocked(getWorkspace).mockResolvedValue(workspace);
  // The page stands where a canvas puts it, in the strip of main visuals, before and after the server's.
  const inStrip: WorkspaceState = { ...workspace, visuals: [{ ...workspace.visuals[0]!, placement: "main" }] };
  // The catalog is slow too: the page is in the strip before it arrives.
  vi.mocked(getVisualCatalog).mockImplementation(() => new Promise(() => {}));
  const mounted = vi.fn();
  function Content() {
    useEffect(() => mounted(), []);
    return <div>Browse content</div>;
  }
  render(<QueryClientProvider client={createQueryClient(() => {})}><Canvas><Content /></Canvas></QueryClientProvider>);
  expect(await screen.findByText("Browse content")).toBeTruthy();
  await act(async () => open(inStrip));
  await waitFor(() => expect(document.querySelector(`[data-visual-instance="${inStrip.visuals[0]!.id}"]`)).not.toBeNull(), { interval: 1 });
  expect(mounted).toHaveBeenCalledTimes(1);
});

test("the canvas does not read at mount, nor on a timer, nor when its tab comes back: the shell's stream carries it", async () => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  browseOnly();
  vi.mocked(createDefaultWorkspace).mockResolvedValue(workspace);
  vi.mocked(getWorkspace).mockResolvedValue(workspace);
  render(<QueryClientProvider client={createQueryClient(() => {})}><Canvas><div>Browse content</div></Canvas></QueryClientProvider>);
  await screen.findByRole("button", { name: "Configure" });
  await waitFor(() => expect(document.querySelector("[data-visual-instance]")).not.toBeNull(), { interval: 1 });
  await act(async () => { vi.advanceTimersByTime(10_000); });
  act(() => {
    focusManager.setFocused(false);
    focusManager.setFocused(true);
  });
  expect(getWorkspace).not.toHaveBeenCalled();
});

test("the page cannot be dismissed, and Configure cannot hide it", async () => {
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [
      { type: "trax.browse", version: 1, title: "Browse", description: "Browse records", default_size: "wide", requires: [], parameter_schema: {} },
      { type: "trax.chat", version: 1, title: "Chat", description: "Chat", default_size: "compact", requires: [], parameter_schema: {} },
    ],
  });
  const withChat: WorkspaceState = { ...workspace, visuals: [{ ...workspace.visuals[0]!, placement: "main" },
    { id: "chat-instance", type: "trax.chat", version: 1, placement: "side", record_id: null, params: {} }] };
  vi.mocked(createDefaultWorkspace).mockResolvedValue(withChat);
  vi.mocked(getWorkspace).mockResolvedValue(withChat);
  render(<QueryClientProvider client={createQueryClient(() => {})}><Canvas><div>Browse content</div></Canvas></QueryClientProvider>);
  await screen.findAllByTitle("Dismiss visual");
  const tiles = [...document.querySelectorAll<HTMLElement>(".visual-tile")];
  expect(tiles).toHaveLength(2);
  const dismissals = tiles.map((tile) => within(tile).queryAllByTitle("Dismiss visual").length);
  expect(dismissals).toEqual([0, 1]);
  fireEvent.click(screen.getByRole("button", { name: "Configure" }));
  expect(screen.getByRole("checkbox", { name: /Browse/ })).toHaveProperty("disabled", true);
  expect(screen.getByRole("checkbox", { name: /Chat/ })).toHaveProperty("disabled", false);
});

test("a view that crashes inside the canvas gets the crash screen with Copy details and Reload, and clears when the page moves", async () => {
  browseOnly();
  vi.spyOn(console, "error").mockImplementation(() => {});
  vi.mocked(createDefaultWorkspace).mockResolvedValue(workspace);
  vi.mocked(getWorkspace).mockResolvedValue(workspace);
  function Broken(): never {
    throw new Error("the view broke");
  }
  const client = createQueryClient(() => {});
  const view = render(<QueryClientProvider client={client}><Canvas><Broken /></Canvas></QueryClientProvider>);
  expect((await screen.findByRole("alert")).textContent).toBe("the view broke");
  expect(screen.getByRole("button", { name: "Reload" })).toBeTruthy();
  expect(screen.getByRole("button", { name: /Copy details/ })).toBeTruthy();
  // The toolbar, outside the page, is still there.
  expect(screen.getByRole("toolbar", { name: "Canvas controls" })).toBeTruthy();
  history.replaceState(null, "", "#/list/Issue");
  view.rerender(<QueryClientProvider client={client}><Canvas><div>Moved on</div></Canvas></QueryClientProvider>);
  expect(await screen.findByText("Moved on")).toBeTruthy();
  vi.restoreAllMocks();
});

test("Configure opens its panel and Done collapses it; open, it stays open for the tab", async () => {
  browseOnly();
  vi.mocked(createDefaultWorkspace).mockResolvedValue(workspace);
  vi.mocked(getWorkspace).mockResolvedValue(workspace);
  const show = () => render(<QueryClientProvider client={createQueryClient(() => {})}><Canvas><div>Browse content</div></Canvas></QueryClientProvider>);
  const configure = () => screen.getByRole("button", { name: "Configure" });
  const panel = () => screen.queryByRole("complementary", { name: "Configure visuals" });
  show();
  expect(configure().getAttribute("aria-expanded")).toBe("false");
  expect(panel()).toBeNull();
  fireEvent.click(configure());
  expect(configure().getAttribute("aria-controls")).toBe(panel()!.id);
  cleanup();
  show();
  expect(configure().getAttribute("aria-expanded")).toBe("true");
  fireEvent.click(within(panel()!).getByRole("button", { name: "Done" }));
  expect(panel()).toBeNull();
  expect(configure().getAttribute("aria-expanded")).toBe("false");
});

test("older reads and replay receipts cannot replace a newer canvas revision", () => {
  const older = { ...workspace, revision: 2 };
  expect(newerWorkspace(workspace, older)).toBe(workspace);
  expect(newerWorkspace(older, workspace)).toBe(workspace);
  expect(newerWorkspace(undefined, workspace)).toBe(workspace);
});

test("a single floating visual keeps its placement control", async () => {
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [{ type: "trax.browse", version: 1, title: "Browse", description: "Browse records",
      default_size: "wide", requires: [], parameter_schema: {} }],
  });
  vi.mocked(createDefaultWorkspace).mockResolvedValue(workspace);
  vi.mocked(getWorkspace).mockResolvedValue(workspace);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Canvas><div>Browse content</div></Canvas></QueryClientProvider>);
  expect(await screen.findByText("Browse content")).toBeTruthy();
  expect(await screen.findByRole("combobox", { name: "Place trax.browse" })).toBeTruthy();
  const tab = screen.getByRole("button", { name: "Expand" });
  expect(tab.getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(tab);
  expect(screen.getByRole("button", { name: "Collapse" }).getAttribute("aria-expanded")).toBe("true");

  const dragHandle = screen.getByRole("button", { name: "Move Browse" });
  const tile = document.querySelector<HTMLElement>(`[data-visual-instance="${workspace.visuals[0]!.id}"]`);
  const stage = document.querySelector<HTMLElement>(".visual-stage");
  Object.defineProperty(stage, "clientWidth", { configurable: true, value: 800 });
  Object.defineProperty(stage, "clientHeight", { configurable: true, value: 600 });
  Object.defineProperty(tile, "offsetWidth", { configurable: true, value: 300 });
  Object.defineProperty(tile, "offsetHeight", { configurable: true, value: 400 });
  fireEvent.keyDown(dragHandle, { key: "ArrowRight" });
  expect(tile?.style.left).toBe("16px");
});

test("a drag moves a floating visual without rendering the canvas, and it keeps its place on release", async () => {
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [{ type: "trax.browse", version: 1, title: "Browse", description: "Browse records",
      default_size: "wide", requires: [], parameter_schema: {} }],
  });
  vi.mocked(createDefaultWorkspace).mockResolvedValue(workspace);
  vi.mocked(getWorkspace).mockResolvedValue(workspace);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  let commits = 0;
  render(<QueryClientProvider client={client}><Profiler id="canvas" onRender={() => { commits += 1; }}>
    <Canvas><div>Browse content</div></Canvas>
  </Profiler></QueryClientProvider>);
  const dragHandle = await screen.findByRole("button", { name: "Move Browse" });
  // jsdom has no pointer capture.
  dragHandle.setPointerCapture = vi.fn();
  const tile = document.querySelector<HTMLElement>(`[data-visual-instance="${workspace.visuals[0]!.id}"]`)!;
  const stage = document.querySelector<HTMLElement>(".visual-stage")!;
  Object.defineProperty(stage, "clientWidth", { configurable: true, value: 800 });
  Object.defineProperty(stage, "clientHeight", { configurable: true, value: 600 });
  Object.defineProperty(tile, "offsetWidth", { configurable: true, value: 300 });
  Object.defineProperty(tile, "offsetHeight", { configurable: true, value: 400 });

  fireEvent.pointerDown(dragHandle, { pointerId: 1, clientX: 100, clientY: 100 });
  const before = commits;
  fireEvent.pointerMove(dragHandle, { pointerId: 1, clientX: 140, clientY: 120 });
  fireEvent.pointerMove(dragHandle, { pointerId: 1, clientX: 180, clientY: 130 });
  // Each move is a transform on the tile: nothing renders, nothing lays out again.
  expect(commits).toBe(before);
  expect(tile.style.transform).toBe("translate(80px, 30px)");
  // Past the stage's edge it stops at the edge.
  fireEvent.pointerMove(dragHandle, { pointerId: 1, clientX: 900, clientY: 900 });
  expect(tile.style.transform).toBe("translate(500px, 200px)");
  fireEvent.pointerMove(dragHandle, { pointerId: 1, clientX: 180, clientY: 130 });
  fireEvent.pointerUp(dragHandle, { pointerId: 1, clientX: 180, clientY: 130 });
  expect(tile.style.transform).toBe("");
  expect([tile.style.left, tile.style.top]).toEqual(["80px", "30px"]);
});

test("Configure resolves the current ref route before showing a record-required visual", async () => {
  const recordId = "61d3a095-c7f1-4d27-a4c4-a5b1c218a31e";
  history.replaceState(null, "", "#/ref/AgentSession/20379");
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [
      { type: "trax.browse", version: 1, title: "Browse", description: "Browse records",
        default_size: "wide", requires: [], parameter_schema: {} },
      { type: "trax.subgraph", version: 1, title: "Graph", description: "Show context graph",
        default_size: "compact", requires: ["record"], parameter_schema: {} },
    ],
  });
  vi.mocked(findRef).mockResolvedValue(recordId);
  vi.mocked(createDefaultWorkspace).mockResolvedValue(workspace);
  vi.mocked(getWorkspace).mockResolvedValue(workspace);
  const changed = { ...workspace, revision: 4, visuals: [...workspace.visuals, {
    id: "graph-instance", type: "trax.subgraph", version: 1,
    placement: "side" as const, record_id: recordId, params: {},
  }] };
  vi.mocked(applyWorkspaceOperation).mockResolvedValue(changed);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Canvas><div>Record detail</div></Canvas></QueryClientProvider>);

  fireEvent.click(await screen.findByRole("button", { name: "Configure" }));
  const graphToggle = await screen.findByRole("checkbox", { name: /Graph/ });
  await waitFor(() => expect(graphToggle).toHaveProperty("disabled", false), { interval: 1 });
  fireEvent.click(graphToggle);

  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledWith(
    workspace.id,
    workspace.revision,
    { kind: "show", visual_type: "trax.subgraph", record_id: recordId },
    expect.any(String),
  ));
});

test("a delayed read cannot undo an operation's newer canvas revision", async () => {
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [
      { type: "trax.browse", version: 1, title: "Browse", description: "Browse records",
        default_size: "wide", requires: [], parameter_schema: {} },
      { type: "trax.chat", version: 1, title: "Chat", description: "Chat about the page",
        default_size: "compact", requires: [], parameter_schema: {} },
    ],
  });
  const initial = { ...workspace, revision: 1, visuals: [{ ...workspace.visuals[0]!, placement: "main" as const }] };
  const changed = { ...initial, revision: 2, visuals: [...initial.visuals, {
    id: "440b0d37-76f3-478c-acb5-20a40cecc10d", type: "trax.chat", version: 1,
    placement: "side" as const, record_id: null, params: {},
  }] };
  vi.mocked(createDefaultWorkspace).mockResolvedValue(initial);
  let releaseRead: (value: WorkspaceState) => void = () => { throw new Error("Read not pending"); };
  vi.mocked(getWorkspace).mockImplementation(() => new Promise((resolve) => { releaseRead = resolve; }));
  // The canvas reads only after a refused write; that read is the delayed one.
  vi.mocked(applyWorkspaceOperation).mockRejectedValueOnce(new Error("refused")).mockResolvedValue(changed);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Canvas><div>Browse content</div></Canvas></QueryClientProvider>);
  fireEvent.click(await screen.findByRole("button", { name: "Configure" }));
  fireEvent.click(await screen.findByRole("checkbox", { name: /Chat/ }));
  await waitFor(() => expect(getWorkspace).toHaveBeenCalledTimes(1), { interval: 1 });
  fireEvent.click(await screen.findByRole("checkbox", { name: /Chat/ }));
  await waitFor(() => expect(client.getQueryData<WorkspaceState>(["workspace", workspace.id])?.revision).toBe(2), { interval: 1 });
  await act(async () => { releaseRead(initial); });
  expect(client.getQueryData<WorkspaceState>(["workspace", workspace.id])?.revision).toBe(2);
  expect((screen.getByRole("checkbox", { name: /Chat/ }) as HTMLInputElement).checked).toBe(true);
});

test("Chat about this shows a floating Chat pane with the record context", async () => {
  const recordId = "61d3a095-c7f1-4d27-a4c4-a5b1c218a31e";
  history.replaceState(null, "", `#/lookup/${recordId}`);
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [
      { type: "trax.browse", version: 1, title: "Browse", description: "Browse records",
        default_size: "wide", requires: [], parameter_schema: {} },
      { type: "trax.chat", version: 1, title: "Chat", description: "Chat about the page",
        default_size: "compact", requires: [], parameter_schema: {} },
    ],
  });
  vi.mocked(createDefaultWorkspace).mockResolvedValue(workspace);
  vi.mocked(getWorkspace).mockResolvedValue(workspace);
  const changed: WorkspaceState = {
    ...workspace,
    revision: 4,
    focused_instance: "chat-instance",
    visuals: [...workspace.visuals, {
      id: "chat-instance", type: "trax.chat", version: 1, placement: "floating",
      record_id: recordId, params: {},
    }],
  };
  vi.mocked(applyWorkspaceOperation).mockResolvedValue(changed);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Canvas><div>Record detail</div></Canvas></QueryClientProvider>);

  const controls = screen.getByRole("toolbar", { name: "Canvas controls" });
  expect(within(controls).getByRole("button", { name: "Configure" })).toBeTruthy();
  const chatButton = within(controls).getByRole("button", { name: "Chat about this" });
  expect(screen.queryByRole("button", { name: "Show context graph" })).toBeNull();
  await waitFor(() => expect((chatButton as HTMLButtonElement).disabled).toBe(false), { interval: 1 });
  fireEvent.click(chatButton);

  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledWith(
    workspace.id,
    workspace.revision,
    { kind: "show", visual_type: "trax.chat", placement: "floating", record_id: recordId },
    expect.any(String),
  ));
  await waitFor(() => expect(client.getQueryData<WorkspaceState>(["workspace", workspace.id])?.focused_instance)
    .toBe("chat-instance"));
});

test("Chat about this opens the floating Chat body at a narrow viewport", async () => {
  window.innerWidth = 800;
  const recordId = "61d3a095-c7f1-4d27-a4c4-a5b1c218a31e";
  history.replaceState(null, "", `#/lookup/${recordId}`);
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [
      { type: "trax.browse", version: 1, title: "Browse", description: "Browse records",
        default_size: "wide", requires: [], parameter_schema: {} },
      { type: "trax.chat", version: 1, title: "Chat", description: "Chat about the page",
        default_size: "compact", requires: [], parameter_schema: {} },
    ],
  });
  vi.mocked(createDefaultWorkspace).mockResolvedValue(workspace);
  vi.mocked(getWorkspace).mockResolvedValue(workspace);
  const changed: WorkspaceState = {
    ...workspace,
    revision: 4,
    focused_instance: "chat-instance",
    visuals: [...workspace.visuals, {
      id: "chat-instance", type: "trax.chat", version: 1, placement: "floating",
      record_id: recordId, params: {},
    }],
  };
  vi.mocked(applyWorkspaceOperation).mockResolvedValue(changed);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Canvas><div>Record detail</div></Canvas></QueryClientProvider>);

  const chatButton = screen.getByRole("button", { name: "Chat about this" });
  await waitFor(() => expect((chatButton as HTMLButtonElement).disabled).toBe(false), { interval: 1 });
  fireEvent.click(chatButton);

  expect(await screen.findByRole("button", { name: "Collapse" })).toBeTruthy();
  expect(document.querySelector(".visual-tile-mobile-expanded")).toBeTruthy();
});

test("a link to a record in Chat only moves the page: it writes nothing to the canvas", async () => {
  const chat: WorkspaceState["visuals"][number] = {
    id: "chat-instance", type: "trax.chat", version: 1, placement: "main",
    record_id: "record-id", params: {},
  };
  const initial = { ...workspace, visuals: [chat] };
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [
      { type: "trax.browse", version: 1, title: "Browse", description: "Browse records",
        default_size: "wide", requires: [], parameter_schema: {} },
      { type: "trax.chat", version: 1, title: "Chat", description: "Chat about the page",
        default_size: "compact", requires: [], parameter_schema: {} },
    ],
  });
  vi.mocked(createDefaultWorkspace).mockResolvedValue(initial);
  vi.mocked(getWorkspace).mockResolvedValue(initial);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Canvas><div>Browse record view</div></Canvas></QueryClientProvider>);

  const link = await screen.findByRole("link", { name: "Issue#42 Context record" });
  expect(link.getAttribute("href")).toBe("#/lookup/record-id");
  fireEvent.click(link);
  await waitFor(() => expect(location.hash).toBe("#/lookup/record-id"), { interval: 1 });
  expect(applyWorkspaceOperation).not.toHaveBeenCalled();
});

test("the record Chat is about comes from the address alone, never from a visual", async () => {
  history.replaceState(null, "", "#/list/Issue");
  browseOnly();
  const withRecord: WorkspaceState = { ...workspace, visuals: [{ ...workspace.visuals[0]!, record_id: "stale-record" }] };
  vi.mocked(createDefaultWorkspace).mockResolvedValue(withRecord);
  vi.mocked(getWorkspace).mockResolvedValue(withRecord);
  render(<QueryClientProvider client={createQueryClient(() => {})}><Canvas><div>List</div></Canvas></QueryClientProvider>);
  await screen.findByRole("button", { name: "Configure" });
  await waitFor(() => expect(document.querySelector("[data-visual-instance]")).not.toBeNull(), { interval: 1 });
  expect((screen.getByRole("button", { name: "Chat about this" }) as HTMLButtonElement).disabled).toBe(true);
});

test("a canvas write waits for a pending one and uses its new revision", async () => {
  const recordId = "61d3a095-c7f1-4d27-a4c4-a5b1c218a31e";
  history.replaceState(null, "", `#/lookup/${recordId}`);
  const chat: WorkspaceState["visuals"][number] = {
    id: "chat-instance", type: "trax.chat", version: 1, placement: "side",
    record_id: null, params: {},
  };
  const initial = { ...workspace, visuals: [...workspace.visuals, chat] };
  const afterOperation: WorkspaceState = { ...initial, revision: 4 };
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [
      { type: "trax.browse", version: 1, title: "Browse", description: "Browse records",
        default_size: "wide", requires: [], parameter_schema: {} },
      { type: "trax.chat", version: 1, title: "Chat", description: "Chat about the page",
        default_size: "compact", requires: [], parameter_schema: {} },
    ],
  });
  vi.mocked(createDefaultWorkspace).mockResolvedValue(initial);
  vi.mocked(getWorkspace).mockResolvedValue(initial);
  let finishOperation: (state: WorkspaceState) => void = () => { throw new Error("Write not pending"); };
  vi.mocked(applyWorkspaceOperation).mockImplementationOnce(() => new Promise((resolve) => { finishOperation = resolve; }))
    .mockResolvedValue({ ...afterOperation, revision: 5 });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Canvas><div>Record detail</div></Canvas></QueryClientProvider>);

  const chatButton = screen.getByRole("button", { name: "Chat about this" });
  await waitFor(() => expect((chatButton as HTMLButtonElement).disabled).toBe(false), { interval: 1 });
  fireEvent.click(chatButton);
  // A mock's call changes nothing on screen, so only the interval checks again.
  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledOnce(), { interval: 1 });
  const focusChat = screen.getByRole("button", { name: "Chat" });
  expect((focusChat as HTMLButtonElement).disabled).toBe(true);

  await act(async () => { finishOperation(afterOperation); });
  await waitFor(() => expect((focusChat as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(focusChat);
  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenLastCalledWith(
    initial.id, 4, { kind: "focus", instance_id: "chat-instance" }, expect.any(String)), { interval: 1 });
  expect(client.getQueryData<WorkspaceState>(["workspace", initial.id])?.revision).toBe(5);
});

test("a rejected canvas write reports how to recover", async () => {
  const recordId = "61d3a095-c7f1-4d27-a4c4-a5b1c218a31e";
  history.replaceState(null, "", `#/lookup/${recordId}`);
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [{ type: "trax.browse", version: 1, title: "Browse", description: "Browse records",
      default_size: "wide", requires: [], parameter_schema: {} }],
  });
  vi.mocked(createDefaultWorkspace).mockResolvedValue(workspace);
  vi.mocked(getWorkspace).mockResolvedValue(workspace);
  vi.mocked(applyWorkspaceOperation).mockRejectedValue(new Error("revision conflict"));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Canvas><div>Record detail</div></Canvas></QueryClientProvider>);

  const chatButton = screen.getByRole("button", { name: "Chat about this" });
  await waitFor(() => expect((chatButton as HTMLButtonElement).disabled).toBe(false), { interval: 1 });
  fireEvent.click(chatButton);

  expect((await screen.findByRole("alert")).textContent).toContain("Its revision may have changed; try again.");
});

test("Configure opens Artifact content from its canonical link", async () => {
  const artifactId = "0b6f7c1e-2f7a-4c55-9d7e-1f0e6b1d2a33";
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [
      { type: "trax.browse", version: 1, title: "Browse", description: "Browse records",
        default_size: "wide", requires: [], parameter_schema: {} },
      { type: "trax.artifact", version: 1, title: "Artifact", description: "Shared content",
        default_size: "wide", requires: [], parameter_schema: {} },
    ],
  });
  vi.mocked(createDefaultWorkspace).mockResolvedValue(workspace);
  vi.mocked(getWorkspace).mockResolvedValue(workspace);
  vi.mocked(applyWorkspaceOperation).mockResolvedValue({ ...workspace, revision: 4 });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Canvas><div>Browse content</div></Canvas></QueryClientProvider>);

  fireEvent.click(await screen.findByRole("button", { name: "Configure" }));
  expect(screen.getByRole("textbox", { name: "Artifact link" })).toBeTruthy();
  fireEvent.change(screen.getByRole("textbox", { name: "Artifact link" }),
    { target: { value: `https://trackinizer.example.com/app/#/lookup/${artifactId}` } });
  fireEvent.click(screen.getByRole("button", { name: "Open Artifact" }));
  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledWith(
    workspace.id, workspace.revision,
    { kind: "show", visual_type: "trax.artifact", placement: "main", record_id: artifactId },
    expect.any(String),
  ));
});

test("a shared Artifact route can open Chat about its exact revision", async () => {
  const artifactId = "0b6f7c1e-2f7a-4c55-9d7e-1f0e6b1d2a33";
  history.replaceState(null, "", `#/lookup/${artifactId}`);
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [
      { type: "trax.browse", version: 1, title: "Browse", description: "Browse records",
        default_size: "wide", requires: [], parameter_schema: {} },
      { type: "trax.chat", version: 1, title: "Chat", description: "Chat about the page",
        default_size: "compact", requires: [], parameter_schema: {} },
    ],
  });
  vi.mocked(createDefaultWorkspace).mockResolvedValue(workspace);
  vi.mocked(getWorkspace).mockResolvedValue(workspace);
  vi.mocked(applyWorkspaceOperation).mockResolvedValue({ ...workspace, revision: 4 });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Canvas><div>Artifact page</div></Canvas></QueryClientProvider>);

  const button = await screen.findByRole("button", { name: "Chat about this" });
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false), { interval: 1 });
  fireEvent.click(button);
  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledWith(
    workspace.id, workspace.revision,
    { kind: "show", visual_type: "trax.chat", placement: "floating", record_id: artifactId },
    expect.any(String),
  ));
});

test("Chat targets the focused Artifact visual when another record is in the URL", async () => {
  const artifactId = "0b6f7c1e-2f7a-4c55-9d7e-1f0e6b1d2a33";
  const recordId = "61d3a095-c7f1-4d27-a4c4-a5b1c218a31e";
  history.replaceState(null, "", `#/lookup/${recordId}`);
  const artifactVisual: WorkspaceState["visuals"][number] = {
    id: "artifact-pane", type: "trax.artifact", version: 1, placement: "main", record_id: artifactId,
    params: {},
  };
  const state = { ...workspace, focused_instance: artifactVisual.id,
    visuals: [...workspace.visuals, artifactVisual] };
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [
      { type: "trax.browse", version: 1, title: "Browse", description: "Browse records",
        default_size: "wide", requires: [], parameter_schema: {} },
      { type: "trax.artifact", version: 1, title: "Artifact", description: "Shared content",
        default_size: "wide", requires: [], parameter_schema: {} },
      { type: "trax.chat", version: 1, title: "Chat", description: "Chat about the page",
        default_size: "compact", requires: [], parameter_schema: {} },
    ],
  });
  vi.mocked(createDefaultWorkspace).mockResolvedValue(state);
  vi.mocked(getWorkspace).mockResolvedValue(state);
  vi.mocked(applyWorkspaceOperation).mockResolvedValue({ ...state, revision: 4 });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Canvas><div>Record page</div></Canvas></QueryClientProvider>);

  const button = await screen.findByRole("button", { name: "Chat about this" });
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false), { interval: 1 });
  fireEvent.click(button);
  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledWith(
    state.id, state.revision,
    { kind: "show", visual_type: "trax.chat", placement: "floating", record_id: artifactId },
    expect.any(String),
  ));
});

test("the Chat button shows Chat floating, and focuses it once shown", async () => {
  browseOnly();
  const chat = { id: "chat-instance", type: "trax.chat", version: 1, placement: "floating" as const, record_id: null, params: {} };
  const withChat: WorkspaceState = { ...workspace, revision: 4, visuals: [...workspace.visuals, chat] };
  vi.mocked(createDefaultWorkspace).mockResolvedValue(workspace);
  vi.mocked(getWorkspace).mockResolvedValue(workspace);
  vi.mocked(applyWorkspaceOperation).mockResolvedValue(withChat);
  const client = createQueryClient(() => {});
  render(<QueryClientProvider client={client}><Canvas><div>Browse content</div></Canvas></QueryClientProvider>);
  const button = () => screen.getByRole("button", { name: "Chat" }) as HTMLButtonElement;
  await waitFor(() => expect(button().disabled).toBe(false), { interval: 1 });
  fireEvent.click(button());
  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledWith(
    workspace.id, 3, { kind: "show", visual_type: "trax.chat", placement: "floating" }, expect.any(String)));
  await waitFor(() => expect(client.getQueryData<WorkspaceState>(["workspace", workspace.id])?.revision).toBe(4), { interval: 1 });
  await waitFor(() => expect(button().disabled).toBe(false), { interval: 1 });
  fireEvent.click(button());
  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenLastCalledWith(
    workspace.id, 4, { kind: "focus", instance_id: "chat-instance" }, expect.any(String)));
});

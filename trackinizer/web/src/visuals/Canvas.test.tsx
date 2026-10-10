import { focusManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render as renderBare, screen, waitFor, within } from "@testing-library/react";
import { Profiler, useEffect, type ReactElement, type ReactNode } from "react";
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
import { ProfileContext } from "../app/boot";
import { PROFILE } from "../detail/testing";
import { createQueryClient } from "../app/queryClient";
import { newerWorkspace } from "../app/canvasStream";
import { storageKey } from "../state/store";
import { EMPTY_STATE, parseState } from "../state/value";
import { Canvas } from "./Canvas";
import { ChatFeed, ChatFeedContext } from "./chatFeed";
import { CLICK_SLOP, rememberedTile, withTile } from "./floatingTile";
import { FOLD_AFTER_MS, OPEN_AFTER_MS } from "./hoverOpen";

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

/** The canvas shows the signed-in user's state, so every render has a profile above it. */
function render(ui: ReactElement) {
  return renderBare(ui, { wrapper: ({ children }: { children: ReactNode }) => <ProfileContext value={PROFILE}>{children}</ProfileContext> });
}

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
  localStorage.clear();
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

test("on a record's page, Chat docks beside the page with the record context", async () => {
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
      id: "chat-instance", type: "trax.chat", version: 1, placement: "side",
      record_id: recordId, params: {},
    }],
  };
  vi.mocked(applyWorkspaceOperation).mockResolvedValue(changed);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Canvas><div>Record detail</div></Canvas></QueryClientProvider>);

  const controls = screen.getByRole("toolbar", { name: "Canvas controls" });
  expect(within(controls).getByRole("button", { name: "Configure" })).toBeTruthy();
  const chatButton = within(controls).getByRole("button", { name: "Chat" });
  expect(screen.queryByRole("button", { name: "Show context graph" })).toBeNull();
  await waitFor(() => expect((chatButton as HTMLButtonElement).disabled).toBe(false), { interval: 1 });
  fireEvent.click(chatButton);

  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledWith(
    workspace.id,
    workspace.revision,
    { kind: "show", visual_type: "trax.chat", record_id: recordId },
    expect.any(String),
  ));
  await waitFor(() => expect(client.getQueryData<WorkspaceState>(["workspace", workspace.id])?.focused_instance)
    .toBe("chat-instance"));
  await waitFor(() => expect(document.querySelector(`.visual-side-column > ${CHAT_TILE}`)).not.toBeNull(), { interval: 1 });
});

const CHAT_TILE = '[data-visual-instance="chat-instance"]';

/** The page with Chat beside it, stored at `placement`, under a feed the test holds. */
async function mountChat(placement: "side" | "floating" = "side") {
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [
      { type: "trax.browse", version: 1, title: "Browse", description: "Browse records",
        default_size: "wide", requires: [], parameter_schema: {} },
      { type: "trax.chat", version: 1, title: "Chat", description: "Chat about the page",
        default_size: "compact", requires: [], parameter_schema: {} },
    ],
  });
  const state: WorkspaceState = { ...workspace, visuals: [
    { ...workspace.visuals[0]!, placement: "main" },
    { id: "chat-instance", type: "trax.chat", version: 1, placement, record_id: null, params: {} }] };
  vi.mocked(createDefaultWorkspace).mockResolvedValue(state);
  vi.mocked(getWorkspace).mockResolvedValue(state);
  vi.mocked(applyWorkspaceOperation).mockResolvedValue({ ...state, revision: 4, focused_instance: "chat-instance" });
  const feed = new ChatFeed();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><ChatFeedContext value={feed}>
    <Canvas><div>Browse content</div></Canvas></ChatFeedContext></QueryClientProvider>);
  await waitFor(() => expect(document.querySelector(CHAT_TILE)).not.toBeNull(), { interval: 1 });
  return { feed, state, tile: document.querySelector<HTMLElement>(CHAT_TILE)! };
}

test.each(["side", "floating"] as const)("Chat stored %s stands docked in the right column, and cannot be placed floating", async (placement) => {
  const { tile } = await mountChat(placement);
  expect(tile.parentElement?.className).toBe("visual-side-column");
  expect([...tile.classList]).toContain("visual-tile-side");
  expect([...within(tile).getByRole<HTMLSelectElement>("combobox", { name: "Place trax.chat" }).options].map((option) => option.text))
    .toEqual(["Main", "Left", "Right"]);
  // Docked at the right, only the other side is somewhere to go.
  expect(within(tile).getByRole<HTMLButtonElement>("button", { name: "Dock Chat at the right" }).disabled).toBe(true);
  expect(within(tile).getByRole<HTMLButtonElement>("button", { name: "Dock Chat at the left" }).disabled).toBe(false);
});

test("a dock button moves a tile to that side of the page at once; the page itself has none", async () => {
  const { state, tile } = await mountChat();
  const moved: WorkspaceState = { ...state, revision: 4, visuals: [state.visuals[0]!, { ...state.visuals[1]!, placement: "left" }] };
  vi.mocked(applyWorkspaceOperation).mockResolvedValue(moved);
  expect(within(document.querySelector<HTMLElement>(BROWSE_TILE)!).queryByRole("button", { name: /^Dock / })).toBeNull();

  fireEvent.click(within(tile).getByRole("button", { name: "Dock Chat at the left" }));
  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledWith(
    state.id, state.revision, { kind: "place", instance_id: "chat-instance", placement: "left" }, expect.any(String)), { interval: 1 });
  await waitFor(() => expect(document.querySelector(`.visual-side-column-left > ${CHAT_TILE}`)).not.toBeNull(), { interval: 1 });
  // The left column stands before the page's strip, and its button has nowhere left to go.
  const stage = document.querySelector(".visual-stage")!;
  expect([...stage.children].map((child) => child.className)).toEqual(["visual-side-column visual-side-column-left", "visual-main-strip"]);
  await waitFor(() => expect(screen.getByRole<HTMLButtonElement>("button", { name: "Dock Chat at the left" }).disabled).toBe(true), { interval: 1 });
});

test("a dock button brings back a Chat that stands aside, to the side it names", async () => {
  const { feed, state, tile } = await mountChat();
  act(() => feed.stepAside());
  fireEvent.click(within(tile).getByRole("button", { name: "Dock Chat at the left" }));
  expect(feed.snapshot().aside).toBe(0);
  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledWith(
    state.id, state.revision, { kind: "place", instance_id: "chat-instance", placement: "left" }, expect.any(String)), { interval: 1 });
});

test("when the assistant shows something Chat stands aside: the same tile floats, folded, and its column takes no room, until it is docked", async () => {
  const { feed, tile } = await mountChat();
  act(() => feed.stepAside());
  // The same element: Chat was never mounted again, so its draft and its scroll stay.
  expect(document.querySelector(CHAT_TILE)).toBe(tile);
  expect([...tile.classList]).toEqual(expect.arrayContaining(["visual-tile-floating", "visual-tile-aside", "visual-tile-collapsed"]));
  expect(tile.parentElement?.className).toBe("visual-side-column visual-side-column-vacant");
  // As the browser leaves a tile sized by hand.
  Object.assign(tile.style, { width: "500px", height: "300px" });

  // Back at the side it came from: nothing to write to the canvas.
  fireEvent.click(within(tile).getByRole("button", { name: "Dock Chat at the right" }));
  expect(document.querySelector(CHAT_TILE)).toBe(tile);
  expect([...tile.classList]).toContain("visual-tile-side");
  expect([...tile.classList]).not.toContain("visual-tile-floating");
  expect(tile.parentElement?.className).toBe("visual-side-column");
  expect([tile.style.width, tile.style.height]).toEqual(["", ""]);
  expect(applyWorkspaceOperation).not.toHaveBeenCalled();
});

test("Chat aside opens under the pointer, folds once it has left, and folds at once when the assistant shows more", async () => {
  const { feed, tile } = await mountChat();
  const folded = () => tile.classList.contains("visual-tile-collapsed");
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  act(() => feed.stepAside());
  fireEvent.pointerEnter(tile);
  act(() => { vi.advanceTimersByTime(OPEN_AFTER_MS); });
  expect(folded()).toBe(false);
  fireEvent.pointerLeave(tile);
  act(() => { vi.advanceTimersByTime(FOLD_AFTER_MS); });
  expect(folded()).toBe(true);

  // The fold button does it for the keyboard, and nothing of it is remembered.
  fireEvent.click(within(tile).getByRole("button", { name: "Expand Chat" }));
  expect(folded()).toBe(false);
  expect(localStorage.getItem(STATE_KEY)).toBeNull();
  act(() => feed.stepAside());
  expect(folded()).toBe(true);
});

test("the Chat button docks a Chat that stands aside", async () => {
  const { feed, state, tile } = await mountChat();
  act(() => feed.stepAside());
  expect([...tile.classList]).toContain("visual-tile-floating");
  fireEvent.click(within(screen.getByRole("toolbar", { name: "Canvas controls" })).getByRole("button", { name: "Chat" }));
  expect(feed.snapshot().aside).toBe(0);
  expect([...tile.classList]).toContain("visual-tile-side");
  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledWith(
    state.id, state.revision, { kind: "focus", instance_id: "chat-instance" }, expect.any(String)), { interval: 1 });
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
  render(<QueryClientProvider client={client}><ProfileContext value={PROFILE}>
    <Canvas><div>Browse record view</div></Canvas></ProfileContext></QueryClientProvider>);

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
  vi.mocked(applyWorkspaceOperation).mockResolvedValue({ ...withRecord, revision: 4 });
  render(<QueryClientProvider client={createQueryClient(() => {})}><Canvas><div>List</div></Canvas></QueryClientProvider>);
  await waitFor(() => expect(document.querySelector("[data-visual-instance]")).not.toBeNull(), { interval: 1 });
  const button = screen.getByRole("button", { name: "Chat" }) as HTMLButtonElement;
  await waitFor(() => expect(button.disabled).toBe(false), { interval: 1 });
  fireEvent.click(button);
  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledWith(
    withRecord.id, withRecord.revision, { kind: "show", visual_type: "trax.chat", record_id: null },
    expect.any(String)), { interval: 1 });
});

test("on a page with no record, Chat drops the record an earlier page gave it", async () => {
  history.replaceState(null, "", "#/list/Issue");
  const chat: WorkspaceState["visuals"][number] = {
    id: "chat-instance", type: "trax.chat", version: 1, placement: "side",
    record_id: "earlier-record", params: {},
  };
  const state = { ...workspace, visuals: [...workspace.visuals, chat] };
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [
      { type: "trax.browse", version: 1, title: "Browse", description: "Browse records",
        default_size: "wide", requires: [], parameter_schema: {} },
      { type: "trax.chat", version: 1, title: "Chat", description: "Chat about the page",
        default_size: "compact", requires: [], parameter_schema: {} },
    ],
  });
  vi.mocked(createDefaultWorkspace).mockResolvedValue(state);
  vi.mocked(getWorkspace).mockResolvedValue(state);
  vi.mocked(applyWorkspaceOperation).mockResolvedValue({ ...state, revision: 4 });
  render(<QueryClientProvider client={createQueryClient(() => {})}><Canvas><div>List</div></Canvas></QueryClientProvider>);

  const button = screen.getByRole("button", { name: "Chat" }) as HTMLButtonElement;
  await waitFor(() => expect(button.disabled).toBe(false), { interval: 1 });
  fireEvent.click(button);
  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledWith(
    state.id, state.revision, { kind: "show", visual_type: "trax.chat", record_id: null },
    expect.any(String)), { interval: 1 });
});

test("a canvas write waits for a pending one and uses its new revision", async () => {
  const recordId = "61d3a095-c7f1-4d27-a4c4-a5b1c218a31e";
  history.replaceState(null, "", `#/lookup/${recordId}`);
  const chat: WorkspaceState["visuals"][number] = {
    id: "chat-instance", type: "trax.chat", version: 1, placement: "side",
    record_id: null, params: {},
  };
  const initial = { ...workspace, visuals: [...workspace.visuals, chat] };
  const afterOperation: WorkspaceState = {
    ...initial, revision: 4,
    visuals: [...workspace.visuals, { ...chat, placement: "floating", record_id: recordId }],
  };
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

  const chatButton = screen.getByRole("button", { name: "Chat" });
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

  const chatButton = screen.getByRole("button", { name: "Chat" });
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

  const button = await screen.findByRole("button", { name: "Chat" });
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false), { interval: 1 });
  fireEvent.click(button);
  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledWith(
    workspace.id, workspace.revision,
    { kind: "show", visual_type: "trax.chat", record_id: artifactId },
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

  const button = await screen.findByRole("button", { name: "Chat" });
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false), { interval: 1 });
  fireEvent.click(button);
  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledWith(
    state.id, state.revision,
    { kind: "show", visual_type: "trax.chat", record_id: artifactId },
    expect.any(String),
  ));
});

test("the Chat button shows Chat, where the server places it, and focuses it once shown", async () => {
  browseOnly();
  const chat = { id: "chat-instance", type: "trax.chat", version: 1, placement: "side" as const, record_id: null, params: {} };
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
    workspace.id, 3, { kind: "show", visual_type: "trax.chat", record_id: null }, expect.any(String)));
  await waitFor(() => expect(client.getQueryData<WorkspaceState>(["workspace", workspace.id])?.revision).toBe(4), { interval: 1 });
  await waitFor(() => expect(button().disabled).toBe(false), { interval: 1 });
  fireEvent.click(button());
  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenLastCalledWith(
    workspace.id, 4, { kind: "focus", instance_id: "chat-instance" }, expect.any(String)));
});

const BROWSE_TILE = `[data-visual-instance="${workspace.visuals[0]!.id}"]`;
const STATE_KEY = storageKey(PROFILE.email);

function storedTile() {
  return rememberedTile(parseState(JSON.parse(localStorage.getItem(STATE_KEY)!)), "trax.browse");
}

/** The floating page, 800 x 600 stage and a 300 x 400 tile, with its top bar. */
async function mountFloating() {
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [{ type: "trax.browse", version: 1, title: "Browse", description: "Browse records",
      default_size: "wide", requires: [], parameter_schema: {} }],
  });
  vi.mocked(createDefaultWorkspace).mockResolvedValue(workspace);
  vi.mocked(getWorkspace).mockResolvedValue(workspace);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Canvas><div>Browse content</div></Canvas></QueryClientProvider>);
  const handle = await screen.findByRole("button", { name: "Move Browse" });
  const tile = document.querySelector<HTMLElement>(BROWSE_TILE)!;
  const bar = tile.querySelector<HTMLElement>(".visual-tile-toolbar")!;
  const stage = document.querySelector<HTMLElement>(".visual-stage")!;
  // jsdom has no pointer capture and no layout.
  Element.prototype.setPointerCapture = vi.fn();
  Object.defineProperty(stage, "clientWidth", { configurable: true, value: 800 });
  Object.defineProperty(stage, "clientHeight", { configurable: true, value: 600 });
  Object.defineProperty(tile, "offsetWidth", { configurable: true, value: 300 });
  Object.defineProperty(tile, "offsetHeight", { configurable: true, value: 400 });
  return { tile, bar, handle, title: within(bar).getByText("Browse") };
}

test("pressing the title bar and dragging moves the tile with the pointer, and the place is kept on release", async () => {
  const { tile, title } = await mountFloating();
  fireEvent.pointerDown(title, { pointerId: 1, clientX: 100, clientY: 100 });
  fireEvent.pointerMove(title, { pointerId: 1, clientX: 140, clientY: 120 });
  expect(tile.style.transform).toBe("translate(40px, 20px)");
  fireEvent.pointerUp(title, { pointerId: 1, clientX: 140, clientY: 120 });
  expect(tile.style.transform).toBe("");
  expect([tile.style.left, tile.style.top]).toEqual(["40px", "20px"]);
  // A drag is not a click: the tile stays open.
  expect(tile.classList.contains("visual-tile-collapsed")).toBe(false);
  expect(storedTile()).toEqual({ collapsed: false, place: { left: 40, top: 20 } });
});

test("a drag by the title bar follows every move with a transform alone: the canvas does not render", async () => {
  let commits = 0;
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [{ type: "trax.browse", version: 1, title: "Browse", description: "Browse records",
      default_size: "wide", requires: [], parameter_schema: {} }],
  });
  vi.mocked(createDefaultWorkspace).mockResolvedValue(workspace);
  vi.mocked(getWorkspace).mockResolvedValue(workspace);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Profiler id="canvas" onRender={() => { commits += 1; }}>
    <Canvas><div>Browse content</div></Canvas>
  </Profiler></QueryClientProvider>);
  await screen.findByRole("button", { name: "Move Browse" });
  const tile = document.querySelector<HTMLElement>(BROWSE_TILE)!;
  const bar = tile.querySelector<HTMLElement>(".visual-tile-toolbar")!;
  Element.prototype.setPointerCapture = vi.fn();
  Object.defineProperty(document.querySelector(".visual-stage"), "clientWidth", { configurable: true, value: 800 });
  Object.defineProperty(document.querySelector(".visual-stage"), "clientHeight", { configurable: true, value: 600 });
  Object.defineProperty(tile, "offsetWidth", { configurable: true, value: 300 });
  Object.defineProperty(tile, "offsetHeight", { configurable: true, value: 400 });

  fireEvent.pointerDown(bar, { pointerId: 1, clientX: 10, clientY: 10 });
  const before = commits;
  for (let step = 1; step <= 30; step += 1) {
    fireEvent.pointerMove(bar, { pointerId: 1, clientX: 10 + step * 10, clientY: 10 + step });
    expect(tile.style.transform).toBe(`translate(${Math.min(500, step * 10)}px, ${step}px)`);
  }
  expect(commits).toBe(before);
  fireEvent.pointerCancel(bar, { pointerId: 1 });
  // The browser taking the pointer puts the tile back.
  expect(tile.style.transform).toBe("");
  expect(tile.style.left).toBe("");
  expect(localStorage.getItem(STATE_KEY)).toBeNull();
});

test("a press on the title bar that does not move collapses the tile to its bar, and the next expands it", async () => {
  const { tile, title } = await mountFloating();
  fireEvent.pointerDown(title, { pointerId: 1, clientX: 100, clientY: 100 });
  fireEvent.pointerMove(title, { pointerId: 1, clientX: 100 + CLICK_SLOP, clientY: 100 });
  fireEvent.pointerUp(title, { pointerId: 1, clientX: 100 + CLICK_SLOP, clientY: 100 });
  expect(tile.classList.contains("visual-tile-collapsed")).toBe(true);
  // A click moves nothing, even by the pixels the hand wandered.
  expect(tile.style.transform).toBe("");
  expect(tile.style.left).toBe("");
  expect(storedTile().collapsed).toBe(true);

  fireEvent.pointerDown(title, { pointerId: 2, clientX: 100, clientY: 100 });
  fireEvent.pointerUp(title, { pointerId: 2, clientX: 100, clientY: 100 });
  expect(tile.classList.contains("visual-tile-collapsed")).toBe(false);
  expect(storedTile().collapsed).toBe(false);
});

test("a collapsed tile can be dragged by its bar, and stays collapsed", async () => {
  const { tile, title } = await mountFloating();
  fireEvent.pointerDown(title, { pointerId: 1, clientX: 100, clientY: 100 });
  fireEvent.pointerUp(title, { pointerId: 1, clientX: 100, clientY: 100 });
  fireEvent.pointerDown(title, { pointerId: 2, clientX: 100, clientY: 100 });
  fireEvent.pointerMove(title, { pointerId: 2, clientX: 160, clientY: 100 });
  fireEvent.pointerUp(title, { pointerId: 2, clientX: 160, clientY: 100 });
  expect(tile.classList.contains("visual-tile-collapsed")).toBe(true);
  expect(storedTile()).toEqual({ collapsed: true, place: { left: 60, top: 0 } });
});

test("the bar's buttons and place control keep working: pressing them starts no drag and no collapse", async () => {
  const { tile, bar } = await mountFloating();
  const focus = within(bar).getByRole("button", { name: "Focus" });
  fireEvent.pointerDown(focus, { pointerId: 1, clientX: 100, clientY: 100 });
  fireEvent.pointerMove(focus, { pointerId: 1, clientX: 180, clientY: 100 });
  fireEvent.pointerUp(focus, { pointerId: 1, clientX: 180, clientY: 100 });
  const place = within(bar).getByRole("combobox", { name: "Place trax.browse" });
  fireEvent.pointerDown(place, { pointerId: 2, clientX: 100, clientY: 100 });
  fireEvent.pointerUp(place, { pointerId: 2, clientX: 100, clientY: 100 });
  expect(tile.style.transform).toBe("");
  expect(tile.classList.contains("visual-tile-collapsed")).toBe(false);
  expect(applyWorkspaceOperation).not.toHaveBeenCalled();
  expect(localStorage.getItem(STATE_KEY)).toBeNull();
  fireEvent.click(focus);
  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalled());
});

test("a right-button press on the bar starts nothing", async () => {
  const { tile, title } = await mountFloating();
  fireEvent.pointerDown(title, { pointerId: 1, button: 2, clientX: 100, clientY: 100 });
  fireEvent.pointerUp(title, { pointerId: 1, button: 2, clientX: 100, clientY: 100 });
  expect(tile.classList.contains("visual-tile-collapsed")).toBe(false);
});

test("the move handle still drags, and a press on it that does not move does not collapse", async () => {
  const { tile, handle } = await mountFloating();
  fireEvent.pointerDown(handle, { pointerId: 1, clientX: 100, clientY: 100 });
  fireEvent.pointerUp(handle, { pointerId: 1, clientX: 100, clientY: 100 });
  expect(tile.classList.contains("visual-tile-collapsed")).toBe(false);
  fireEvent.pointerDown(handle, { pointerId: 2, clientX: 100, clientY: 100 });
  fireEvent.pointerMove(handle, { pointerId: 2, clientX: 130, clientY: 150 });
  fireEvent.pointerUp(handle, { pointerId: 2, clientX: 130, clientY: 150 });
  expect([tile.style.left, tile.style.top]).toEqual(["30px", "50px"]);
  expect(storedTile().place).toEqual({ left: 30, top: 50 });
});

test("arrow keys on the handle move the tile and the place is kept", async () => {
  const { tile, handle } = await mountFloating();
  fireEvent.keyDown(handle, { key: "ArrowDown", shiftKey: true });
  expect(tile.style.top).toBe("48px");
  expect(storedTile().place).toEqual({ left: 0, top: 48 });
});

test("on a phone the bar is a plain header: pressing it moves and folds nothing", async () => {
  window.innerWidth = 390;
  const { tile, title } = await mountFloating();
  fireEvent.pointerDown(title, { pointerId: 1, clientX: 100, clientY: 100 });
  fireEvent.pointerMove(title, { pointerId: 1, clientX: 140, clientY: 100 });
  fireEvent.pointerUp(title, { pointerId: 1, clientX: 140, clientY: 100 });
  expect(tile.style.transform).toBe("");
  expect(tile.classList.contains("visual-tile-collapsed")).toBe(false);
});

test("the collapse button does what a click on the bar does, for the keyboard", async () => {
  const { tile, bar } = await mountFloating();
  fireEvent.click(within(bar).getByRole("button", { name: "Collapse Browse" }));
  expect(tile.classList.contains("visual-tile-collapsed")).toBe(true);
  expect(within(bar).getByRole("button", { name: "Expand Browse" }).getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(within(bar).getByRole("button", { name: "Expand Browse" }));
  expect(tile.classList.contains("visual-tile-collapsed")).toBe(false);
  expect(storedTile().collapsed).toBe(false);
});

test("a place and a collapse stored for this user show on the next load", async () => {
  localStorage.setItem(STATE_KEY, JSON.stringify(withTile(EMPTY_STATE, "trax.browse", { collapsed: true, place: { left: 120, top: 70 } })));
  const { tile } = await mountFloating();
  expect(tile.classList.contains("visual-tile-collapsed")).toBe(true);
  expect([tile.style.left, tile.style.top]).toEqual(["120px", "70px"]);
});

test("a stored place the stage has since shrunk below is held inside it", async () => {
  localStorage.setItem(STATE_KEY, JSON.stringify(withTile(EMPTY_STATE, "trax.browse", { place: { left: 700, top: 590 } })));
  const { tile } = await mountFloating();
  // The measure runs on the next layout, once jsdom has been told the sizes.
  fireEvent(window, new Event("resize"));
  expect([tile.style.left, tile.style.top]).toEqual(["500px", "200px"]);
});

test("storage that cannot hold the state still lets the tile move and collapse", async () => {
  // A value of another shape: the canvas must not overwrite it, and must not fail.
  const unreadable = JSON.stringify({ stars: [], notification: {} });
  localStorage.setItem(STATE_KEY, unreadable);
  const { tile, title } = await mountFloating();
  fireEvent.pointerDown(title, { pointerId: 1, clientX: 100, clientY: 100 });
  fireEvent.pointerMove(title, { pointerId: 1, clientX: 110, clientY: 120 });
  fireEvent.pointerUp(title, { pointerId: 1, clientX: 110, clientY: 120 });
  expect([tile.style.left, tile.style.top]).toEqual(["10px", "20px"]);
  fireEvent.pointerDown(title, { pointerId: 2, clientX: 100, clientY: 100 });
  fireEvent.pointerUp(title, { pointerId: 2, clientX: 100, clientY: 100 });
  expect(tile.classList.contains("visual-tile-collapsed")).toBe(true);
  expect(localStorage.getItem(STATE_KEY)).toBe(unreadable);
});

test("opening a saved view shows its places over the ones dragged before, and keeps what is collapsed", async () => {
  localStorage.setItem(STATE_KEY, JSON.stringify(withTile(EMPTY_STATE, "trax.browse", { collapsed: true, place: { left: 120, top: 70 } })));
  vi.mocked(listWorkspacePresets).mockResolvedValue([{
    id: "preset-id", name: "Triage", agent_instructions: null, continuation_record_id: null,
    state: { visuals: workspace.visuals, focused_instance: null, agent_instructions: null, continuation_record_id: null },
    created_at: "2026-09-29T08:00:00Z", modified_at: "2026-09-29T08:00:00Z",
  }]);
  vi.mocked(openWorkspacePreset).mockResolvedValue({
    ...workspace, revision: 4,
    visuals: workspace.visuals.map((visual) => ({ ...visual, floating_rect: { left: 72, top: 64, width: 440, height: 320 } })),
  });
  const { tile } = await mountFloating();
  fireEvent.click(screen.getByRole("button", { name: "Configure" }));
  fireEvent.click(await screen.findByRole("button", { name: "Open Triage" }));
  await screen.findByText("Opened “Triage”.");
  expect([tile.style.left, tile.style.top]).toEqual(["72px", "64px"]);
  expect(storedTile()).toEqual({ collapsed: true, place: null });
});

/** The stage and tile as the page draws them: jsdom lays nothing out. */
function drawnAt(tile: HTMLElement, left: number, top: number) {
  tile.getBoundingClientRect = () => ({ left, top, right: left + 300, bottom: top + 400, width: 300, height: 400, x: left, y: top, toJSON: () => ({}) });
}

/** Drag the tile to (left, top), then shrink the stage so it holds the tile at (heldLeft, heldTop). */
async function dragThenShrink(left: number, top: number, held: { left: number; top: number }) {
  const mounted = await mountFloating();
  const { tile, title } = mounted;
  fireEvent.pointerDown(title, { pointerId: 9, clientX: 0, clientY: 0 });
  fireEvent.pointerMove(title, { pointerId: 9, clientX: left, clientY: top });
  fireEvent.pointerUp(title, { pointerId: 9, clientX: left, clientY: top });
  const stage = document.querySelector<HTMLElement>(".visual-stage")!;
  Object.defineProperty(stage, "clientWidth", { configurable: true, value: held.left + 300 });
  Object.defineProperty(stage, "clientHeight", { configurable: true, value: held.top + 400 });
  fireEvent(window, new Event("resize"));
  expect([tile.style.left, tile.style.top]).toEqual([`${held.left}px`, `${held.top}px`]);
  // The stage held the tile inside itself; the place last dragged to still says otherwise.
  drawnAt(tile, held.left, held.top);
  return mounted;
}

test("a drag starts from where the tile is drawn, not from the place it was last dragged to", async () => {
  const { tile, title } = await dragThenShrink(500, 200, { left: 300, top: 100 });
  fireEvent.pointerDown(title, { pointerId: 1, clientX: 100, clientY: 100 });
  fireEvent.pointerMove(title, { pointerId: 1, clientX: 90, clientY: 100 });
  expect(tile.style.transform).toBe("translate(-10px, 0px)");
  fireEvent.pointerUp(title, { pointerId: 1, clientX: 90, clientY: 100 });
  expect(storedTile().place).toEqual({ left: 290, top: 100 });
});

test("an arrow key moves the tile from where it is drawn, not from the place it was last dragged to", async () => {
  const { handle } = await dragThenShrink(500, 200, { left: 300, top: 100 });
  fireEvent.keyDown(handle, { key: "ArrowUp" });
  expect(storedTile().place).toEqual({ left: 300, top: 84 });
});

test("a drag whose pointer capture was lost is over: the next press drags", async () => {
  const { tile, title } = await mountFloating();
  fireEvent.pointerDown(title, { pointerId: 1, clientX: 100, clientY: 100 });
  fireEvent.pointerMove(title, { pointerId: 1, clientX: 130, clientY: 100 });
  // The captured element left the page, so no pointerup will arrive.
  fireEvent.lostPointerCapture(title, { pointerId: 1 });
  expect(tile.style.transform).toBe("");
  fireEvent.pointerDown(title, { pointerId: 2, clientX: 100, clientY: 100 });
  fireEvent.pointerMove(title, { pointerId: 2, clientX: 120, clientY: 100 });
  expect(tile.style.transform).toBe("translate(20px, 0px)");
  fireEvent.pointerUp(title, { pointerId: 2, clientX: 120, clientY: 100 });
  expect(storedTile().place).toEqual({ left: 20, top: 0 });
});

test("the normal end of a drag is not undone by the capture release that follows it", async () => {
  const { tile, title } = await mountFloating();
  fireEvent.pointerDown(title, { pointerId: 1, clientX: 100, clientY: 100 });
  fireEvent.pointerMove(title, { pointerId: 1, clientX: 140, clientY: 100 });
  fireEvent.pointerUp(title, { pointerId: 1, clientX: 140, clientY: 100 });
  fireEvent.lostPointerCapture(title, { pointerId: 1 });
  expect([tile.style.left, tile.style.transform]).toEqual(["40px", ""]);
});

test("a tile held inside a smaller stage returns to its saved place when the stage grows back", async () => {
  localStorage.setItem(STATE_KEY, JSON.stringify(withTile(EMPTY_STATE, "trax.browse", { place: { left: 500, top: 100 } })));
  const { tile } = await mountFloating();
  const stage = document.querySelector<HTMLElement>(".visual-stage")!;
  const width = (value: number) => Object.defineProperty(stage, "clientWidth", { configurable: true, value });
  width(600);
  fireEvent(window, new Event("resize"));
  expect(tile.style.left).toBe("300px");
  width(800);
  fireEvent(window, new Event("resize"));
  expect(tile.style.left).toBe("500px");
});

test("on a phone the saved place is left alone: the tile is a plain header there", async () => {
  window.innerWidth = 390;
  localStorage.setItem(STATE_KEY, JSON.stringify(withTile(EMPTY_STATE, "trax.browse", { place: { left: 700, top: 100 } })));
  const { tile } = await mountFloating();
  fireEvent(window, new Event("resize"));
  expect(tile.style.left).toBe("700px");
});

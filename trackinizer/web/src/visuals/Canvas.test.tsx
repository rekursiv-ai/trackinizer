import { focusManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useEffect } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { getVisualCatalog } from "../api/visuals";
import { findRef } from "../api/detail";
import {
  applyWorkspaceOperation,
  createDefaultWorkspace,
  getWorkspace,
  listConnectableSessions,
  setWorkspaceConnection,
  type WorkspaceState,
} from "../api/workspaces";
import { createWorkspacePreset, listWorkspacePresets, openWorkspacePreset } from "../api/presets";
import { createQueryClient } from "../app/queryClient";
import { Canvas, newerWorkspace } from "./Canvas";

vi.mock("../api/visuals", () => ({ getVisualCatalog: vi.fn() }));
vi.mock("../api/workspaces", () => ({
  createDefaultWorkspace: vi.fn(),
  getWorkspace: vi.fn(),
  applyWorkspaceOperation: vi.fn(),
  listConnectableSessions: vi.fn(),
  setWorkspaceConnection: vi.fn(),
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
  connected_session_id: null,
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

test("saves the current canvas with workflow guidance and reopens it disconnected", async () => {
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
  vi.mocked(createDefaultWorkspace).mockResolvedValue({ ...workspace, connected_session_id: "session-id" });
  vi.mocked(getWorkspace).mockResolvedValue({ ...workspace, connected_session_id: "session-id" });
  vi.mocked(listWorkspacePresets).mockResolvedValue([saved]);
  vi.mocked(createWorkspacePreset).mockResolvedValue(saved);
  vi.mocked(openWorkspacePreset).mockResolvedValue({
    ...workspace,
    revision: 4,
    connected_session_id: null,
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
  expect((await screen.findByRole("status")).textContent).toContain("Previous Chat session disconnected.");
  expect(client.getQueryData<WorkspaceState>(["workspace", workspace.id])?.connected_session_id).toBeNull();
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
  await waitFor(() => expect(createWorkspacePreset).toHaveBeenCalledTimes(2));
  expect(vi.mocked(createWorkspacePreset).mock.calls[1]?.[3]).toBe(vi.mocked(createWorkspacePreset).mock.calls[0]?.[3]);
  fireEvent.click(screen.getByRole("button", { name: "Open Issue triage" }));
  expect((await screen.findByRole("alert")).textContent).toContain("Could not open this saved view.");
  fireEvent.click(screen.getByRole("button", { name: "Open Issue triage" }));
  await waitFor(() => expect(openWorkspacePreset).toHaveBeenCalledTimes(2));
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
  const mounted = vi.fn();
  function Content() {
    useEffect(() => mounted(), []);
    return <div>Browse content</div>;
  }
  render(<QueryClientProvider client={createQueryClient(() => {})}><Canvas><Content /></Canvas></QueryClientProvider>);
  expect(await screen.findByText("Browse content")).toBeTruthy();
  await act(async () => open(workspace));
  await waitFor(() => expect(document.querySelector(`[data-visual-instance="${workspace.visuals[0]!.id}"]`)).not.toBeNull());
  expect(mounted).toHaveBeenCalledTimes(1);
});

test("the canvas reads itself again every 2 s only while an agent session is paired", async () => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  browseOnly();
  vi.mocked(createDefaultWorkspace).mockResolvedValue(workspace);
  vi.mocked(getWorkspace).mockResolvedValue(workspace);
  const client = createQueryClient(() => {});
  render(<QueryClientProvider client={client}><Canvas><div>Browse content</div></Canvas></QueryClientProvider>);
  await waitFor(() => expect(getWorkspace).toHaveBeenCalledTimes(1));
  await act(async () => { vi.advanceTimersByTime(10_000); });
  expect(getWorkspace).toHaveBeenCalledTimes(1);

  // A paired agent may change the canvas, and only a read shows it.
  await act(async () => { client.setQueryData(["workspace", workspace.id], { ...workspace, revision: 4, connected_session_id: "session-id" }); });
  await act(async () => { vi.advanceTimersByTime(2_000); });
  expect(getWorkspace).toHaveBeenCalledTimes(2);
});

test("the canvas reads itself again when its tab comes back, for a change made in another tab", async () => {
  browseOnly();
  vi.mocked(createDefaultWorkspace).mockResolvedValue(workspace);
  vi.mocked(getWorkspace).mockResolvedValue(workspace);
  render(<QueryClientProvider client={createQueryClient(() => {})}><Canvas><div>Browse content</div></Canvas></QueryClientProvider>);
  await waitFor(() => expect(getWorkspace).toHaveBeenCalledTimes(1));
  act(() => {
    focusManager.setFocused(false);
    focusManager.setFocused(true);
  });
  await waitFor(() => expect(getWorkspace).toHaveBeenCalledTimes(2));
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
  await waitFor(() => expect(graphToggle).toHaveProperty("disabled", false));
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
      { type: "trax.chat", version: 1, title: "Chat", description: "Connect session",
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
  vi.mocked(applyWorkspaceOperation).mockResolvedValue(changed);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Canvas><div>Browse content</div></Canvas></QueryClientProvider>);
  fireEvent.click(await screen.findByRole("button", { name: "Configure" }));
  fireEvent.click(await screen.findByRole("checkbox", { name: /Chat/ }));
  await waitFor(() => expect(client.getQueryData<WorkspaceState>(["workspace", workspace.id])?.revision).toBe(2));
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
      { type: "trax.chat", version: 1, title: "Chat", description: "Connect session",
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
  await waitFor(() => expect((chatButton as HTMLButtonElement).disabled).toBe(false));
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
      { type: "trax.chat", version: 1, title: "Chat", description: "Connect session",
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
  await waitFor(() => expect((chatButton as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(chatButton);

  expect(await screen.findByRole("button", { name: "Collapse" })).toBeTruthy();
  expect(document.querySelector(".visual-tile-mobile-expanded")).toBeTruthy();
});

test("opening a Chat record reveals Browse before navigating to the record", async () => {
  const chat: WorkspaceState["visuals"][number] = {
    id: "chat-instance", type: "trax.chat", version: 1, placement: "main",
    record_id: "record-id", params: {},
  };
  const initial = { ...workspace, visuals: [chat] };
  const revealed: WorkspaceState = {
    ...initial,
    revision: 4,
    visuals: [chat, { id: "browse-instance", type: "trax.browse", version: 1,
      placement: "main", record_id: null, params: {} }],
  };
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [
      { type: "trax.browse", version: 1, title: "Browse", description: "Browse records",
        default_size: "wide", requires: [], parameter_schema: {} },
      { type: "trax.chat", version: 1, title: "Chat", description: "Connect session",
        default_size: "compact", requires: [], parameter_schema: {} },
    ],
  });
  vi.mocked(createDefaultWorkspace).mockResolvedValue(initial);
  vi.mocked(getWorkspace).mockResolvedValue(initial);
  vi.mocked(applyWorkspaceOperation).mockResolvedValue(revealed);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Canvas><div>Browse record view</div></Canvas></QueryClientProvider>);

  fireEvent.click(await screen.findByRole("link", { name: "Issue#42 Context record" }));

  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledWith(
    initial.id,
    initial.revision,
    { kind: "show", visual_type: "trax.browse", placement: "main", record_id: "record-id" },
    expect.any(String),
  ));
  expect(await screen.findByText("Browse record view")).toBeTruthy();
  await waitFor(() => expect(location.hash).toBe("#/lookup/record-id"));
});

test("Chat pairing waits for a pending canvas write and uses its new revision", async () => {
  const recordId = "61d3a095-c7f1-4d27-a4c4-a5b1c218a31e";
  history.replaceState(null, "", `#/lookup/${recordId}`);
  const chat: WorkspaceState["visuals"][number] = {
    id: "chat-instance", type: "trax.chat", version: 1, placement: "side",
    record_id: null, params: {},
  };
  const initial = { ...workspace, visuals: [...workspace.visuals, chat] };
  const afterOperation: WorkspaceState = { ...initial, revision: 4 };
  const afterPairing: WorkspaceState = { ...afterOperation, revision: 5,
    connected_session_id: "session-id" };
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [
      { type: "trax.browse", version: 1, title: "Browse", description: "Browse records",
        default_size: "wide", requires: [], parameter_schema: {} },
      { type: "trax.chat", version: 1, title: "Chat", description: "Connect session",
        default_size: "compact", requires: [], parameter_schema: {} },
    ],
  });
  vi.mocked(createDefaultWorkspace).mockResolvedValue(initial);
  vi.mocked(getWorkspace).mockResolvedValue(initial);
  vi.mocked(listConnectableSessions).mockResolvedValue([
    { id: "session-id", title: "codex session", actor: "researcher", cli: "codex" },
  ]);
  let finishOperation: (state: WorkspaceState) => void = () => { throw new Error("Write not pending"); };
  vi.mocked(applyWorkspaceOperation).mockImplementation(() => new Promise((resolve) => { finishOperation = resolve; }));
  vi.mocked(setWorkspaceConnection).mockResolvedValue(afterPairing);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Canvas><div>Record detail</div></Canvas></QueryClientProvider>);

  const chatButton = screen.getByRole("button", { name: "Chat about this" });
  await waitFor(() => expect((chatButton as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(chatButton);
  // A mock's call changes nothing on screen, so only the interval checks again.
  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledOnce(), { interval: 1 });
  const connect = await screen.findByRole("button", { name: "Connect session" });
  expect((connect as HTMLButtonElement).disabled).toBe(true);
  expect(setWorkspaceConnection).not.toHaveBeenCalled();

  await act(async () => { finishOperation(afterOperation); });
  await waitFor(() => expect((screen.getByRole("button", { name: "Connect session" }) as HTMLButtonElement).disabled)
    .toBe(false));
  fireEvent.click(screen.getByRole("button", { name: "Connect session" }));
  fireEvent.click(await screen.findByRole("button", { name: "Connect codex session" }));
  await waitFor(() => expect(setWorkspaceConnection).toHaveBeenCalledWith(initial.id, 4, "session-id"));
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
  await waitFor(() => expect((chatButton as HTMLButtonElement).disabled).toBe(false));
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
      { type: "trax.chat", version: 1, title: "Chat", description: "Connect session",
        default_size: "compact", requires: [], parameter_schema: {} },
    ],
  });
  vi.mocked(createDefaultWorkspace).mockResolvedValue(workspace);
  vi.mocked(getWorkspace).mockResolvedValue(workspace);
  vi.mocked(applyWorkspaceOperation).mockResolvedValue({ ...workspace, revision: 4 });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Canvas><div>Artifact page</div></Canvas></QueryClientProvider>);

  const button = await screen.findByRole("button", { name: "Chat about this" });
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
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
      { type: "trax.chat", version: 1, title: "Chat", description: "Connect session",
        default_size: "compact", requires: [], parameter_schema: {} },
    ],
  });
  vi.mocked(createDefaultWorkspace).mockResolvedValue(state);
  vi.mocked(getWorkspace).mockResolvedValue(state);
  vi.mocked(applyWorkspaceOperation).mockResolvedValue({ ...state, revision: 4 });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Canvas><div>Record page</div></Canvas></QueryClientProvider>);

  const button = await screen.findByRole("button", { name: "Chat about this" });
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(button);
  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledWith(
    state.id, state.revision,
    { kind: "show", visual_type: "trax.chat", placement: "floating", record_id: artifactId },
    expect.any(String),
  ));
});

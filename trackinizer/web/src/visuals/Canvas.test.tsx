import { focusManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render as renderBare, screen, waitFor, within } from "@testing-library/react";
import { Profiler, useEffect, useLayoutEffect, useRef, type ReactElement, type ReactNode } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { getVisualCatalog } from "../api/visuals";
import { findRef } from "../api/detail";
import {
  applyWorkspaceOperation,
  createDefaultWorkspace,
  getWorkspace,
  type WorkspaceState,
} from "../api/workspaces";
import { ProfileContext } from "../app/boot";
import { WorkbenchSlot } from "../app/workbenchSlot";
import { PROFILE } from "../detail/testing";
import { createQueryClient } from "../app/queryClient";
import { newerWorkspace } from "../stations/cache";
import { setStation } from "../stations/store";
import { setStationLayout } from "../api/stations";
import { Canvas } from "./Canvas";
import { forgetHistories } from "./canvasHistory";
import { currentChatColumn, setChatHidden } from "./chatColumnState";
import { CLICK_SLOP, rememberedTile, SNAP_SIDE, SNAP_TOP, TEAR_SLOP } from "./floatingTile";
import { flushLayouts, forgetLayouts, LAYOUT_EVERY_MS, type Layout } from "./stationLayout";
import { useWorkspaceActions } from "./workspaceActions";

vi.mock("../api/visuals", () => ({ getVisualCatalog: vi.fn() }));
vi.mock("../api/stations", () => ({ setStationLayout: vi.fn().mockResolvedValue(undefined) }));
vi.mock("../api/workspaces", () => ({
  createDefaultWorkspace: vi.fn(),
  getWorkspace: vi.fn(),
  applyWorkspaceOperation: vi.fn(),
  sendWorkspaceMessage: vi.fn(),
}));
vi.mock("../api/detail", () => ({
  findRef: vi.fn(),
  getDetail: vi.fn().mockResolvedValue({
    self: { id: "record-id", kind: "Issue", seq: 42, title: "Context record" },
    edges: {}, backlinks: {}, changes: [],
  }),
}));

/** The canvas shows the signed-in user's state, so every render has a profile above it; its controls stand in the sidebar's slot, beside it. */
function render(ui: ReactElement) {
  return renderBare(ui, { wrapper: ({ children }: { children: ReactNode }) => <ProfileContext value={PROFILE}>{children}<WorkbenchSlot /></ProfileContext> });
}

const workspace: WorkspaceState = {
  id: "c5286865-67b6-4bd8-ab51-e06e10c326c5",
  name: "unnamed-workstream#1",
  conversation_id: null,
  page: null,
  revision: 3,
  focused_instance: null,
  visuals: [{ id: "889ffcb2-cf44-43e7-9806-eb08428c6203", type: "trax.browse", version: 1,
    placement: "floating", record_id: null, params: {} }],
};

afterEach(() => {
  setStation(null);
  forgetLayouts();
  forgetHistories();
  cleanup();
  vi.clearAllMocks();
  vi.mocked(setStationLayout).mockResolvedValue(undefined);
  vi.useRealTimers();
  focusManager.setFocused(undefined);
  window.innerWidth = 1024;
  history.replaceState(null, "", "#/");
  sessionStorage.clear();
  localStorage.clear();
});

/** The layout keys the canvas has written to its station so far, later writes over earlier ones. */
function written(): Layout {
  flushLayouts();
  return Object.assign({}, ...vi.mocked(setStationLayout).mock.calls.map(([, patch]) => patch)) as Layout;
}

/** The Browse visual alone, as the server's catalog describes it. */
function browseOnly() {
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [{ type: "trax.browse", version: 1, title: "Browse", description: "Browse records",
      default_size: "wide", requires: [], parameter_schema: {} }],
  });
}

test.each(["main", "floating"] as const)("the browse pane keeps its content mounted while the server's %s canvas arrives", async (placement) => {
  browseOnly();
  let open: (state: WorkspaceState) => void = () => { throw new Error("No canvas pending"); };
  vi.mocked(createDefaultWorkspace).mockImplementation(() => new Promise((resolve) => { open = resolve; }));
  vi.mocked(getWorkspace).mockResolvedValue(workspace);
  // A saved canvas may move the page out of the initial main strip.
  const inStrip: WorkspaceState = { ...workspace, visuals: [{ ...workspace.visuals[0]!, placement }] };
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

test("Browse's initial children mount connected and can autofocus before a saved station arrives", () => {
  browseOnly();
  vi.mocked(createDefaultWorkspace).mockImplementation(() => new Promise(() => {}));
  const connected = vi.fn();
  function Content() {
    const input = useRef<HTMLInputElement>(null);
    useLayoutEffect(() => { connected(input.current?.isConnected); }, []);
    return <input ref={input} aria-label="Initial Browse draft" autoFocus />;
  }
  render(<QueryClientProvider client={createQueryClient(() => {})}><Canvas><Content /></Canvas></QueryClientProvider>);
  expect(connected.mock.calls).toEqual([[true]]);
  expect(document.activeElement).toBe(screen.getByRole("textbox", { name: "Initial Browse draft" }));
});

test("Browse keeps focus when an unrelated Canvas stage measurement renders", async () => {
  browseOnly();
  const initial: WorkspaceState = { ...workspace, visuals: [{ ...workspace.visuals[0]!, placement: "main" }] };
  vi.mocked(createDefaultWorkspace).mockResolvedValue(initial);
  render(<QueryClientProvider client={createQueryClient(() => {})}><Canvas><input aria-label="Focused Browse draft" /></Canvas></QueryClientProvider>);
  const input = await screen.findByRole<HTMLInputElement>("textbox", { name: "Focused Browse draft" });
  await waitFor(() => expect(document.querySelector(`[data-visual-instance="${initial.visuals[0]!.id}"]`)).not.toBeNull(), { interval: 1 });
  input.focus();
  const stage = document.querySelector(".visual-stage")!;
  Object.defineProperty(stage, "clientWidth", { configurable: true, value: 900 });
  fireEvent(window, new Event("resize"));
  expect(document.activeElement).toBe(input);
});

test("docked Browse fits its flow slot and scroll clipping without moving focus or DOM", async () => {
  browseOnly();
  const initial: WorkspaceState = { ...workspace, visuals: [{ ...workspace.visuals[0]!, placement: "main" }] };
  vi.mocked(createDefaultWorkspace).mockResolvedValue(initial);
  render(<QueryClientProvider client={createQueryClient(() => {})}><Canvas><input aria-label="Clipped Browse draft" /></Canvas></QueryClientProvider>);
  const input = await screen.findByRole<HTMLInputElement>("textbox", { name: "Clipped Browse draft" });
  await waitFor(() => expect(document.querySelector(`[data-visual-instance="${initial.visuals[0]!.id}"]`)).not.toBeNull(), { interval: 1 });
  const stage = document.querySelector<HTMLElement>(".visual-stage")!;
  const slot = document.querySelector<HTMLElement>(".visual-browse-slot")!;
  const strip = slot.parentElement!;
  const tile = input.closest<HTMLElement>(".visual-tile")!;
  drawnAt(stage, 20, 10);
  drawnAt(slot, 100, 50);
  drawnAt(strip, 150, 40);
  Object.defineProperties(slot, { clientLeft: { value: 1 }, clientWidth: { value: 300 }, clientHeight: { value: 400 } });
  stage.scrollLeft = 7;
  stage.scrollTop = 11;
  input.focus();
  fireEvent(window, new Event("resize"));
  expect([tile.style.left, tile.style.top, tile.style.width, tile.style.height, tile.style.clipPath]).toEqual(["101px", "50px", "300px", "400px", "inset(0px 81px 40px 49px)"]);
  drawnAt(slot, 50, 50);
  fireEvent.scroll(strip);
  expect([tile.style.left, tile.style.clipPath]).toEqual(["51px", "inset(0px 31px 40px 99px)"]);
  expect(tile.parentElement).toBe(stage);
  expect(document.activeElement).toBe(input);
});

test("Browse keeps its input state and DOM identity when live placement changes between strip and floating", async () => {
  browseOnly();
  const initial: WorkspaceState = { ...workspace, visuals: [{ ...workspace.visuals[0]!, placement: "main" }] };
  vi.mocked(createDefaultWorkspace).mockResolvedValue(initial);
  const mounted = vi.fn();
  function Content() {
    useEffect(() => mounted(), []);
    return <input aria-label="Browse draft" defaultValue="" />;
  }
  const client = createQueryClient(() => {});
  render(<QueryClientProvider client={client}><Canvas><Content /></Canvas></QueryClientProvider>);
  const input = await screen.findByRole<HTMLInputElement>("textbox", { name: "Browse draft" });
  await waitFor(() => expect(document.querySelector(`[data-visual-instance="${initial.visuals[0]!.id}"]`)).not.toBeNull(), { interval: 1 });
  fireEvent.change(input, { target: { value: "unfinished draft" } });
  input.focus();
  const parent = input.closest(".visual-tile")?.parentNode;
  for (const [revision, placement] of [[4, "floating"], [5, "main"]] as const) {
    await act(async () => client.setQueryData(["workspace", workspace.id], { ...initial, revision, visuals: [{ ...initial.visuals[0]!, placement }] }));
    await waitFor(() => expect(input.closest(".visual-tile")?.classList.contains(`visual-tile-${placement}`)).toBe(true), { interval: 1 });
    expect(screen.getByRole("textbox", { name: "Browse draft" })).toBe(input);
    expect(input.value).toBe("unfinished draft");
    expect(input.closest(".visual-tile")?.parentNode).toBe(parent);
    expect(document.activeElement).toBe(input);
    expect(input.closest(".visual-tile")?.classList.contains(`visual-tile-${placement}`)).toBe(true);
  }
  expect(mounted).toHaveBeenCalledTimes(1);
});

test("the canvas does not read at mount, nor on a timer, nor when its tab comes back: the shell's stream carries it", async () => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  browseOnly();
  vi.mocked(createDefaultWorkspace).mockResolvedValue(workspace);
  vi.mocked(getWorkspace).mockResolvedValue(workspace);
  render(<QueryClientProvider client={createQueryClient(() => {})}><Canvas><div>Browse content</div></Canvas></QueryClientProvider>);
  await screen.findByRole("group", { name: "Workbench visuals" });
  await waitFor(() => expect(document.querySelector("[data-visual-instance]")).not.toBeNull(), { interval: 1 });
  await act(async () => { vi.advanceTimersByTime(10_000); });
  act(() => {
    focusManager.setFocused(false);
    focusManager.setFocused(true);
  });
  expect(getWorkspace).not.toHaveBeenCalled();
});

const CATALOG_WITH_NOTE = {
  default_visual: "trax.browse",
  visuals: [
    { type: "trax.browse", version: 1, title: "Browse", description: "Browse records", default_size: "wide" as const, requires: [], parameter_schema: {} },
    { type: "test.note", version: 1, title: "Note", description: "A visual no renderer draws", default_size: "compact" as const, requires: [], parameter_schema: {} },
  ],
};

/** The page and a note, or the note alone once the page is closed. */
function pageAndNote(withPage: boolean): WorkspaceState {
  const note = { id: "note-instance", type: "test.note", version: 1, placement: "side" as const, record_id: null, params: {} };
  return { ...workspace, visuals: withPage ? [{ ...workspace.visuals[0]!, placement: "main" }, note] : [note] };
}

test("the page has the close control the other tiles have, and closing it hides the page", async () => {
  vi.mocked(getVisualCatalog).mockResolvedValue(CATALOG_WITH_NOTE);
  const withNote = pageAndNote(true);
  vi.mocked(createDefaultWorkspace).mockResolvedValue(withNote);
  vi.mocked(getWorkspace).mockResolvedValue(withNote);
  vi.mocked(applyWorkspaceOperation).mockResolvedValue({ ...withNote, revision: 4, visuals: [withNote.visuals[1]!] });
  render(<QueryClientProvider client={createQueryClient(() => {})}><Canvas><div>Browse content</div></Canvas></QueryClientProvider>);
  await screen.findAllByTitle("Dismiss visual");
  const tiles = [...document.querySelectorAll<HTMLElement>(".visual-tile[data-visual-instance]")];
  expect(tiles).toHaveLength(2);
  expect(tiles.map((tile) => within(tile).queryAllByTitle("Dismiss visual").length)).toEqual([1, 1]);
  expect(screen.getByRole("checkbox", { name: /Browse/ })).toHaveProperty("disabled", false);
  expect(screen.getByRole("checkbox", { name: /Browse/ })).toHaveProperty("checked", true);

  fireEvent.click(within(document.querySelector<HTMLElement>(BROWSE_TILE)!).getByTitle("Dismiss visual"));

  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledWith(
    withNote.id, withNote.revision, { kind: "hide", instance_id: withNote.visuals[0]!.id }, expect.any(String)), { interval: 1 });
  await waitFor(() => expect(document.querySelector(".visual-main-strip")).toBeNull(), { interval: 1 });
  // The press on the close button is not also a click on the bar, which would focus a tile that is gone.
  expect(applyWorkspaceOperation).toHaveBeenCalledTimes(1);
});

test("with the page closed, the Workbench section offers it unchecked and checking it shows the page in the main strip", async () => {
  vi.mocked(getVisualCatalog).mockResolvedValue(CATALOG_WITH_NOTE);
  const closed = pageAndNote(false);
  vi.mocked(createDefaultWorkspace).mockResolvedValue(closed);
  vi.mocked(getWorkspace).mockResolvedValue(closed);
  vi.mocked(applyWorkspaceOperation).mockResolvedValue({ ...pageAndNote(true), revision: 4 });
  render(<QueryClientProvider client={createQueryClient(() => {})}><Canvas><div>Browse content</div></Canvas></QueryClientProvider>);
  const option = await screen.findByRole("checkbox", { name: /Browse/ });
  await waitFor(() => expect(option).toHaveProperty("disabled", false), { interval: 1 });
  expect(option).toHaveProperty("checked", false);
  expect(screen.queryByText("Browse content")).toBeNull();

  fireEvent.click(option);

  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledWith(
    closed.id, closed.revision, { kind: "show", visual_type: "trax.browse", placement: "main" }, expect.any(String)), { interval: 1 });
  expect(await screen.findByText("Browse content")).toBeTruthy();
});

/** Move the address as the router does: a replaced entry and one hashchange. */
function userNavigates(hash: string) {
  act(() => {
    history.replaceState(null, "", hash);
    dispatchEvent(new HashChangeEvent("hashchange"));
  });
}

test("with the page closed, the user's own navigation shows it again; with it open, a navigation writes nothing", async () => {
  vi.mocked(getVisualCatalog).mockResolvedValue(CATALOG_WITH_NOTE);
  const closed = pageAndNote(false);
  vi.mocked(createDefaultWorkspace).mockResolvedValue(closed);
  vi.mocked(getWorkspace).mockResolvedValue(closed);
  vi.mocked(applyWorkspaceOperation).mockResolvedValue({ ...pageAndNote(true), revision: 4 });
  render(<QueryClientProvider client={createQueryClient(() => {})}><Canvas><div>Browse content</div></Canvas></QueryClientProvider>);
  await waitFor(() => expect(document.querySelector('[data-visual-type="test.note"]')).not.toBeNull(), { interval: 1 });
  expect(applyWorkspaceOperation).not.toHaveBeenCalled();

  userNavigates("#/list/Issue");

  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledWith(
    closed.id, closed.revision, { kind: "show", visual_type: "trax.browse", placement: "main" }, expect.any(String)), { interval: 1 });
  await waitFor(() => expect(document.querySelector(".visual-main-strip")).not.toBeNull(), { interval: 1 });
  userNavigates("#/list/Belief");
  expect(applyWorkspaceOperation).toHaveBeenCalledTimes(1);
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
  // The controls, outside the page, are still there.
  expect(screen.getByRole("group", { name: "Workbench visuals" })).toBeTruthy();
  history.replaceState(null, "", "#/list/Issue");
  view.rerender(<QueryClientProvider client={client}><Canvas><div>Moved on</div></Canvas></QueryClientProvider>);
  expect(await screen.findByText("Moved on")).toBeTruthy();
  vi.restoreAllMocks();
});

test("the controls stand in the sidebar's slot, and the canvas has no toolbar, Canvas label or Configure button of its own", async () => {
  browseOnly();
  vi.mocked(createDefaultWorkspace).mockResolvedValue(workspace);
  vi.mocked(getWorkspace).mockResolvedValue(workspace);
  const view = render(<QueryClientProvider client={createQueryClient(() => {})}><Canvas><div>Browse content</div></Canvas></QueryClientProvider>);
  const controls = await screen.findByRole("group", { name: "Workbench visuals" });
  expect(view.container.querySelector(".workbench-slot")!.contains(controls)).toBe(true);
  expect(view.container.querySelector(".visual-canvas")!.contains(controls)).toBe(false);
  expect(await within(controls).findByRole("checkbox", { name: /Browse/ })).toBeTruthy();
  expect(document.querySelector(".visual-toolbar")).toBeNull();
  expect(screen.queryByRole("toolbar", { name: /controls/ })).toBeNull();
  expect(screen.queryByRole("button", { name: "Configure" })).toBeNull();
  expect(screen.queryByText("Canvas")).toBeNull();
});

test("a canvas with no sidebar to stand in draws its page all the same", async () => {
  browseOnly();
  vi.mocked(createDefaultWorkspace).mockResolvedValue(workspace);
  vi.mocked(getWorkspace).mockResolvedValue(workspace);
  renderBare(<QueryClientProvider client={createQueryClient(() => {})}><ProfileContext value={PROFILE}><Canvas><div>Browse content</div></Canvas></ProfileContext></QueryClientProvider>);
  expect(await screen.findByText("Browse content")).toBeTruthy();
  expect(screen.queryByRole("group", { name: "Workbench visuals" })).toBeNull();
});

test("older reads and replay receipts cannot replace a newer canvas revision", () => {
  const older = { ...workspace, revision: 2 };
  expect(newerWorkspace(workspace, older)).toBe(workspace);
  expect(newerWorkspace(older, workspace)).toBe(workspace);
  expect(newerWorkspace(undefined, workspace)).toBe(workspace);
});

test.each([false, true])("queued operations keep their originating station while another writes independently (replaced=%s)", async (replaced) => {
  vi.mocked(getVisualCatalog).mockResolvedValue(CATALOG_WITH_NOTE);
  const a = pageAndNote(true);
  const b: WorkspaceState = { ...a, id: "station-b", revision: 100, visuals: a.visuals.map((visual) => ({ ...visual, id: `${visual.id}-b` })) };
  const client = createQueryClient(() => {});
  client.setQueryData(["workspace", a.id], a);
  client.setQueryData(["workspace", b.id], b);
  setStation(a.id);
  let release: (state: WorkspaceState) => void = () => { throw new Error("No held operation"); };
  vi.mocked(applyWorkspaceOperation).mockImplementationOnce(() => new Promise<WorkspaceState>((resolve) => { release = resolve; }))
    .mockImplementation(async (id, revision) => ({ ...(id === a.id ? a : b), revision: revision + 1 }));
  function Content() {
    const actions = useWorkspaceActions();
    return <>{[a, b].map((state) => <button key={state.id} onClick={() => actions?.operate({ kind: "focus", instance_id: state.visuals[0]!.id })}>Focus {state.id}</button>)}</>;
  }
  render(<QueryClientProvider client={client}><Canvas><Content /></Canvas></QueryClientProvider>);
  await screen.findAllByTitle("Dismiss visual");
  fireEvent.click(within(document.querySelector<HTMLElement>(NOTE_TILE)!).getByTitle("Dismiss visual"));
  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledTimes(1), { interval: 1 });
  fireEvent.click(screen.getByRole("button", { name: `Focus ${a.id}` }));
  await act(async () => { setStation(b.id); history.replaceState(null, "", "#/b"); });
  await waitFor(() => expect(document.querySelector('[data-visual-instance="note-instance-b"]')).not.toBeNull(), { interval: 1 });
  fireEvent.click(screen.getByRole("button", { name: `Focus ${b.id}` }));
  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledTimes(2), { interval: 1 });
  expect(vi.mocked(applyWorkspaceOperation).mock.calls[1]!.slice(0, 2)).toEqual([b.id, 100]);
  // A 404 read may cache the default replacement under the unavailable station's query key.
  if (replaced) client.setQueryData(["workspace", a.id], b);
  await act(async () => release({ ...a, revision: 4, visuals: [a.visuals[0]!] }));
  if (replaced) {
    await waitFor(() => expect(client.getMutationCache().getAll().some((mutation) => mutation.state.status === "pending")).toBe(false), { interval: 1 });
    expect(applyWorkspaceOperation).toHaveBeenCalledTimes(2);
    expect(document.querySelector(".visual-write-error")).toBeNull();
    expect(getWorkspace).not.toHaveBeenCalled();
  } else {
    await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledTimes(3), { interval: 1 });
    expect(vi.mocked(applyWorkspaceOperation).mock.calls[2]!.slice(0, 3)).toEqual([a.id, 4, { kind: "focus", instance_id: a.visuals[0]!.id }]);
    expect(client.getQueryData<WorkspaceState>(["workspace", a.id])?.revision).toBe(5);
  }
  expect(client.getQueryData<WorkspaceState>(["workspace", b.id])?.revision).toBe(101);
  expect(document.querySelector('[data-visual-instance="note-instance-b"]')).not.toBeNull();
});

test("a single floating visual keeps its bar", async () => {
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
  expect(await screen.findByRole("button", { name: "Move Browse" })).toBeTruthy();
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
  fireEvent(window, new Event("resize"));
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
  fireEvent(window, new Event("resize"));

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

test("the Workbench section resolves the current ref route before showing a record-required visual", async () => {
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

  await screen.findByRole("group", { name: "Workbench visuals" });
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
      { type: "trax.timeline", version: 1, title: "Timeline", description: "A record's history",
        default_size: "compact", requires: [], parameter_schema: {} },
    ],
  });
  const initial = { ...workspace, revision: 1, visuals: [{ ...workspace.visuals[0]!, placement: "main" as const }] };
  const changed = { ...initial, revision: 2, visuals: [...initial.visuals, {
    id: "440b0d37-76f3-478c-acb5-20a40cecc10d", type: "trax.timeline", version: 1,
    placement: "side" as const, record_id: null, params: {},
  }] };
  vi.mocked(createDefaultWorkspace).mockResolvedValue(initial);
  let releaseRead: (value: WorkspaceState) => void = () => { throw new Error("Read not pending"); };
  vi.mocked(getWorkspace).mockImplementation(() => new Promise((resolve) => { releaseRead = resolve; }));
  // The canvas reads only after a refused write; that read is the delayed one.
  vi.mocked(applyWorkspaceOperation).mockRejectedValueOnce(new Error("refused")).mockResolvedValue(changed);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Canvas><div>Browse content</div></Canvas></QueryClientProvider>);
  await screen.findByRole("group", { name: "Workbench visuals" });
  fireEvent.click(await screen.findByRole("checkbox", { name: /Timeline/ }));
  await waitFor(() => expect(getWorkspace).toHaveBeenCalledTimes(1), { interval: 1 });
  fireEvent.click(await screen.findByRole("checkbox", { name: /Timeline/ }));
  await waitFor(() => expect(client.getQueryData<WorkspaceState>(["workspace", workspace.id])?.revision).toBe(2), { interval: 1 });
  await act(async () => { releaseRead(initial); });
  expect(client.getQueryData<WorkspaceState>(["workspace", workspace.id])?.revision).toBe(2);
  expect((screen.getByRole("checkbox", { name: /Timeline/ }) as HTMLInputElement).checked).toBe(true);
});

const NOTE_TILE = '[data-visual-instance="note-instance"]';

/** The page with a visual no renderer draws docked beside it, which has the bar every docked tile has. */
async function mountNote() {
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [
      { type: "trax.browse", version: 1, title: "Browse", description: "Browse records",
        default_size: "wide", requires: [], parameter_schema: {} },
      { type: "test.note", version: 1, title: "Note", description: "A visual no renderer draws",
        default_size: "compact", requires: [], parameter_schema: {} },
    ],
  });
  const state: WorkspaceState = { ...workspace, visuals: [
    { ...workspace.visuals[0]!, placement: "main" },
    { id: "note-instance", type: "test.note", version: 1, placement: "side", record_id: null, params: {} }] };
  vi.mocked(createDefaultWorkspace).mockResolvedValue(state);
  vi.mocked(getWorkspace).mockResolvedValue(state);
  vi.mocked(applyWorkspaceOperation).mockResolvedValue({ ...state, revision: 4, focused_instance: "note-instance" });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Canvas><div>Browse content</div></Canvas></QueryClientProvider>);
  await waitFor(() => expect(document.querySelector(NOTE_TILE)).not.toBeNull(), { interval: 1 });
  return { state, tile: document.querySelector<HTMLElement>(NOTE_TILE)! };
}

test("a workbench of three columns hides Chat, and gives it back when a column goes", async () => {
  setChatHidden(false);
  const { state } = await mountNote();
  expect(currentChatColumn().hidden).toBe(false);
  const three: WorkspaceState = { ...state, revision: 4, visuals: [...state.visuals,
    { id: "left-note", type: "test.note", version: 1, placement: "left", record_id: null, params: {} }] };
  vi.mocked(applyWorkspaceOperation).mockResolvedValue(three);
  fireEvent.click(within(document.querySelector<HTMLElement>(NOTE_TILE)!).getByRole("button", { name: "Dock Note at the left" }));
  await waitFor(() => expect(currentChatColumn().hidden).toBe(true), { interval: 1 });
  vi.mocked(applyWorkspaceOperation).mockResolvedValue({ ...state, revision: 5 });
  fireEvent.click(within(document.querySelector<HTMLElement>(".visual-side-column-left")!).getByTitle("Dismiss visual"));
  await waitFor(() => expect(currentChatColumn().hidden).toBe(false), { interval: 1 });
});

test("a dock button moves a tile to that side of the page at once; the page itself has none", async () => {
  const { state, tile } = await mountNote();
  const moved: WorkspaceState = { ...state, revision: 4, visuals: [state.visuals[0]!, { ...state.visuals[1]!, placement: "left" }] };
  vi.mocked(applyWorkspaceOperation).mockResolvedValue(moved);
  expect(within(document.querySelector<HTMLElement>(BROWSE_TILE)!).queryByRole("button", { name: /^Dock / })).toBeNull();

  fireEvent.click(within(tile).getByRole("button", { name: "Dock Note at the left" }));
  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledWith(
    state.id, state.revision, { kind: "place", instance_id: "note-instance", placement: "left" }, expect.any(String)), { interval: 1 });
  await waitFor(() => expect(document.querySelector(`.visual-side-column-left > ${NOTE_TILE}`)).not.toBeNull(), { interval: 1 });
  // The left column stands before the page's strip, and its button has nowhere left to go.
  const stage = document.querySelector(".visual-stage")!;
  expect([...stage.children].filter((child) => !child.classList.contains("visual-tile-browse")).map((child) => child.className)).toEqual([
    "visual-side-column visual-side-column-left", "visual-column-edge", "visual-main-strip", "visual-snap", "visual-drag-ghost"]);
  await waitFor(() => expect(screen.getByRole<HTMLButtonElement>("button", { name: "Dock Note at the left" }).disabled).toBe(true), { interval: 1 });
});

/** Tell jsdom, which lays nothing out, that the stage is 800 px wide and starts at the window's corner. */
function sizeStage() {
  const stage = document.querySelector<HTMLElement>(".visual-stage")!;
  Element.prototype.setPointerCapture = vi.fn();
  Object.defineProperty(stage, "clientWidth", { configurable: true, value: 800 });
  Object.defineProperty(stage, "clientHeight", { configurable: true, value: 600 });
  vi.spyOn(stage, "getBoundingClientRect").mockReturnValue({ left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600 } as DOMRect);
  fireEvent(window, new Event("resize"));
  return stage;
}

test("a press on a docked tile's bar that only slips focuses it: the tile leaves its place when dragged farther", async () => {
  const { state, tile } = await mountNote();
  sizeStage();
  const title = within(tile.querySelector<HTMLElement>(".visual-tile-toolbar")!).getByText("Note");
  const ghost = document.querySelector<HTMLElement>(".visual-drag-ghost")!;
  fireEvent.pointerDown(title, { pointerId: 1, clientX: 600, clientY: 16 });
  fireEvent.pointerMove(title, { pointerId: 1, clientX: 600 - TEAR_SLOP, clientY: 16 });
  expect(ghost.style.display).not.toBe("block");
  fireEvent.pointerUp(title, { pointerId: 1, clientX: 600 - TEAR_SLOP, clientY: 16 });
  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledWith(
    state.id, state.revision, { kind: "focus", instance_id: "note-instance" }, expect.any(String)), { interval: 1 });
});

test("a docked tile dragged to the other edge docks there, and to the top edge joins the page's strip", async () => {
  const { state, tile } = await mountNote();
  sizeStage();
  const snap = document.querySelector<HTMLElement>(".visual-snap")!;
  const title = () => within(tile.querySelector<HTMLElement>(".visual-tile-toolbar")!).getByText("Note");
  const drop = (pointerId: number, x: number, y: number) => {
    fireEvent.pointerDown(title(), { pointerId, clientX: 600, clientY: 16 });
    fireEvent.pointerMove(title(), { pointerId, clientX: x, clientY: y });
    const zone = snap.dataset.zone;
    fireEvent.pointerUp(title(), { pointerId, clientX: x, clientY: y });
    return zone;
  };
  expect(drop(1, 20, 300)).toBe("left");
  expect(drop(2, 400, 4)).toBe("main");
  // Back at the edge it stands at already: nothing to write.
  expect(drop(3, 790, 300)).toBe("side");
  await waitFor(() => expect(vi.mocked(applyWorkspaceOperation).mock.calls.map((call) => call[2])).toEqual(
    ["left", "main"].map((placement) => ({ kind: "place", instance_id: "note-instance", placement }))), { interval: 1 });
  expect(vi.mocked(applyWorkspaceOperation).mock.calls[0]!.slice(0, 2)).toEqual([state.id, state.revision]);
});

test("a docked visual floats by its placement when dragged out", async () => {
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [
      { type: "trax.browse", version: 1, title: "Browse", description: "Browse records",
        default_size: "wide", requires: [], parameter_schema: {} },
      { type: "test.note", version: 1, title: "Note", description: "A visual no renderer draws",
        default_size: "compact", requires: [], parameter_schema: {} },
    ],
  });
  const state: WorkspaceState = { ...workspace, visuals: [
    { ...workspace.visuals[0]!, placement: "main" },
    { id: "note-instance", type: "test.note", version: 1, placement: "side", record_id: null, params: {} }] };
  vi.mocked(createDefaultWorkspace).mockResolvedValue(state);
  vi.mocked(getWorkspace).mockResolvedValue(state);
  vi.mocked(applyWorkspaceOperation).mockResolvedValue({ ...state, revision: 4 });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Canvas><div>Browse content</div></Canvas></QueryClientProvider>);
  const title = await screen.findByText("Note", { selector: ".visual-tile-toolbar span" });
  sizeStage();
  fireEvent.pointerDown(title, { pointerId: 1, clientX: 600, clientY: 16 });
  fireEvent.pointerMove(title, { pointerId: 1, clientX: 400, clientY: 300 });
  fireEvent.pointerUp(title, { pointerId: 1, clientX: 400, clientY: 300 });
  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledWith(
    state.id, state.revision, { kind: "place", instance_id: "note-instance", placement: "floating" }, expect.any(String)), { interval: 1 });
  // Kept as fractions of the 800 by 600 stage.
  expect(written().tiles?.["test.note"]?.place).toEqual({ left: 0.425, top: 0.4733 });
  // The page stays the page: its bar focuses, and does not drag.
  expect(document.querySelector(BROWSE_TILE + " .visual-tile-toolbar-draggable")).toBeNull();
});

test("the edge between a column and the page resizes the column, by the pointer or the arrow keys, and the width is kept", async () => {
  const { tile } = await mountNote();
  sizeStage();
  const column = tile.parentElement!;
  const edge = screen.getByRole("separator", { name: "Resize the right column" });
  Object.defineProperty(column, "offsetWidth", { configurable: true, get: () => Number.parseInt(column.style.getPropertyValue("--column-width") || "360", 10) });
  fireEvent.pointerDown(edge, { pointerId: 1, clientX: 440, clientY: 300 });
  fireEvent.pointerMove(edge, { pointerId: 1, clientX: 380, clientY: 300 });
  expect(column.style.getPropertyValue("--column-width")).toBe("420px");
  // Never narrower than a column's least, never more than seven tenths of the stage.
  fireEvent.pointerMove(edge, { pointerId: 1, clientX: 790, clientY: 300 });
  expect(column.style.getPropertyValue("--column-width")).toBe("260px");
  fireEvent.pointerMove(edge, { pointerId: 1, clientX: 10, clientY: 300 });
  expect(column.style.getPropertyValue("--column-width")).toBe("560px");
  fireEvent.pointerMove(edge, { pointerId: 1, clientX: 400, clientY: 300 });
  fireEvent.pointerUp(edge, { pointerId: 1, clientX: 400, clientY: 300 });
  expect(written().columns).toEqual({ side: 0.5 });
  await waitFor(() => expect(edge.getAttribute("aria-valuenow")).toBe("400"), { interval: 1 });

  // The arrow points where the edge goes: left widens the right column.
  fireEvent.keyDown(edge, { key: "ArrowLeft" });
  await waitFor(() => expect(column.style.getPropertyValue("--column-width")).toBe("416px"), { interval: 1 });
  fireEvent.keyDown(edge, { key: "ArrowRight", shiftKey: true });
  await waitFor(() => expect(column.style.getPropertyValue("--column-width")).toBe("368px"), { interval: 1 });
  expect(written().columns).toEqual({ side: 0.46 });
});

test("two tiles that share the main strip trade room by the divider between them, and their shares are kept", async () => {
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [
      { type: "trax.browse", version: 1, title: "Browse", description: "Browse records",
        default_size: "wide", requires: [], parameter_schema: {} },
      { type: "test.note", version: 1, title: "Note", description: "A visual no renderer draws",
        default_size: "wide", requires: [], parameter_schema: {} },
    ],
  });
  const state: WorkspaceState = { ...workspace, visuals: [
    { ...workspace.visuals[0]!, placement: "main" },
    { id: "note-instance", type: "test.note", version: 1, placement: "main", record_id: null, params: {} }] };
  vi.mocked(createDefaultWorkspace).mockResolvedValue(state);
  vi.mocked(getWorkspace).mockResolvedValue(state);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Canvas><div>Browse content</div></Canvas></QueryClientProvider>);
  await screen.findByText("Note", { selector: ".visual-tile-toolbar span" });
  Element.prototype.setPointerCapture = vi.fn();
  const [page, note] = [...document.querySelectorAll<HTMLElement>(".visual-main-strip > .visual-tile")];
  page!.getBoundingClientRect = () => ({ width: 500, height: 600 }) as DOMRect;
  note!.getBoundingClientRect = () => ({ width: 300, height: 600 }) as DOMRect;
  // One divider, between the two: it is the strip's second child.
  const divider = screen.getByRole("separator");
  expect([...page!.parentElement!.children].map((child) => child.className.split(" ")[0])).toEqual(["visual-tile", "visual-divider", "visual-tile"]);
  const shares = () => [page!, note!].map((tile) => tile.style.getPropertyValue("--tile-share"));

  fireEvent.pointerDown(divider, { pointerId: 1, clientX: 500, clientY: 300 });
  fireEvent.pointerMove(divider, { pointerId: 1, clientX: 560, clientY: 300 });
  // While it is dragged, each tile's share is its width: what the page gains the other gives.
  expect(shares()).toEqual(["560", "240"]);
  expect(page!.style.getPropertyValue("--tile-basis")).toBe("0px");
  fireEvent.pointerUp(divider, { pointerId: 1, clientX: 560, clientY: 300 });
  // Kept as shares of the strip, an even one being 1.
  expect(written().share).toEqual({ "trax.browse": 1.4, "test.note": 0.6 });
  await waitFor(() => expect(shares()).toEqual(["1.4", "0.6"]), { interval: 1 });
});

test("a floating window resizes by any edge or corner, inside the stage, and keeps the size it was left", async () => {
  const { tile, bar } = await mountFloating();
  drawnAt(tile, 100, 50);
  const box = () => [tile.style.left, tile.style.top, tile.style.width, tile.style.height];
  const drag = (edge: string, pointerId: number, dx: number, dy: number) => {
    const handle = tile.querySelector<HTMLElement>(`.visual-window-edge-${edge}`)!;
    fireEvent.pointerDown(handle, { pointerId, clientX: 200, clientY: 200 });
    fireEvent.pointerMove(handle, { pointerId, clientX: 200 + dx, clientY: 200 + dy });
    const during = box();
    fireEvent.pointerUp(handle, { pointerId, clientX: 200 + dx, clientY: 200 + dy });
    return during;
  };
  expect(tile.querySelectorAll(".visual-window-edge")).toHaveLength(8);
  // The tile is 300 by 400 at (100, 50) in a stage of 800 by 600.
  expect(drag("e", 1, 60, 99)).toEqual(["100px", "50px", "360px", "400px"]);
  expect(written().floating).toEqual({ "trax.browse": { width: 0.45, height: 0.6667 } });
  await waitFor(() => expect(tile.style.width).toBe("360px"), { interval: 1 });
  expect(storedTile().place).toEqual({ left: 100, top: 50 });
  // jsdom still measures 300 by 400: each drag starts from that box.
  expect(drag("nw", 2, -40, -20)).toEqual(["60px", "30px", "340px", "420px"]);
  expect(drag("s", 3, 0, 5000)).toEqual(["100px", "50px", "300px", "550px"]);
  expect(drag("w", 4, 5000, 0)).toEqual(["120px", "50px", "280px", "400px"]);

  // Folded to its bar, a window has no size to give.
  fireEvent.click(within(bar).getByRole("button", { name: "Collapse Browse" }));
  expect(tile.querySelectorAll(".visual-window-edge")).toHaveLength(0);
});

test("a canvas write waits for a pending one and uses its new revision", async () => {
  const timeline: WorkspaceState["visuals"][number] = {
    id: "timeline-instance", type: "trax.timeline", version: 1, placement: "side",
    record_id: null, params: {},
  };
  const afterShow: WorkspaceState = { ...workspace, revision: 4, visuals: [...workspace.visuals, timeline] };
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [
      { type: "trax.browse", version: 1, title: "Browse", description: "Browse records",
        default_size: "wide", requires: [], parameter_schema: {} },
      { type: "trax.timeline", version: 1, title: "Timeline", description: "A record's history",
        default_size: "compact", requires: [], parameter_schema: {} },
    ],
  });
  vi.mocked(createDefaultWorkspace).mockResolvedValue(workspace);
  vi.mocked(getWorkspace).mockResolvedValue(workspace);
  let finishOperation: (state: WorkspaceState) => void = () => { throw new Error("Write not pending"); };
  vi.mocked(applyWorkspaceOperation).mockImplementationOnce(() => new Promise((resolve) => { finishOperation = resolve; }))
    .mockResolvedValue({ ...workspace, revision: 5 });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Canvas><div>Record detail</div></Canvas></QueryClientProvider>);

  await screen.findByRole("group", { name: "Workbench visuals" });
  const option = await screen.findByRole("checkbox", { name: /Timeline/ });
  fireEvent.click(option);
  // A mock's call changes nothing on screen, so only the interval checks again.
  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledOnce(), { interval: 1 });
  expect((option as HTMLInputElement).disabled).toBe(true);

  await act(async () => { finishOperation(afterShow); });
  await waitFor(() => expect((option as HTMLInputElement).disabled).toBe(false));
  fireEvent.click(option);
  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenLastCalledWith(
    workspace.id, 4, { kind: "hide", instance_id: "timeline-instance" }, expect.any(String)), { interval: 1 });
  expect(client.getQueryData<WorkspaceState>(["workspace", workspace.id])?.revision).toBe(5);
});

test("a rejected canvas write reports how to recover", async () => {
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [
      { type: "trax.browse", version: 1, title: "Browse", description: "Browse records",
        default_size: "wide", requires: [], parameter_schema: {} },
      { type: "trax.timeline", version: 1, title: "Timeline", description: "A record's history",
        default_size: "compact", requires: [], parameter_schema: {} },
    ],
  });
  vi.mocked(createDefaultWorkspace).mockResolvedValue(workspace);
  vi.mocked(getWorkspace).mockResolvedValue(workspace);
  vi.mocked(applyWorkspaceOperation).mockRejectedValue(new Error("revision conflict"));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Canvas><div>Record detail</div></Canvas></QueryClientProvider>);

  await screen.findByRole("group", { name: "Workbench visuals" });
  fireEvent.click(await screen.findByRole("checkbox", { name: /Timeline/ }));

  expect((await screen.findByRole("alert")).textContent).toContain("Its revision may have changed; try again.");
});

test("the Workbench section opens Artifact content from its canonical link", async () => {
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

  expect(await screen.findByRole("textbox", { name: "Artifact link" })).toBeTruthy();
  fireEvent.change(screen.getByRole("textbox", { name: "Artifact link" }),
    { target: { value: `https://trackinizer.example.com/app/#/lookup/${artifactId}` } });
  fireEvent.click(screen.getByRole("button", { name: "Open Artifact" }));
  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledWith(
    workspace.id, workspace.revision,
    { kind: "show", visual_type: "trax.artifact", placement: "main", record_id: artifactId },
    expect.any(String),
  ));
});

const BROWSE_TILE = `[data-visual-instance="${workspace.visuals[0]!.id}"]`;

function storedTile() {
  return rememberedTile(written(), "trax.browse", { width: 800, height: 600 });
}

/** The floating page of `state`, 800 x 600 stage and a 300 x 400 tile, with its top bar. */
async function mountFloating(state: WorkspaceState = workspace) {
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [{ type: "trax.browse", version: 1, title: "Browse", description: "Browse records",
      default_size: "wide", requires: [], parameter_schema: {} }],
  });
  vi.mocked(createDefaultWorkspace).mockResolvedValue(state);
  vi.mocked(getWorkspace).mockResolvedValue(state);
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
  fireEvent(window, new Event("resize"));
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
  expect(written()).toEqual({});
});

test("a press on the title bar that does not move focuses the tile, and neither moves nor collapses it", async () => {
  const { tile, title } = await mountFloating();
  vi.mocked(applyWorkspaceOperation).mockResolvedValue({ ...workspace, revision: 4, focused_instance: workspace.visuals[0]!.id });
  fireEvent.pointerDown(title, { pointerId: 1, clientX: 100, clientY: 100 });
  fireEvent.pointerMove(title, { pointerId: 1, clientX: 100 + CLICK_SLOP, clientY: 100 });
  fireEvent.pointerUp(title, { pointerId: 1, clientX: 100 + CLICK_SLOP, clientY: 100 });
  // A click moves nothing, even by the pixels the hand wandered.
  expect(tile.style.transform).toBe("");
  expect(tile.style.left).toBe("");
  expect(tile.classList.contains("visual-tile-collapsed")).toBe(false);
  expect(written()).toEqual({});
  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledWith(
    workspace.id, workspace.revision, { kind: "focus", instance_id: workspace.visuals[0]!.id }, expect.any(String)), { interval: 1 });
  await waitFor(() => expect(tile.classList.contains("visual-tile-focused")).toBe(true), { interval: 1 });

  // Focused already, a second click writes nothing.
  fireEvent.pointerDown(title, { pointerId: 2, clientX: 100, clientY: 100 });
  fireEvent.pointerUp(title, { pointerId: 2, clientX: 100, clientY: 100 });
  expect(applyWorkspaceOperation).toHaveBeenCalledOnce();
});

test("a floating tile dropped at an edge of the stage docks there, and the stage shows where while it is dragged", async () => {
  const { tile, title } = await mountFloating();
  vi.mocked(applyWorkspaceOperation).mockResolvedValue({ ...workspace, revision: 4 });
  const snap = document.querySelector<HTMLElement>(".visual-snap")!;
  const drop = (pointerId: number, x: number, y: number) => {
    fireEvent.pointerDown(title, { pointerId, clientX: 300, clientY: 200 });
    fireEvent.pointerMove(title, { pointerId, clientX: x, clientY: y });
    const zone = snap.dataset.zone;
    fireEvent.pointerUp(title, { pointerId, clientX: x, clientY: y });
    return zone;
  };
  // In the open the tile only moves; its bar's move handle never docks it.
  expect(drop(1, 400, 300)).toBe("");
  expect(applyWorkspaceOperation).not.toHaveBeenCalled();
  expect(drop(2, SNAP_SIDE, 300)).toBe("left");
  expect(drop(3, 800 - SNAP_SIDE, 300)).toBe("side");
  expect(drop(4, 400, SNAP_TOP)).toBe("main");
  expect(snap.dataset.zone).toBe("");
  expect(tile.style.transform).toBe("");
  await waitFor(() => expect(vi.mocked(applyWorkspaceOperation).mock.calls.map((call) => call[2])).toEqual(
    ["left", "side", "main"].map((placement) => ({ kind: "place", instance_id: workspace.visuals[0]!.id, placement }))), { interval: 1 });
});

test("a collapsed tile can be dragged by its bar, and stays collapsed", async () => {
  const { tile, bar, title } = await mountFloating();
  fireEvent.click(within(bar).getByRole("button", { name: "Collapse Browse" }));
  fireEvent.pointerDown(title, { pointerId: 2, clientX: 100, clientY: 100 });
  fireEvent.pointerMove(title, { pointerId: 2, clientX: 160, clientY: 100 });
  fireEvent.pointerUp(title, { pointerId: 2, clientX: 160, clientY: 100 });
  expect(tile.classList.contains("visual-tile-collapsed")).toBe(true);
  expect(storedTile()).toEqual({ collapsed: true, place: { left: 60, top: 0 } });
});

test("the bar's buttons keep their own presses: pressing one starts no drag and focuses nothing", async () => {
  const { tile, bar } = await mountFloating();
  const fold = within(bar).getByRole("button", { name: "Collapse Browse" });
  fireEvent.pointerDown(fold, { pointerId: 1, clientX: 100, clientY: 100 });
  fireEvent.pointerMove(fold, { pointerId: 1, clientX: 180, clientY: 100 });
  fireEvent.pointerUp(fold, { pointerId: 1, clientX: 180, clientY: 100 });
  expect(tile.style.transform).toBe("");
  expect(tile.classList.contains("visual-tile-collapsed")).toBe(false);
  expect(applyWorkspaceOperation).not.toHaveBeenCalled();
  expect(written()).toEqual({});
  fireEvent.click(fold);
  expect(tile.classList.contains("visual-tile-collapsed")).toBe(true);
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

/** The floating page with `layout` as its station holds it. */
function withLayout(layout: Layout): WorkspaceState {
  return { ...workspace, layout };
}

test("a place and a collapse stored for the station show on the next load, on any browser", async () => {
  const { tile } = await mountFloating(withLayout({ tiles: { "trax.browse": { collapsed: true, place: { left: 0.15, top: 0.1167 } } } }));
  expect(tile.classList.contains("visual-tile-collapsed")).toBe(true);
  expect([tile.style.left, tile.style.top]).toEqual(["120px", "70px"]);
});

test("a stored place the stage has since shrunk below is held inside it", async () => {
  const { tile } = await mountFloating(withLayout({ tiles: { "trax.browse": { place: { left: 0.875, top: 0.9833 } } } }));
  fireEvent(window, new Event("resize"));
  expect([tile.style.left, tile.style.top]).toEqual(["500px", "200px"]);
});

test("a station that refuses the layout still lets the tile move and collapse", async () => {
  vi.mocked(setStationLayout).mockRejectedValue(new Error("offline"));
  const { tile, title } = await mountFloating();
  fireEvent.pointerDown(title, { pointerId: 1, clientX: 100, clientY: 100 });
  fireEvent.pointerMove(title, { pointerId: 1, clientX: 110, clientY: 120 });
  fireEvent.pointerUp(title, { pointerId: 1, clientX: 110, clientY: 120 });
  expect([tile.style.left, tile.style.top]).toEqual(["10px", "20px"]);
  fireEvent.click(within(tile).getByRole("button", { name: "Collapse Browse" }));
  expect(tile.classList.contains("visual-tile-collapsed")).toBe(true);
  expect(written().tiles?.["trax.browse"]?.collapsed).toBe(true);
});

test("a stage narrower than the stored place shows the tile pulled inside and writes nothing", async () => {
  const { tile } = await mountFloating(withLayout({ tiles: { "trax.browse": { place: { left: 0.875, top: 0.1667 } } }, columns: { side: 0.9 } }));
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  const stage = document.querySelector<HTMLElement>(".visual-stage")!;
  Object.defineProperty(stage, "clientWidth", { configurable: true, value: 400 });
  fireEvent(window, new Event("resize"));
  expect(tile.style.left).toBe("100px");
  await act(async () => { await vi.advanceTimersByTimeAsync(LAYOUT_EVERY_MS + 100); });
  expect(setStationLayout).not.toHaveBeenCalled();
});

/** The browser state an older build left in this browser for the user. */
function legacyBrowserState(tiles: object) {
  const key = `trackinizer.v2.${location.origin}.${PROFILE.email}`;
  localStorage.setItem(key, JSON.stringify({ version: 2, stars: [], views: [], aliases: [], people: {}, read: { boundary: null, marks: [] }, ui: { collapsed: [], lens: null, tiles } }));
  return key;
}

test("legacy layout survives a refused migration and is forgotten only after a successful reload saves it", async () => {
  const tiles = { "trax.browse": { collapsed: true, place: { left: 160, top: 60 } } };
  const key = legacyBrowserState(tiles);
  const sizes = { side: 400, chat: 512, floating: {}, share: {} };
  localStorage.setItem("trackinizer.v2.canvas.sizes", JSON.stringify(sizes));
  let reject: (error: Error) => void = () => { throw new Error("No held migration"); };
  vi.mocked(setStationLayout).mockImplementationOnce(() => new Promise<void>((_resolve, refuse) => { reject = refuse; }));
  await mountFloating();
  expect(JSON.parse(localStorage.getItem("trackinizer.v2.canvas.sizes")!)).toEqual(sizes);
  expect(JSON.parse(localStorage.getItem(key)!).ui.tiles).toEqual(tiles);
  await act(async () => reject(new Error("offline")));
  expect(JSON.parse(localStorage.getItem("trackinizer.v2.canvas.sizes")!)).toEqual(sizes);
  expect(JSON.parse(localStorage.getItem(key)!).ui.tiles).toEqual(tiles);
  cleanup();
  forgetLayouts();
  setStation(null);
  await mountFloating();
  await waitFor(() => expect(JSON.parse(localStorage.getItem("trackinizer.v2.canvas.sizes")!)).toEqual({ chat: 512 }), { interval: 1 });
  expect(JSON.parse(localStorage.getItem(key)!).ui.tiles).toEqual({});
  expect(setStationLayout).toHaveBeenCalledTimes(2);
});

test("the first station a browser opens takes the places and sizes it kept before, as fractions, and the browser forgets them", async () => {
  const key = legacyBrowserState({ "trax.browse": { collapsed: true, place: { left: 160, top: 60 } } });
  localStorage.setItem("trackinizer.v2.canvas.sizes", JSON.stringify({ side: 400, chat: 512, floating: { "trax.browse": { width: 400, height: 300 } }, share: {} }));
  await mountFloating();
  await waitFor(() => expect(written()).toEqual({
    columns: { side: 0.5 },
    floating: { "trax.browse": { width: 0.5, height: 0.5 } },
    tiles: { "trax.browse": { collapsed: true, place: { left: 0.2, top: 0.1 } } },
  }), { interval: 1 });
  // Chat's width stays for Chat's own migration.
  expect(JSON.parse(localStorage.getItem("trackinizer.v2.canvas.sizes")!)).toEqual({ chat: 512 });
  expect(JSON.parse(localStorage.getItem(key)!).ui.tiles).toEqual({});
});

test("a station that already has a layout keeps it: what the browser kept is forgotten, not written over", async () => {
  const key = legacyBrowserState({ "trax.browse": { collapsed: true, place: { left: 160, top: 60 } } });
  localStorage.setItem("trackinizer.v2.canvas.sizes", JSON.stringify({ side: 400, floating: {}, share: {} }));
  await mountFloating(withLayout({ columns: { side: 0.3 } }));
  await waitFor(() => expect(localStorage.getItem("trackinizer.v2.canvas.sizes")).toBeNull(), { interval: 1 });
  expect(JSON.parse(localStorage.getItem(key)!).ui.tiles).toEqual({});
  expect(written()).toEqual({});
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
  // Kept as fractions of the shrunk stage, 600 by 500.
  expect(rememberedTile(written(), "trax.browse", { width: 600, height: 500 }).place).toEqual({ left: 290, top: 100 });
});

test("an arrow key moves the tile from where it is drawn, not from the place it was last dragged to", async () => {
  const { handle } = await dragThenShrink(500, 200, { left: 300, top: 100 });
  fireEvent.keyDown(handle, { key: "ArrowUp" });
  expect(rememberedTile(written(), "trax.browse", { width: 600, height: 500 }).place).toEqual({ left: 300, top: 84 });
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
  const { tile } = await mountFloating(withLayout({ tiles: { "trax.browse": { place: { left: 0.625, top: 0.1667 } } } }));
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
  const { tile } = await mountFloating(withLayout({ tiles: { "trax.browse": { place: { left: 0.875, top: 0.1667 } } } }));
  fireEvent(window, new Event("resize"));
  expect(tile.style.left).toBe("700px");
});

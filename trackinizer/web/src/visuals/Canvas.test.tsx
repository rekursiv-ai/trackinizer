import { focusManager, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render as renderBare, screen, waitFor, within } from "@testing-library/react";
import { useEffect, useLayoutEffect, useRef, type ReactElement, type ReactNode } from "react";
import { afterEach, beforeAll, expect, test, vi } from "vitest";
import { getVisualCatalog } from "../api/visuals";
import {
  applyWorkspaceOperation,
  createDefaultWorkspace,
  getWorkspace,
  type WorkspaceState,
} from "../api/workspaces";
import { CommandRegistry, CommandRegistryContext } from "../commands/registry";
import { ProfileContext } from "../app/boot";
import { WorkbenchSlot } from "../app/workbenchSlot";
import { PROFILE } from "../detail/testing";
import { createQueryClient } from "../app/queryClient";
import { newerWorkspace } from "../stations/cache";
import { setStation } from "../stations/store";
import { setStationLayout } from "../api/stations";
import { CaptureContext, CaptureRegistry } from "../canvasCapture/stageRegistry";
import { Canvas } from "./Canvas";
import { forgetHistories } from "./canvasHistory";
import { forgetLayouts } from "./stationLayout";
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

beforeAll(async () => {
  await Promise.all([import("./Timeline"), import("./ContextGraph")]);
});


/** The canvas offers its pen's Escape as a command, which needs a registry above it. */
const commands = new CommandRegistry();
/** The canvas shows the signed-in user's state, so every render has a profile above it; its controls stand in the sidebar's slot, beside it. */
function render(ui: ReactElement) {
  return renderBare(ui, { wrapper: ({ children }: { children: ReactNode }) => <CommandRegistryContext value={commands}><ProfileContext value={PROFILE}>{children}<WorkbenchSlot /></ProfileContext></CommandRegistryContext> });
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

test("the canvas offers its stage, with the boxes standing on it, to an agent's request for a picture, until it unmounts", async () => {
  browseOnly();
  const boxed: WorkspaceState = { ...workspace, marks: [
    { id: "b1", seq: 1, author: "jp@example.com", at: "2026-10-17T10:00:00Z", points: [{ x: 0.1, y: 0.1 }, { x: 0.4, y: 0.4 }] },
    { id: "b2", seq: 2, author: "agent", at: "2026-10-17T10:01:00Z", points: [{ x: 0.5, y: 0.5 }, { x: 0.9, y: 0.9 }] },
  ] };
  vi.mocked(createDefaultWorkspace).mockResolvedValue(boxed);
  const registry = new CaptureRegistry();
  const view = render(<QueryClientProvider client={createQueryClient(() => {})}><CaptureContext value={registry}><Canvas><p>Page</p></Canvas></CaptureContext></QueryClientProvider>);
  await screen.findByText("Page");
  await waitFor(() => expect(registry.get()?.stage.querySelectorAll("[data-mark]")).toHaveLength(2), { interval: 1 });

  const source = registry.get()!;
  expect(source.stage).toBe(document.querySelector(".visual-stage"));
  expect([...source.stage.querySelectorAll<SVGElement>("[data-mark]")].map((box) => [box.dataset.mark, box.dataset.author, box.style.left, box.style.width])).toEqual([
    ["b1", "person", "25%", "30%"],
    ["b2", "agent", "70%", "40%"],
  ]);
  view.unmount();
  expect(registry.get()).toBeNull();
});

test("a box pinned to a tile stands in that tile's body, below its bar, and one pinned to a tile that is not there does not show", async () => {
  browseOnly();
  const [browse] = workspace.visuals;
  const mark = (id: string, tile: string) => ({ id, seq: 1, author: "jp@example.com", at: "2026-10-17T10:00:00Z", points: [{ x: 0.1, y: 0.1 }, { x: 0.4, y: 0.4 }], tile });
  vi.mocked(createDefaultWorkspace).mockResolvedValue({ ...workspace, marks: [mark("here", browse!.id), mark("gone", "00000000-0000-4000-8000-000000000000")] });
  render(<QueryClientProvider client={createQueryClient(() => {})}><Canvas><p>Page</p></Canvas></QueryClientProvider>);
  await screen.findByText("Page");

  const tile = document.querySelector(`[data-visual-instance="${browse!.id}"]`)!;
  const body = tile.querySelector(":scope > .visual-tile-body")!;
  await waitFor(() => expect(body.querySelector('[data-mark="here"]')).not.toBeNull());
  expect(document.querySelector('[data-mark="gone"]')).toBeNull();
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

/** Move the address as the router does: a replaced entry and one hashchange. */
function userNavigates(hash: string) {
  act(() => {
    history.replaceState(null, "", hash);
    dispatchEvent(new HashChangeEvent("hashchange"));
  });
}

test("with the page closed, a navigation of the person's own opens it once and draws the route", async () => {
  vi.mocked(getVisualCatalog).mockResolvedValue(CATALOG_WITH_NOTE);
  const closed = pageAndNote(false);
  vi.mocked(createDefaultWorkspace).mockResolvedValue(closed);
  vi.mocked(getWorkspace).mockResolvedValue(closed);
  vi.mocked(applyWorkspaceOperation).mockResolvedValue({ ...pageAndNote(true), revision: 4 });
  render(<QueryClientProvider client={createQueryClient(() => {})}><Canvas><div>Browse content</div></Canvas></QueryClientProvider>);
  await waitFor(() => expect(document.querySelector('[data-visual-type="test.note"]')).not.toBeNull(), { interval: 1 });
  // Opening on the station writes nothing: it shows what it was left showing.
  expect(applyWorkspaceOperation).not.toHaveBeenCalled();
  expect(screen.queryByText("Browse content")).toBeNull();

  // The rail's places are routes, and a route draws only in the page pane, so going to
  // one has to open that pane and put it on the stage. It used to change the address and
  // leave the note standing: a reader clicked Graph and got no graph.
  for (const route of ["#/list/Issue", "#/list/Belief"]) {
    userNavigates(route);
    await new Promise((settle) => setTimeout(settle, 5));
  }
  await screen.findByText("Browse content");
  // Once, though. The second navigation has a page to draw in already.
  expect(applyWorkspaceOperation).toHaveBeenCalledTimes(1);
  expect(vi.mocked(applyWorkspaceOperation).mock.calls[0]![2])
    .toEqual({ kind: "show", visual_type: "trax.browse", placement: "main" });
});

/** The types the stage is drawing, in order: a tile tucked behind another is on the stage but out of sight. */
function shownTypes(): string[] {
  return [...document.querySelectorAll<HTMLElement>(".visual-stage [data-visual-type]")]
    .filter((tile) => tile.style.display !== "none")
    .map((tile) => tile.dataset.visualType!);
}

test("a visual put up while the reader is on a route takes the stage back from the page", async () => {
  vi.mocked(getVisualCatalog).mockResolvedValue(CATALOG_WITH_NOTE);
  // The station opens with the page alone; the note arrives after, the way an agent's
  // `show` or the ask bar's answer does — as an instance that was not there before.
  const paged = { ...workspace, visuals: [{ ...workspace.visuals[0]!, placement: "main" as const }] };
  vi.mocked(createDefaultWorkspace).mockResolvedValue(paged);
  vi.mocked(getWorkspace).mockResolvedValue(paged);
  vi.mocked(applyWorkspaceOperation).mockResolvedValue({ ...pageAndNote(true), revision: 4 });
  function PutUpANote() {
    const actions = useWorkspaceActions();
    return <button onClick={() => actions?.operate({ kind: "show", visual_type: "test.note" })}>Put up a note</button>;
  }
  render(<QueryClientProvider client={createQueryClient(() => {})}><Canvas><PutUpANote /></Canvas></QueryClientProvider>);
  const put = await screen.findByRole("button", { name: "Put up a note" });
  userNavigates("#/list/Issue");
  await new Promise((settle) => setTimeout(settle, 5));
  expect(shownTypes()).toEqual(["trax.browse"]);

  fireEvent.click(put);
  await waitFor(() => expect(document.querySelector(NOTE_TILE)).not.toBeNull(), { interval: 1 });
  // The page stays mounted — a route must keep its scroll and its iframes — but the note
  // is what the stage draws. A reply composed behind the page would never be read.
  expect(shownTypes()).toEqual(["test.note"]);
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
  renderBare(<QueryClientProvider client={createQueryClient(() => {})}><CommandRegistryContext value={commands}><ProfileContext value={PROFILE}><Canvas><div>Browse content</div></Canvas></ProfileContext></CommandRegistryContext></QueryClientProvider>);
  expect(await screen.findByText("Browse content")).toBeTruthy();
  expect(screen.queryByRole("group", { name: "Workbench visuals" })).toBeNull();
});

/** The one tile the stage draws, addressed by the instance the fixture shows. It used
 *  to sit among the floating-tile helpers; those tests went with the window manager and
 *  took the constant with them, while this selector is still how a test reaches the
 *  surface on the stage. */
const NOTE_TILE = '[data-visual-instance="note-instance"]';

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
  await waitFor(() => expect(document.querySelector(NOTE_TILE)).not.toBeNull(), { interval: 1 });
  // The first write, held in flight by the mock above so the second can overtake it. It
  // used to be a click on the tile's own dismiss button; the surface wears no chrome now,
  // so the harness's own button sends it. What is under test is the queue, not the control.
  fireEvent.click(screen.getByRole("button", { name: `Focus ${a.id}`, hidden: true }));
  await waitFor(() => expect(applyWorkspaceOperation).toHaveBeenCalledTimes(1), { interval: 1 });
    // `hidden: true`: this harness renders its buttons as the Canvas's children, which is
  // the PAGE, and the stage tucks the page away while a visual stands on it. Hidden by
  // `display: none` is the right behaviour — a page a reader cannot see should not be
  // in the accessibility tree either — so the query has to opt into it rather than the
  // canvas keeping an invisible page readable for a test's convenience.
fireEvent.click(screen.getByRole("button", { name: `Focus ${a.id}`, hidden: true }));
  await act(async () => { setStation(b.id); history.replaceState(null, "", "#/b"); });
  await waitFor(() => expect(document.querySelector('[data-visual-instance="note-instance-b"]')).not.toBeNull(), { interval: 1 });
  fireEvent.click(screen.getByRole("button", { name: `Focus ${b.id}`, hidden: true }));
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




/** Put `state` on the station and draw the canvas, with `children` as the page. */
function show(state: WorkspaceState, _unused?: undefined, children: ReactNode = <div>Browse content</div>) {
  vi.mocked(createDefaultWorkspace).mockResolvedValue(state);
  vi.mocked(getWorkspace).mockResolvedValue(state);
  render(<QueryClientProvider client={createQueryClient(() => {})}><Canvas>{children}</Canvas></QueryClientProvider>);
}

// THE STAGE DRAWS ONE SURFACE. These replace the thirty-one tests that covered the
// window manager the canvas used to be — docking, floating, dividers, snap zones,
// per-tile placement memory. That machinery is gone, and with it every decision it
// asked a reader to make before they could read anything. What is left has to be held
// just as tightly, because "one surface" is only simple until a second one appears.

test("two visuals on a station draw one surface: the newest stands, the other is not on the stage", async () => {
  vi.mocked(getVisualCatalog).mockResolvedValue(CATALOG_WITH_NOTE);
  const second = { id: "note-two", type: "test.note", version: 1, placement: "side" as const, record_id: null, params: {} };
  const state = pageAndNote(true);
  show({ ...state, visuals: [...state.visuals, second] });
  await waitFor(() => expect(document.querySelector(".visual-tile")).not.toBeNull(), { interval: 1 });
  // Both are on the station; only the last one shown is drawn.
  expect(document.querySelector('[data-visual-instance="note-two"]')).not.toBeNull();
  expect(document.querySelector(NOTE_TILE)).toBeNull();
});

test("the page is kept mounted under whatever stands on it, so a route never loses its state", async () => {
  vi.mocked(getVisualCatalog).mockResolvedValue(CATALOG_WITH_NOTE);
  show(pageAndNote(true), undefined, <input aria-label="draft" defaultValue="" />);
  await waitFor(() => expect(document.querySelector(NOTE_TILE)).not.toBeNull(), { interval: 1 });
  // The page is tucked away behind the note, not unmounted: its DOM — and so its
  // scroll, its selection and anything typed into it — survives. Removing it instead
  // of hiding it is the bug this holds shut; an earlier cut of the one-surface change
  // replaced the page with an empty placeholder and threw the route away.
  const field = screen.getByLabelText("draft", { selector: "input" }) as HTMLInputElement;
  fireEvent.change(field, { target: { value: "half a sentence" } });
  expect((screen.getByLabelText("draft", { selector: "input" }) as HTMLInputElement).value).toBe("half a sentence");
});

test("a surface on the stage offers no chrome of its own, and none of the window manager's", async () => {
  vi.mocked(getVisualCatalog).mockResolvedValue(CATALOG_WITH_NOTE);
  show(pageAndNote(true));
  await waitFor(() => expect(document.querySelector(NOTE_TILE)).not.toBeNull(), { interval: 1 });
  const tile = document.querySelector<HTMLElement>(NOTE_TILE)!;
  // The tile wears nothing: the Canvas tab above is the header, and a visual is put away
  // from the workbench's toggles. Dock, fold, move and the mobile expand went with the
  // placements they served, and the tile's own dismiss went with the tab that replaced it —
  // there is nowhere else for a surface to go, and nothing on it to go there with.
  expect(within(tile).queryAllByRole("button")).toEqual([]);
  expect(document.querySelector(".visual-snap")).toBeNull();
  expect(document.querySelector(".visual-divider")).toBeNull();
  expect(document.querySelector(".visual-side-column")).toBeNull();
  expect(document.querySelector(".visual-window-edge")).toBeNull();
});

test("the page toggle puts the visual down so the rail's routes draw, and picks it back up", async () => {
  // Graph, Files, Runs and Evidence are routes, and a route draws only in the page pane.
  // A composition covers that pane and the control that moved one aside was removed, so
  // every rail click changed the address and nothing on screen. Nothing is written to the
  // station either way: the composition is still there to come back to.
  history.replaceState(null, "", "#/activity");
  show(pageAndNote(true));
  expect(await screen.findByRole("button", { name: "Show the page" })).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "Show the page" }));
  expect(await screen.findByRole("button", { name: "Back to the canvas" })).toBeTruthy();
  expect(screen.getByText("Browse content")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "Back to the canvas" }));
  expect(await screen.findByRole("button", { name: "Show the page" })).toBeTruthy();
  expect(vi.mocked(applyWorkspaceOperation)).not.toHaveBeenCalled();
});

import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render as renderBare, screen, waitFor, within } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { afterEach, beforeAll, beforeEach, expect, test, vi } from "vitest";
import { listStations, setStationLayout } from "../api/stations";
import { getVisualCatalog } from "../api/visuals";
import { applyWorkspaceOperation, createDefaultWorkspace, getWorkspace, type WorkspaceOperation, type WorkspaceState } from "../api/workspaces";
import { ProfileContext } from "../app/boot";
import { createQueryClient } from "../app/queryClient";
import { WorkbenchSlot } from "../app/workbenchSlot";
import { PROFILE } from "../detail/testing";
import { DEFAULT_PREFERENCES, receivePreferences } from "../preferences";
import { setStation } from "../stations/store";
import { Canvas } from "./Canvas";
import { forgetHistories, observe, settle, step } from "./canvasHistory";
import { snapshotOf } from "./canvasSnapshot";
import { forgetLayouts } from "./stationLayout";
import { CanvasBar } from "./CanvasBar";

vi.mock("../api/visuals", () => ({ getVisualCatalog: vi.fn() }));
vi.mock("../api/stations", () => ({ setStationLayout: vi.fn().mockResolvedValue(undefined), listStations: vi.fn().mockResolvedValue([]) }));
vi.mock("../api/workspaces", () => ({ createDefaultWorkspace: vi.fn(), getWorkspace: vi.fn(), applyWorkspaceOperation: vi.fn() }));
vi.mock("../api/detail", async (original) => ({
  ...(await original<typeof import("../api/detail")>()),
  getDetail: vi.fn(async (id: string) => ({ self: { kind: "Issue", seq: id === "r1" ? 1 : 2, title: id === "r1" ? "One" : "Two" } })),
}));

const STATION = "c5286865-67b6-4bd8-ab51-e06e10c326c5";

/** The page and the timeline of record r1 docked at the left, at revision 3. */
const START: WorkspaceState = {
  id: STATION, name: "unnamed-workstream#1", conversation_id: null, page: null, revision: 3, focused_instance: null,
  visuals: [
    { id: "page", type: "trax.browse", version: 1, placement: "main", record_id: null, params: {} },
    { id: "timeline", type: "trax.timeline", version: 1, placement: "left", record_id: "r1", params: {} },
  ],
};

/** The canvas the fake server holds, which each operation changes as the real one does. */
let served: WorkspaceState = START;
let made = 0;

/** How far the clock is moved ahead of real time, so a person's actions come apart as they do. */
let skew = 0;
let clock: { mockRestore(): void } | null = null;

/** Apply `operation` to the fake server's canvas, as `server/visuals/workspaces.py` does. */
function apply(state: WorkspaceState, operation: WorkspaceOperation): WorkspaceState {
  const next = { ...state, revision: state.revision + 1 };
  switch (operation.kind) {
    case "hide": {
      const hidden = state.visuals.find((visual) => visual.id === operation.instance_id);
      const visuals = state.visuals.filter((visual) => visual !== hidden);
      // Closing the last record brings the page back; closing the page alone does not.
      if (!visuals.length && hidden?.type !== "trax.browse") {
        made += 1;
        return { ...next, visuals: [{ id: `made-${made}`, type: "trax.browse", version: 1, placement: "main", record_id: null, params: {} }] };
      }
      return { ...next, visuals };
    }
    case "place":
      return { ...next, visuals: state.visuals.map((visual) => (visual.id === operation.instance_id ? { ...visual, placement: operation.placement } : visual)) };
    case "show": {
      const shown = { type: operation.visual_type, placement: operation.placement ?? "main", record_id: operation.record_id ?? null, params: operation.params ?? {} };
      const existing = state.visuals.find((visual) => visual.type === operation.visual_type
        && (!operation.beside || visual.record_id === (operation.record_id ?? null)));
      if (existing) return { ...next, visuals: state.visuals.map((visual) => (visual === existing ? { ...visual, ...shown } : visual)) };
      made += 1;
      return { ...next, visuals: [...state.visuals, { id: `made-${made}`, version: 1, ...shown }] };
    }
    case "focus":
      return { ...next, focused_instance: operation.instance_id };
    default:
      return next;
  }
}

beforeAll(() => import("./Timeline"));

beforeEach(() => {
  served = START;
  made = 0;
  skew = 0;
  const now = Date.now.bind(Date);
  clock = vi.spyOn(Date, "now").mockImplementation(() => now() + skew);
  history.replaceState(null, "", "#/list/Issue");
  vi.mocked(getVisualCatalog).mockResolvedValue({
    default_visual: "trax.browse",
    visuals: [
      { type: "trax.browse", version: 1, title: "Browse", description: "Browse records", default_size: "wide", requires: [], parameter_schema: {} },
      { type: "trax.timeline", version: 1, title: "Lineage and timeline", description: "A record's lineage", default_size: "compact", requires: ["record"], parameter_schema: {} },
    ],
  });
  vi.mocked(createDefaultWorkspace).mockImplementation(async () => served);
  vi.mocked(getWorkspace).mockImplementation(async () => served);
  vi.mocked(applyWorkspaceOperation).mockImplementation(async (_id, _revision, operation) => {
    served = apply(served, operation);
    return served;
  });
});

afterEach(() => {
  setStation(null);
  forgetLayouts();
  forgetHistories();
  clock?.mockRestore();
  receivePreferences(DEFAULT_PREFERENCES);
  cleanup();
  vi.clearAllMocks();
  vi.mocked(setStationLayout).mockResolvedValue(undefined);
  vi.mocked(listStations).mockResolvedValue([]);
  history.replaceState(null, "", "#/");
  sessionStorage.clear();
});

function render(ui: ReactElement) {
  return renderBare(ui, { wrapper: ({ children }: { children: ReactNode }) => <ProfileContext value={PROFILE}>{children}<WorkbenchSlot /></ProfileContext> });
}

/** The canvas, with its bar. */
async function mount() {
  render(<QueryClientProvider client={createQueryClient(() => {})}><Canvas><div>Page</div></Canvas></QueryClientProvider>);
  await waitFor(() => expect(document.querySelector('[data-visual-type="trax.timeline"]')).not.toBeNull(), { interval: 1 });
}

/** A person's next action comes this much later: never part of the step before. */
function later() {
  skew += 1_000;
}

/** The operations the canvas sent, in order. */
function sent(): WorkspaceOperation[] {
  return vi.mocked(applyWorkspaceOperation).mock.calls.map(([, , operation]) => operation);
}

const button = (name: string) => within(screen.getByRole("toolbar", { name: "Canvas" })).getByRole<HTMLButtonElement>("button", { name });


test("page routes form one-step Back and Forward history", () => {
  const key = `${STATION}/1`;
  const issue = snapshotOf(START.visuals, "#/list/Issue")!;
  const activity = snapshotOf(START.visuals, "#/activity")!;
  observe(key, issue, 0);
  observe(key, activity, 1_000);
  expect(step(key, -1)).toEqual(issue);
  settle(key, issue, issue);
  expect(step(key, 1)).toEqual(activity);
});

test("Forward control walks to the next page route", () => {
  const key = `${STATION}/1`;
  const issue = snapshotOf(START.visuals, "#/list/Issue")!;
  const activity = snapshotOf(START.visuals, "#/activity")!;
  observe(key, issue, 0);
  observe(key, activity, 1_000);
  expect(step(key, -1)).toEqual(issue);
  settle(key, issue, issue);
  const forward = vi.fn(() => expect(step(key, 1)).toEqual(activity));
  renderBare(<CanvasBar back={false} forward={true} moving={false}
    home="graph" actions={{ back: vi.fn(), forward, home: vi.fn(), clear: vi.fn() }} />);

  fireEvent.click(button("Forward"));
  expect(forward).toHaveBeenCalledOnce();
});

test("Back restores a visual closed in this tab", async () => {
  history.replaceState(null, "", "#/activity");
  await mount();
  later();
  fireEvent.click(document.querySelector<HTMLElement>('[data-visual-type="trax.timeline"] [title="Dismiss visual"]')!);
  await waitFor(() => expect(document.querySelector('[data-visual-type="trax.timeline"]')).toBeNull(), { interval: 1 });

  fireEvent.click(button("Back"));
  await waitFor(() => expect(document.querySelector('[data-visual-type="trax.timeline"]')).not.toBeNull(), { interval: 1 });
  expect(sent().at(-1)).toEqual({ kind: "show", visual_type: "trax.timeline", placement: "left", record_id: "r1", params: {} });
  expect(location.hash).toBe("#/activity");
});

test("Forward calls the action for the next visual state", () => {
  const key = `${STATION}/1`;
  const opened = snapshotOf(START.visuals, "#/list/Issue")!;
  const closed = snapshotOf([START.visuals[0]!], "#/list/Issue")!;
  observe(key, opened, 0);
  observe(key, closed, 1_000);
  expect(step(key, -1)).toEqual(opened);
  settle(key, opened, opened);
  const forward = vi.fn(() => expect(step(key, 1)).toEqual(closed));
  renderBare(<CanvasBar back={true} forward={true} moving={false}
    home="graph" actions={{ back: vi.fn(), forward, home: vi.fn(), clear: vi.fn() }} />);

  fireEvent.click(button("Forward"));
  expect(forward).toHaveBeenCalledOnce();
});

test("an agent's change to the canvas is a step Back undoes", async () => {
  const client = createQueryClient(() => {});
  render(<QueryClientProvider client={client}><Canvas><div>Page</div></Canvas></QueryClientProvider>);
  await waitFor(() => expect(document.querySelector('[data-visual-type="trax.timeline"]')).not.toBeNull(), { interval: 1 });
  // The live stream brings the agent's change: the timeline floated.
  served = apply(served, { kind: "place", instance_id: "timeline", placement: "floating" });
  act(() => client.setQueryData(["workspace", STATION], served));
  await waitFor(() => expect(button("Back").disabled).toBe(false), { interval: 1 });

  fireEvent.click(button("Back"));

  await waitFor(() => expect(sent()).toEqual([{ kind: "place", instance_id: "timeline", placement: "left" }]), { interval: 1 });
});

test("Home shows the page on the graph and nothing else; Back brings the canvas back", async () => {
  await mount();
  fireEvent.click(button("Home"));
  await waitFor(() => expect(location.hash).toBe("#/graph"), { interval: 1 });
  expect(sent()).toEqual([{ kind: "hide", instance_id: "timeline" }]);
  await waitFor(() => expect(button("Back").disabled).toBe(false), { interval: 1 });

  fireEvent.click(button("Back"));

  await waitFor(() => expect(location.hash).toBe("#/list/Issue"), { interval: 1 });
  expect(served.visuals.map((visual) => visual.type)).toEqual(["trax.browse", "trax.timeline"]);
});

test("Clear canvas closes every visual, leaves the page as the default view, and Back restores the rest", async () => {
  await mount();

  fireEvent.click(button("Clear canvas"));
  const clearDialog = await screen.findByRole("alertdialog", { name: "Clear canvas?" });
  fireEvent.click(within(clearDialog).getByRole("button", { name: "Clear canvas" }));
  await waitFor(() => expect(sent()).toEqual([{ kind: "hide", instance_id: "page" }, { kind: "hide", instance_id: "timeline" }]), { interval: 1 });
  await waitFor(() => expect(document.querySelector('[data-visual-type="trax.browse"]')).not.toBeNull(), { interval: 1 });
  expect(document.querySelector('[data-visual-type="trax.timeline"]')).toBeNull();

  await waitFor(() => expect(button("Back").disabled).toBe(false), { interval: 1 });
  fireEvent.click(button("Back"));
  await waitFor(() => expect(document.querySelector('[data-visual-type="trax.timeline"]')).not.toBeNull(), { interval: 1 });
});

test("with home set to empty, Home closes every visual and the page comes back as the default view", async () => {
  receivePreferences({ ...DEFAULT_PREFERENCES, home: "empty" });
  await mount();
  expect(button("Home").title).toContain("empty canvas");

  fireEvent.click(button("Home"));

  await waitFor(() => expect(document.querySelector('[data-visual-type="trax.timeline"]')).toBeNull(), { interval: 1 });
  expect(document.querySelector('[data-visual-type="trax.browse"]')).not.toBeNull();
  expect(sent()).toEqual([{ kind: "hide", instance_id: "page" }, { kind: "hide", instance_id: "timeline" }]);
  expect(location.hash).toBe("#/list/Issue");
});

test("Clear canvas is danger-styled and Cancel or Escape leave the action alone", async () => {
  const clear = vi.fn();
  renderBare(<CanvasBar back={false} forward={false} moving={false}
    home="graph" actions={{ back: vi.fn(), forward: vi.fn(), home: vi.fn(), clear }} />);

  fireEvent.click(screen.getByRole("button", { name: "Clear canvas" }));
  let dialog = screen.getByRole("alertdialog", { name: "Clear canvas?" });
  expect(within(dialog).getByText("This closes every visual; the page comes back as the default view. What it worked on stays listed, and Back restores what it showed.")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Clear canvas" }).classList.contains("danger")).toBe(true);
  expect(within(dialog).getByRole("button", { name: "Clear canvas" }).classList.contains("danger")).toBe(true);
  fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
  expect(clear).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: "Clear canvas" }));
  dialog = screen.getByRole("alertdialog", { name: "Clear canvas?" });
  fireEvent.keyDown(dialog, { key: "Escape" });
  expect(clear).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: "Clear canvas" }));
  dialog = screen.getByRole("alertdialog", { name: "Clear canvas?" });
  fireEvent.click(within(dialog).getByRole("button", { name: "Clear canvas" }));
  expect(clear).toHaveBeenCalledTimes(1);
});

test("the strip puts the panes first in their order; a closed chip shows beside, a shown chip focuses", async () => {
  served = { ...START, worked: [{ record_id: "r2", visual_type: "trax.timeline" }, { record_id: "r1", visual_type: "trax.timeline" }] };
  await mount();
  const strip = within(screen.getByRole("navigation", { name: "Worked on" }));
  await strip.findByRole("button", { name: /Issue#2 Two$/ });
  const chips = () => strip.getAllByRole("button").map((chip) => [chip.textContent, chip.getAttribute("aria-pressed")]);
  // r2 was worked on last, but r1 is the one on a pane, so it leads.
  expect(chips()).toEqual([["Lineage Issue#1 One", "true"], ["Lineage Issue#2 Two", "false"]]);

  fireEvent.click(strip.getByRole("button", { name: /Issue#2 Two$/ }));
  await waitFor(() => expect(sent()).toEqual([{ kind: "show", visual_type: "trax.timeline", record_id: "r2", beside: true }]), { interval: 1 });
  await waitFor(() => expect(chips()).toEqual([["Lineage Issue#1 One", "true"], ["Lineage Issue#2 Two", "true"]]), { interval: 1 });
  expect(document.querySelectorAll('[data-visual-type="trax.timeline"]')).toHaveLength(2);
  // Each pane's bar names its record beside its view, as the chip does.
  expect([...document.querySelectorAll(".visual-tile-record")].map((name) => name.textContent).toSorted()).toEqual(["Issue#1 One", "Issue#2 Two"]);

  fireEvent.click(strip.getByRole("button", { name: /Issue#1 One$/ }));
  await waitFor(() => expect(sent().at(-1)).toEqual({ kind: "focus", instance_id: "timeline" }), { interval: 1 });
});

test("a canvas showing no record and having worked on none shows no strip", async () => {
  served = { ...START, visuals: [START.visuals[0]!] };
  render(<QueryClientProvider client={createQueryClient(() => {})}><Canvas><div>Page</div></Canvas></QueryClientProvider>);
  await waitFor(() => expect(document.querySelector('[data-visual-type="trax.browse"]')).not.toBeNull(), { interval: 1 });
  expect(screen.queryByRole("navigation", { name: "Worked on" })).toBeNull();
});

test("a pane alone in its strip fills it, whatever share its visual kept, and record panes stand as cards", async () => {
  served = { ...START, visuals: [START.visuals[1]!], layout: { share: { "trax.timeline": 0.6 } } };
  render(<QueryClientProvider client={createQueryClient(() => {})}><Canvas><div>Page</div></Canvas></QueryClientProvider>);
  const tile = await waitFor(() => {
    const found = document.querySelector<HTMLElement>('.visual-tile[data-visual-type="trax.timeline"]');
    expect(found).not.toBeNull();
    return found!;
  }, { interval: 1 });
  expect(tile.style.getPropertyValue("--tile-share")).toBe("");
  expect(document.querySelector(".visual-stage")!.classList.contains("visual-stage-panes")).toBe(true);
});

/** Home, so Back has a step to restore; it hides the timeline and moves the page to the graph. */
async function stepAway() {
  await mount();
  fireEvent.click(button("Home"));
  await waitFor(() => expect(location.hash).toBe("#/graph"), { interval: 1 });
  await waitFor(() => expect(button("Back").disabled).toBe(false), { interval: 1 });
}

test("a station opened while a restore's steps are on their way gets none of them: not its visuals, not its page", async () => {
  await stepAway();
  // Another station, on Papers with the page alone; and the next step to the server held back, as a slow network holds it.
  const other: WorkspaceState = { ...START, id: "0b7c5d2e-1111-4a5b-9c8d-2e3f4a5b6c7d", name: "beta", revision: 9, visuals: [START.visuals[0]!] };
  vi.mocked(getWorkspace).mockImplementation(async (id) => (id === other.id ? other : served));
  let release = () => {};
  const held = new Promise<void>((resolve) => { release = resolve; });
  const plain = vi.mocked(applyWorkspaceOperation).getMockImplementation()!;
  vi.mocked(applyWorkspaceOperation).mockImplementationOnce(async (...args) => { await held; return plain(...args); });

  // Back restores the timeline, its step on its way; then the other station, before the step lands.
  fireEvent.click(button("Back"));
  await waitFor(() => expect(sent()).toHaveLength(2), { interval: 1 });
  act(() => {
    setStation(other.id, "#/list/Paper");
    history.replaceState(null, "", "#/list/Paper");
    dispatchEvent(new HashChangeEvent("hashchange"));
  });
  await act(async () => { release(); await held; });
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });

  // The other station's canvas and page are its own still.
  expect(vi.mocked(applyWorkspaceOperation).mock.calls.filter(([id]) => id === other.id)).toEqual([]);
  expect(location.hash).toBe("#/list/Paper");
  // And the canvas is not left waiting on the step it gave up.
  await waitFor(() => expect(button("Home").disabled).toBe(false), { interval: 1 });
});

test("coming back to a station left while a restore's steps were on their way finishes them", async () => {
  await stepAway();
  const home = served;
  const other: WorkspaceState = { ...START, id: "0b7c5d2e-1111-4a5b-9c8d-2e3f4a5b6c7d", name: "beta", revision: 9, visuals: [START.visuals[0]!] };
  vi.mocked(getWorkspace).mockImplementation(async (id) => (id === other.id ? other : served));
  let release = () => {};
  const held = new Promise<void>((resolve) => { release = resolve; });
  const plain = vi.mocked(applyWorkspaceOperation).getMockImplementation()!;
  // The step goes out, but its answer is lost, as a write cut short by leaving would be: the server never shows the timeline.
  vi.mocked(applyWorkspaceOperation).mockImplementationOnce(async () => { await held; throw new Error("left"); });

  fireEvent.click(button("Back"));
  await waitFor(() => expect(sent()).toHaveLength(2), { interval: 1 });
  act(() => {
    setStation(other.id, "#/list/Paper");
    history.replaceState(null, "", "#/list/Paper");
    dispatchEvent(new HashChangeEvent("hashchange"));
  });
  await act(async () => { release(); await new Promise((resolve) => setTimeout(resolve, 20)); });
  expect(served).toBe(home);
  vi.mocked(applyWorkspaceOperation).mockImplementation(plain);

  // Back to the first station: its timeline and its page come back.
  act(() => setStation(STATION, "#/graph"));
  await waitFor(() => expect(document.querySelector('[data-visual-type="trax.timeline"]')).not.toBeNull(), { interval: 1 });
  await waitFor(() => expect(location.hash).toBe("#/list/Issue"), { interval: 1 });
});

import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render as renderBare, screen, waitFor } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { listStations, setStationLayout } from "../api/stations";
import { getVisualCatalog } from "../api/visuals";
import { applyWorkspaceOperation, createDefaultWorkspace, getWorkspace, type WorkspaceOperation, type WorkspaceState } from "../api/workspaces";
import { ProfileContext } from "../app/boot";
import { createQueryClient } from "../app/queryClient";
import { WorkbenchSlot } from "../app/workbenchSlot";
import { PROFILE } from "../detail/testing";
import { DEFAULT_PREFERENCES, receivePreferences } from "../preferences";
import { requestTab } from "../stations/canvasTabs";
import { currentStation, setStation } from "../stations/store";
import { StationGroups } from "../topbar/StationGroups";
import { Canvas } from "./Canvas";
import { forgetHistories } from "./canvasHistory";
import { flushLayouts, forgetLayouts, type Layout } from "./stationLayout";

vi.mock("../api/visuals", () => ({ getVisualCatalog: vi.fn() }));
vi.mock("../api/stations", () => ({ setStationLayout: vi.fn().mockResolvedValue(undefined), listStations: vi.fn().mockResolvedValue([]) }));
vi.mock("../api/workspaces", () => ({ createDefaultWorkspace: vi.fn(), getWorkspace: vi.fn(), applyWorkspaceOperation: vi.fn() }));

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
    case "hide":
      return { ...next, visuals: state.visuals.filter((visual) => visual.id !== operation.instance_id) };
    case "place":
      return { ...next, visuals: state.visuals.map((visual) => (visual.id === operation.instance_id ? { ...visual, placement: operation.placement } : visual)) };
    case "show": {
      const shown = { type: operation.visual_type, placement: operation.placement ?? "main", record_id: operation.record_id ?? null, params: operation.params ?? {} };
      const existing = state.visuals.find((visual) => visual.type === operation.visual_type);
      if (existing) return { ...next, visuals: state.visuals.map((visual) => (visual === existing ? { ...visual, ...shown } : visual)) };
      made += 1;
      return { ...next, visuals: [...state.visuals, { id: `made-${made}`, version: 1, ...shown }] };
    }
    default:
      return next;
  }
}

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

/** The canvas, and the top bar's station groups, which draw its tabs. */
async function mount() {
  render(<QueryClientProvider client={createQueryClient(() => {})}><StationGroups /><Canvas><div>Page</div></Canvas></QueryClientProvider>);
  await waitFor(() => expect(document.querySelector('[data-visual-type="trax.timeline"]')).not.toBeNull(), { interval: 1 });
}

/** A person's next action comes this much later: never part of the step before. */
function later() {
  skew += 1_000;
}

/** Move the address as a link does, a person's action: a new entry and one hashchange. */
function userNavigates(hash: string) {
  later();
  act(() => {
    history.pushState(null, "", hash);
    dispatchEvent(new HashChangeEvent("hashchange"));
  });
}

/** The operations the canvas sent, in order. */
function sent(): WorkspaceOperation[] {
  return vi.mocked(applyWorkspaceOperation).mock.calls.map(([, , operation]) => operation);
}

/** The layout keys written so far, later writes over earlier ones. */
function written(): Layout {
  flushLayouts();
  return Object.assign({}, ...vi.mocked(setStationLayout).mock.calls.map(([, patch]) => patch)) as Layout;
}

const button = (name: string) => screen.getByRole<HTMLButtonElement>("button", { name });

test("Back and Forward walk the page's moves and the visuals opened and closed, one step each", async () => {
  await mount();
  expect(button("Back").disabled).toBe(true);
  userNavigates("#/activity");
  await waitFor(() => expect(button("Back").disabled).toBe(false), { interval: 1 });
  later();
  fireEvent.click(document.querySelector<HTMLElement>('[data-visual-type="trax.timeline"] [title="Dismiss visual"]')!);
  await waitFor(() => expect(document.querySelector('[data-visual-type="trax.timeline"]')).toBeNull(), { interval: 1 });

  fireEvent.click(button("Back"));
  await waitFor(() => expect(document.querySelector('[data-visual-type="trax.timeline"]')).not.toBeNull(), { interval: 1 });
  expect(sent().at(-1)).toEqual({ kind: "show", visual_type: "trax.timeline", placement: "left", record_id: "r1", params: {} });
  expect(location.hash).toBe("#/activity");

  await waitFor(() => expect(button("Back").disabled).toBe(false), { interval: 1 });
  fireEvent.click(button("Back"));
  await waitFor(() => expect(location.hash).toBe("#/list/Issue"), { interval: 1 });
  await waitFor(() => expect(button("Back").disabled).toBe(true), { interval: 1 });

  fireEvent.click(button("Forward"));
  await waitFor(() => expect(location.hash).toBe("#/activity"), { interval: 1 });
  await waitFor(() => expect(button("Forward").disabled).toBe(false), { interval: 1 });
  fireEvent.click(button("Forward"));
  await waitFor(() => expect(document.querySelector('[data-visual-type="trax.timeline"]')).toBeNull(), { interval: 1 });
  await waitFor(() => expect(button("Forward").disabled).toBe(true), { interval: 1 });
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

test("with home set to empty, Home clears the canvas", async () => {
  receivePreferences({ ...DEFAULT_PREFERENCES, home: "empty" });
  await mount();
  expect(button("Home").title).toContain("empty canvas");

  fireEvent.click(button("Home"));

  await waitFor(() => expect(screen.getByText(/Nothing on this canvas/)).toBeTruthy(), { interval: 1 });
  expect(sent()).toEqual([{ kind: "hide", instance_id: "page" }, { kind: "hide", instance_id: "timeline" }]);
  expect(location.hash).toBe("#/list/Issue");
});

test("a new tab opens home and keeps the tab left; opening that tab again shows it as it was left", async () => {
  await mount();
  fireEvent.click(button("New tab in unnamed-workstream#1"));
  await waitFor(() => expect(location.hash).toBe("#/graph"), { interval: 1 });
  await waitFor(() => expect(document.querySelector('[data-visual-type="trax.timeline"]')).toBeNull(), { interval: 1 });
  const layout = written();
  expect(layout.tabs?.map(({ name, page }) => [name, page])).toEqual([["Tab 1", "#/list/Issue"], ["Tab 2", "#/graph"]]);
  expect(layout.tabs?.[0]?.visuals?.map((visual) => visual.type)).toEqual(["trax.browse", "trax.timeline"]);
  expect(layout.tab).toBe(layout.tabs?.[1]?.id);
  expect(screen.getByRole("button", { name: /^Tab 2/ }).closest(".station-tab")?.getAttribute("aria-current")).toBe("true");
  // A new tab begins its own steps.
  expect(button("Back").disabled).toBe(true);

  await waitFor(() => expect(screen.getByRole<HTMLButtonElement>("button", { name: /^Tab 1/ }).disabled).toBe(false), { interval: 1 });
  fireEvent.click(screen.getByRole("button", { name: /^Tab 1/ }));

  await waitFor(() => expect(location.hash).toBe("#/list/Issue"), { interval: 1 });
  await waitFor(() => expect(document.querySelector('[data-visual-type="trax.timeline"]')).not.toBeNull(), { interval: 1 });
  expect(written().tab).toBe("1");
  expect(written().tabs?.[1]?.page).toBe("#/graph");
});

test("closing the open tab opens its neighbour; a tab renamed in place keeps its name", async () => {
  await mount();
  fireEvent.click(button("New tab in unnamed-workstream#1"));
  await waitFor(() => expect(location.hash).toBe("#/graph"), { interval: 1 });
  await waitFor(() => expect(button("Close Tab 2").disabled).toBe(false), { interval: 1 });

  fireEvent.click(button("Close Tab 2"));

  await waitFor(() => expect(location.hash).toBe("#/list/Issue"), { interval: 1 });
  expect(written().tabs?.map((tab) => tab.name)).toEqual(["Tab 1"]);
  expect(written().tab).toBe("1");
  expect(screen.queryByRole("button", { name: "Close Tab 1" })).toBeNull();

  fireEvent.doubleClick(screen.getByRole("button", { name: /^Tab 1/ }));
  const name = screen.getByRole<HTMLInputElement>("textbox", { name: "Tab name" });
  fireEvent.change(name, { target: { value: "  Review  " } });
  fireEvent.keyDown(name, { key: "Enter" });

  expect(written().tabs?.map((tab) => tab.name)).toEqual(["Review"]);
  expect(screen.getByRole("button", { name: /^Review/ })).toBeTruthy();
});

test("beside Home the bar names the station and the tab on show, which a rename in the top bar follows", async () => {
  await mount();
  const crumbs = screen.getByRole("group", { name: "On show" });
  expect(crumbs.textContent).toBe("unnamed-workstream#1/Tab 1");

  fireEvent.doubleClick(screen.getByRole("button", { name: /^Tab 1/ }));
  const name = screen.getByRole<HTMLInputElement>("textbox", { name: "Tab name" });
  fireEvent.change(name, { target: { value: "Triage" } });
  fireEvent.keyDown(name, { key: "Enter" });

  expect(crumbs.textContent).toBe("unnamed-workstream#1/Triage");
});

test("a tab asked for on arriving at the station opens there, and the pages after it are the station's again", async () => {
  await mount();
  fireEvent.click(button("New tab in unnamed-workstream#1"));
  await waitFor(() => expect(location.hash).toBe("#/graph"), { interval: 1 });
  const second = written().tabs![1]!.id;
  await waitFor(() => expect(screen.getByRole<HTMLButtonElement>("button", { name: "Tab 1" }).disabled).toBe(false), { interval: 1 });
  fireEvent.click(screen.getByRole("button", { name: "Tab 1" }));
  await waitFor(() => expect(location.hash).toBe("#/list/Issue"), { interval: 1 });
  // A switch from another station lands here on its page, then asks for the second tab, as the top bar does.
  act(() => {
    setStation(STATION, "#/list/Issue");
    requestTab({ station: STATION, tab: second });
  });
  await waitFor(() => expect(location.hash).toBe("#/graph"), { interval: 1 });
  expect(screen.getByRole("group", { name: "On show" }).textContent).toBe("unnamed-workstream#1/Tab 2");
  // The landing the address never showed must not hold the page back: what the tab moves to is written.
  expect(currentStation().landing).toBeNull();
});

test("a station opened while a tab's steps are on their way gets none of them: not its visuals, not its page", async () => {
  await mount();
  fireEvent.click(button("New tab in unnamed-workstream#1"));
  await waitFor(() => expect(location.hash).toBe("#/graph"), { interval: 1 });
  await waitFor(() => expect(screen.getByRole<HTMLButtonElement>("button", { name: "Tab 1" }).disabled).toBe(false), { interval: 1 });
  // Another station, on Papers with the page alone; and the next step to the server held back, as a slow network holds it.
  const other: WorkspaceState = { ...START, id: "0b7c5d2e-1111-4a5b-9c8d-2e3f4a5b6c7d", name: "beta", revision: 9, visuals: [START.visuals[0]!] };
  vi.mocked(getWorkspace).mockImplementation(async (id) => (id === other.id ? other : served));
  let release = () => {};
  const held = new Promise<void>((resolve) => { release = resolve; });
  const plain = vi.mocked(applyWorkspaceOperation).getMockImplementation()!;
  vi.mocked(applyWorkspaceOperation).mockImplementationOnce(async (...args) => { await held; return plain(...args); });

  // Tab 1 is opened, its timeline on its way; then the other station, before the step lands.
  fireEvent.click(screen.getByRole("button", { name: "Tab 1" }));
  await waitFor(() => expect(sent()).toHaveLength(2), { interval: 1 });
  act(() => {
    setStation(other.id, "#/list/Paper");
    history.replaceState(null, "", "#/list/Paper");
    dispatchEvent(new HashChangeEvent("hashchange"));
  });
  await waitFor(() => expect(screen.getByRole("group", { name: "On show" }).textContent).toBe("beta/Tab 1"), { interval: 1 });
  await act(async () => { release(); await held; });
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });

  // The other station's canvas and page are its own still.
  expect(vi.mocked(applyWorkspaceOperation).mock.calls.filter(([id]) => id === other.id)).toEqual([]);
  expect(location.hash).toBe("#/list/Paper");
  // And the canvas is not left waiting on the step it gave up: the station's tabs open again.
  await waitFor(() => expect(screen.getByRole<HTMLButtonElement>("button", { name: "New tab in beta" }).disabled).toBe(false), { interval: 1 });
});

test("coming back to a station left while a tab's steps were on their way finishes them, so the tab is whole and keeps what it shows", async () => {
  await mount();
  fireEvent.click(button("New tab in unnamed-workstream#1"));
  await waitFor(() => expect(location.hash).toBe("#/graph"), { interval: 1 });
  await waitFor(() => expect(screen.getByRole<HTMLButtonElement>("button", { name: "Tab 1" }).disabled).toBe(false), { interval: 1 });
  const home = served;
  const other: WorkspaceState = { ...START, id: "0b7c5d2e-1111-4a5b-9c8d-2e3f4a5b6c7d", name: "beta", revision: 9, visuals: [START.visuals[0]!] };
  let showing = STATION;
  vi.mocked(getWorkspace).mockImplementation(async (id) => (id === other.id ? other : served));
  let release = () => {};
  const held = new Promise<void>((resolve) => { release = resolve; });
  const plain = vi.mocked(applyWorkspaceOperation).getMockImplementation()!;
  // The step goes out, but its answer is lost, as a write cut short by leaving would be: the server never shows the timeline.
  vi.mocked(applyWorkspaceOperation).mockImplementationOnce(async () => { await held; throw new Error("left"); });

  fireEvent.click(screen.getByRole("button", { name: "Tab 1" }));
  await waitFor(() => expect(sent()).toHaveLength(2), { interval: 1 });
  act(() => {
    showing = other.id;
    setStation(other.id, "#/list/Paper");
    history.replaceState(null, "", "#/list/Paper");
    dispatchEvent(new HashChangeEvent("hashchange"));
  });
  await waitFor(() => expect(screen.getByRole("group", { name: "On show" }).textContent).toBe("beta/Tab 1"), { interval: 1 });
  await act(async () => { release(); await new Promise((resolve) => setTimeout(resolve, 20)); });
  expect(served).toBe(home);
  vi.mocked(applyWorkspaceOperation).mockImplementation(plain);

  // Back to the first station: Tab 1 is open, and its timeline and its page come back.
  act(() => {
    showing = STATION;
    setStation(STATION, "#/graph");
  });
  await waitFor(() => expect(document.querySelector('[data-visual-type="trax.timeline"]')).not.toBeNull(), { interval: 1 });
  await waitFor(() => expect(location.hash).toBe("#/list/Issue"), { interval: 1 });
  expect(showing).toBe(STATION);
});

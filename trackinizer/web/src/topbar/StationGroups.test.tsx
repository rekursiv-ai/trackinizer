import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { deleteChat } from "../api/chats";
import { ApiError } from "../api/client";
import { createStation, deleteStation, listStations, renameStation, type StationSummary } from "../api/stations";
import { getWorkspace, type WorkspaceState } from "../api/workspaces";
import { setStationMenu } from "../stations/actions";
import { type CanvasTabActions, publishCanvasTabs, requestTab, useRequestedTab } from "../stations/canvasTabs";
import { currentStation, setStation } from "../stations/store";
import { FOLD_AFTER_MS, StationGroups } from "./StationGroups";

vi.mock("../api/stations", () => ({ listStations: vi.fn(), renameStation: vi.fn(), createStation: vi.fn(), deleteStation: vi.fn() }));
vi.mock("../api/workspaces", () => ({ getWorkspace: vi.fn() }));
// Only the chat delete is a mock; the refusal messages read the real codes from this module.
vi.mock("../api/chats", async (importOriginal) => ({ ...(await importOriginal<typeof import("../api/chats")>()), deleteChat: vi.fn() }));

const state = (id: string, name: string): WorkspaceState => ({ id, name, conversation_id: null, page: null, revision: 1, focused_instance: null, visuals: [] });
const summary = (id: string, name: string, created_at: string, tabs: { id: string; name: string }[] = [], tab: string | null = null): StationSummary =>
  ({ id, name, conversation_id: null, page: null, created_at, tabs, tab });
// The server lists the station changed last first; the groups stand in the order the stations were made.
const STATIONS = [
  summary("w3", "beta", "2026-10-03T10:00:00Z", [{ id: "a", name: "Graph" }, { id: "b", name: "Issue#7" }], "a"),
  summary("w1", "unnamed-workstream#1", "2026-10-01T10:00:00Z"),
  summary("w2", "Alpha trial", "2026-10-02T10:00:00Z"),
];

let client: QueryClient;
let actions: CanvasTabActions & { [name in keyof CanvasTabActions]: ReturnType<typeof vi.fn> };

/** The canvas of w1 as it publishes its tabs: Tab 1 open, and Review. */
function publish(station = "w1") {
  publishCanvasTabs({ station, tabs: [{ id: "1", name: "Tab 1" }, { id: "r", name: "Review" }], open: "1", moving: false, canAdd: true, actions });
}

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  actions = { open: vi.fn<(id: string) => void>(), add: vi.fn<() => void>(), close: vi.fn<(id: string) => void>(), rename: vi.fn<(id: string, name: string) => void>() };
  setStation("w1");
  history.replaceState(null, "", "#/list/Issue");
  vi.mocked(listStations).mockResolvedValue(STATIONS);
  vi.mocked(getWorkspace).mockImplementation(async (id) => state(id, STATIONS.find((each) => each.id === id)!.name));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  publishCanvasTabs(null);
  requestTab(null);
  setStationMenu(false);
  setStation(null);
  vi.clearAllMocks();
  history.replaceState(null, "", "#/");
});

async function show() {
  publish();
  render(<QueryClientProvider client={client}><StationGroups /></QueryClientProvider>);
  const strip = await screen.findByRole("navigation", { name: "Stations" });
  await within(strip).findByRole("group", { name: "beta" });
  return strip;
}

const group = (name: string) => screen.getByRole("group", { name });
const title = (name: string) => within(group(name)).getByRole("button", { name: new RegExp(`^${name.replace(/[#]/g, "\\$&")}`) });
const tabs = (name: string) => [...group(name).querySelectorAll(".station-tab-open")].map((tab) => tab.textContent);

test("each station is a group, in the order they were made; the one on show is current, with its canvas's tabs", async () => {
  const strip = await show();
  expect(within(strip).getAllByRole("group").map((each) => each.getAttribute("aria-label"))).toEqual(["unnamed-workstream#1", "Alpha trial", "beta"]);
  expect(title("unnamed-workstream#1").getAttribute("aria-current")).toBe("true");
  expect(title("beta").getAttribute("aria-current")).toBeNull();
  expect(tabs("unnamed-workstream#1")).toEqual(["Tab 1", "Review"]);
  // Another station's tabs are as the list names them; a station with none has the first.
  expect(tabs("beta")).toEqual(["Graph", "Issue#7"]);
  expect(tabs("Alpha trial")).toEqual(["Tab 1"]);
});

test("folded, a group's tabs are inert and its title counts them; the current title names its open tab", async () => {
  await show();
  const tabsOf = (name: string) => group(name).querySelector<HTMLElement>(".station-group-tabs")!;
  expect(tabsOf("beta").hasAttribute("inert")).toBe(true);
  fireEvent.pointerEnter(group("beta"));
  expect(tabsOf("beta").hasAttribute("inert")).toBe(false);
  fireEvent.pointerLeave(screen.getByRole("navigation", { name: "Stations" }));
  expect(title("beta").textContent).toBe("beta2");
  expect(title("unnamed-workstream#1").textContent).toBe("unnamed-workstream#12· Tab 1");
});

test("the pointer resting on a group slides its tabs out, and they fold a moment after it leaves the bar", async () => {
  const strip = await show();
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  fireEvent.pointerEnter(group("beta"));
  expect(group("beta").classList).toContain("is-open");
  expect(title("beta").getAttribute("aria-expanded")).toBe("true");
  // Onto the next group: the first folds at once, the next opens.
  fireEvent.pointerEnter(group("Alpha trial"));
  expect(group("beta").classList).not.toContain("is-open");
  expect(group("Alpha trial").classList).toContain("is-open");
  fireEvent.pointerLeave(strip);
  act(() => { vi.advanceTimersByTime(FOLD_AFTER_MS - 1); });
  expect(group("Alpha trial").classList).toContain("is-open");
  act(() => { vi.advanceTimersByTime(1); });
  expect(group("Alpha trial").classList).not.toContain("is-open");
});

test("a click on the current title keeps its tabs out, and a second folds them; the keyboard in a group opens it too", async () => {
  await show();
  fireEvent.click(title("unnamed-workstream#1"));
  expect(group("unnamed-workstream#1").classList).toContain("is-expanded");
  fireEvent.click(title("unnamed-workstream#1"));
  expect(group("unnamed-workstream#1").classList).not.toContain("is-open");
  act(() => title("beta").focus());
  expect(group("beta").classList).toContain("is-open");
});

test("a click on another title opens its station and keeps its tabs out", async () => {
  await show();
  fireEvent.click(title("beta"));
  await waitFor(() => expect(currentStation().id).toBe("w3"));
  expect(getWorkspace).toHaveBeenCalledWith("w3");
  expect(group("beta").classList).toContain("is-expanded");
});

test("a tab of another station asks its canvas for that tab, then opens the station", async () => {
  function Requested() {
    const asked = useRequestedTab();
    return <output aria-label="Asked">{asked ? `${asked.station}/${asked.tab}` : ""}</output>;
  }
  publish();
  render(<QueryClientProvider client={client}><StationGroups /><Requested /></QueryClientProvider>);
  await screen.findByRole("group", { name: "beta" });
  fireEvent.click(within(group("beta")).getByRole("button", { name: "Issue#7" }));
  expect(screen.getByLabelText("Asked").textContent).toBe("w3/b");
  await waitFor(() => expect(currentStation().id).toBe("w3"));
  expect(actions.open).not.toHaveBeenCalled();
});

test("the open station's tabs open, close, add and rename through its canvas", async () => {
  await show();
  fireEvent.click(title("unnamed-workstream#1"));
  fireEvent.click(within(group("unnamed-workstream#1")).getByRole("button", { name: "Review" }));
  expect(actions.open).toHaveBeenCalledWith("r");
  fireEvent.click(screen.getByRole("button", { name: "Close Review" }));
  expect(actions.close).toHaveBeenCalledWith("r");
  fireEvent.click(screen.getByRole("button", { name: "New tab in unnamed-workstream#1" }));
  expect(actions.add).toHaveBeenCalledTimes(1);
  fireEvent.doubleClick(within(group("unnamed-workstream#1")).getByRole("button", { name: "Review" }));
  const name = screen.getByRole<HTMLInputElement>("textbox", { name: "Tab name" });
  fireEvent.change(name, { target: { value: " Triage " } });
  fireEvent.keyDown(name, { key: "Enter" });
  expect(actions.rename).toHaveBeenCalledWith("r", "Triage");
  // Another station's tabs neither close nor add here: its canvas is not the one on show.
  expect(within(group("beta")).queryByRole("button", { name: "Close Graph" })).toBeNull();
  expect(screen.queryByRole("button", { name: "New tab in beta" })).toBeNull();
});

test("a double click on the current title edits its name; Enter saves, the cache follows, and focus comes back", async () => {
  vi.mocked(renameStation).mockResolvedValue(state("w1", "my work"));
  await show();
  fireEvent.doubleClick(title("unnamed-workstream#1"));
  const name = screen.getByRole<HTMLInputElement>("textbox", { name: "Station name" });
  expect(document.activeElement).toBe(name);
  fireEvent.change(name, { target: { value: "  my work " } });
  fireEvent.keyDown(name, { key: "Enter" });
  await waitFor(() => expect(renameStation).toHaveBeenCalledWith("w1", "my work"));
  await waitFor(() => expect(screen.queryByRole("textbox", { name: "Station name" })).toBeNull());
  expect(renameStation).toHaveBeenCalledTimes(1);
  expect(client.getQueryData<WorkspaceState>(["workspace", "w1"])?.name).toBe("my work");
});

test("Esc, a blank name and an unchanged name leave the name as it was, with no request; Esc stops at the field", async () => {
  await show();
  const outer = vi.fn((event: KeyboardEvent) => event.key);
  document.addEventListener("keydown", outer);
  for (const [value, key] of [["changed", "Escape"], ["   ", "Enter"], ["unnamed-workstream#1", "Enter"]] as const) {
    fireEvent.doubleClick(title("unnamed-workstream#1"));
    const name = screen.getByRole("textbox", { name: "Station name" });
    fireEvent.change(name, { target: { value } });
    fireEvent.keyDown(name, { key });
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "Station name" })).toBeNull());
  }
  expect(renameStation).not.toHaveBeenCalled();
  expect(outer.mock.results.map((result) => result.value)).not.toContain("Escape");
  document.removeEventListener("keydown", outer);
  expect(document.activeElement).toBe(title("unnamed-workstream#1"));
});

test("a name that is taken says so and keeps the field; typing again clears the complaint", async () => {
  vi.mocked(renameStation).mockRejectedValue(new ApiError(409, "That name is taken"));
  await show();
  fireEvent.doubleClick(title("unnamed-workstream#1"));
  const name = screen.getByRole("textbox", { name: "Station name" });
  fireEvent.change(name, { target: { value: "beta" } });
  fireEvent.keyDown(name, { key: "Enter" });
  expect((await screen.findByRole("alert")).textContent).toBe("That name is taken");
  const kept = screen.getByRole("textbox", { name: "Station name" });
  fireEvent.change(kept, { target: { value: "beta 2" } });
  expect(screen.queryByRole("alert")).toBeNull();
});

test("+ Station makes a fresh station and switches to it; one that fails says so and stays", async () => {
  vi.mocked(createStation).mockResolvedValueOnce(state("w4", "unnamed-workstream#2")).mockRejectedValueOnce(new ApiError(500, "boom"));
  await show();
  fireEvent.click(screen.getByRole("button", { name: "New station" }));
  await waitFor(() => expect(currentStation().id).toBe("w4"));
  fireEvent.click(screen.getByRole("button", { name: "New station" }));
  expect((await screen.findByRole("alert")).textContent).toBe("Could not make a station.");
  expect(currentStation().id).toBe("w4");
});

test("a station that fails to open says so, and asks its canvas for no tab", async () => {
  await show();
  vi.mocked(getWorkspace).mockRejectedValue(new ApiError(500, "boom"));
  fireEvent.click(within(group("beta")).getByRole("button", { name: "Issue#7" }));
  expect((await screen.findByRole("alert")).textContent).toBe("Could not open that station.");
  expect(currentStation().id).toBe("w1");
});

test("only the open station's tabs stay out: a group left for another folds, by a click or any other switch", async () => {
  await show();
  fireEvent.click(title("unnamed-workstream#1"));
  expect(group("unnamed-workstream#1").classList).toContain("is-expanded");
  fireEvent.click(title("beta"));
  await waitFor(() => expect(currentStation().id).toBe("w3"));
  await waitFor(() => expect(group("beta").classList).toContain("is-expanded"));
  expect(group("unnamed-workstream#1").classList).not.toContain("is-open");
  // A switch the bar did not make (g ], the palette, a fork) folds the group left too.
  act(() => setStation("w2"));
  await waitFor(() => expect(title("Alpha trial").getAttribute("aria-current")).toBe("true"));
  expect(group("beta").classList).not.toContain("is-open");
  expect(group("Alpha trial").classList).not.toContain("is-open");
});

test("the × on a background workspace deletes its chat and the station, and leaves the shown one", async () => {
  vi.mocked(listStations).mockResolvedValue([
    { ...summary("w3", "beta", "2026-10-03T10:00:00Z", [{ id: "a", name: "Graph" }], "a"), conversation_id: "c3" },
    summary("w1", "unnamed-workstream#1", "2026-10-01T10:00:00Z"),
    summary("w2", "Alpha trial", "2026-10-02T10:00:00Z"),
  ]);
  vi.mocked(deleteChat).mockResolvedValue();
  vi.mocked(deleteStation).mockResolvedValue();
  await show();
  fireEvent.click(within(group("beta")).getByRole("button", { name: "Delete beta" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete" }));
  await waitFor(() => expect(deleteStation).toHaveBeenCalledWith("w3"));
  expect(deleteChat).toHaveBeenCalledWith("c3");
  expect(client.getQueryData<StationSummary[]>(["stations"])?.map((each) => each.id)).toEqual(["w1", "w2"]);
  // Deleting a workspace I was not on leaves me where I was.
  expect(currentStation().id).toBe("w1");
});

test("deleting the shown workspace clears its chat and moves to the one beside it", async () => {
  vi.mocked(listStations).mockResolvedValue([
    summary("w3", "beta", "2026-10-03T10:00:00Z"),
    { ...summary("w1", "unnamed-workstream#1", "2026-10-01T10:00:00Z"), conversation_id: "c1" },
    summary("w2", "Alpha trial", "2026-10-02T10:00:00Z"),
  ]);
  // The shown station's chat comes from its own state, not the list; w1 holds c1.
  vi.mocked(getWorkspace).mockImplementation(async (id) => ({ ...state(id, STATIONS.find((each) => each.id === id)!.name), conversation_id: id === "w1" ? "c1" : null }));
  vi.mocked(deleteChat).mockResolvedValue();
  vi.mocked(deleteStation).mockResolvedValue();
  await show();
  fireEvent.click(within(group("unnamed-workstream#1")).getByRole("button", { name: "Delete unnamed-workstream#1" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete" }));
  await waitFor(() => expect(currentStation().id).toBe("w2"));
  expect(deleteChat).toHaveBeenCalledWith("c1");
  expect(deleteStation).toHaveBeenCalledWith("w1");
});

test("a chat the server refuses to delete stops the workspace delete and says why", async () => {
  vi.mocked(listStations).mockResolvedValue([
    { ...summary("w3", "beta", "2026-10-03T10:00:00Z"), conversation_id: "c3" },
    summary("w1", "unnamed-workstream#1", "2026-10-01T10:00:00Z"),
    summary("w2", "Alpha trial", "2026-10-02T10:00:00Z"),
  ]);
  vi.mocked(deleteChat).mockRejectedValue(new ApiError(403, "403 Forbidden", "chat_has_other_posters"));
  vi.mocked(deleteStation).mockResolvedValue();
  await show();
  fireEvent.click(within(group("beta")).getByRole("button", { name: "Delete beta" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete" }));
  expect((await screen.findByRole("alert")).textContent).toBe("Others have posted in this chat, so only an admin can delete it.");
  expect(deleteStation).not.toHaveBeenCalled();
  expect(client.getQueryData<StationSummary[]>(["stations"])?.map((each) => each.id)).toEqual(["w3", "w1", "w2"]);
});

test("the only workspace stays when deleted; its chat is cleared and the station is not", async () => {
  vi.mocked(listStations).mockResolvedValue([{ ...summary("w1", "unnamed-workstream#1", "2026-10-01T10:00:00Z"), conversation_id: "c1" }]);
  // The shown station's chat comes from its own state, not the list; w1 holds c1.
  vi.mocked(getWorkspace).mockResolvedValue({ ...state("w1", "unnamed-workstream#1"), conversation_id: "c1" });
  vi.mocked(deleteChat).mockResolvedValue();
  vi.mocked(deleteStation).mockResolvedValue();
  publish();
  render(<QueryClientProvider client={client}><StationGroups /></QueryClientProvider>);
  await screen.findByRole("group", { name: "unnamed-workstream#1" });
  fireEvent.click(within(group("unnamed-workstream#1")).getByRole("button", { name: "Delete unnamed-workstream#1" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete" }));
  await waitFor(() => expect(deleteChat).toHaveBeenCalledWith("c1"));
  expect(deleteStation).not.toHaveBeenCalled();
  expect(currentStation().id).toBe("w1");
  expect(client.getQueryData<StationSummary[]>(["stations"])?.map((each) => each.id)).toEqual(["w1"]);
});

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { ApiError } from "../api/client";
import { createStation, listStations, renameStation, type StationSummary } from "../api/stations";
import { getWorkspace, type WorkspaceState } from "../api/workspaces";
import { setStationMenu } from "./actions";
import { currentStation, setStation } from "./store";
import { StationTabs } from "./StationTabs";

vi.mock("../api/stations", () => ({ listStations: vi.fn(), renameStation: vi.fn(), createStation: vi.fn() }));
vi.mock("../api/workspaces", () => ({ getWorkspace: vi.fn() }));

const state = (id: string, name: string): WorkspaceState => ({ id, name, conversation_id: null, page: null, revision: 1, focused_instance: null, visuals: [] });
const summary = (id: string, name: string, created_at: string): StationSummary => ({ id, name, conversation_id: null, page: null, created_at });
// The server lists the station changed last first; the tabs stand in the order the stations were made.
const STATIONS = [
  summary("w3", "beta", "2026-10-03T10:00:00Z"),
  summary("w1", "unnamed-workstream#1", "2026-10-01T10:00:00Z"),
  summary("w2", "Alpha trial", "2026-10-02T10:00:00Z"),
];

let client: QueryClient;

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  setStation("w1");
  history.replaceState(null, "", "#/list/Issue");
  vi.mocked(listStations).mockResolvedValue(STATIONS);
  vi.mocked(getWorkspace).mockImplementation(async (id) => state(id, STATIONS.find((each) => each.id === id)!.name));
});

afterEach(() => {
  cleanup();
  setStationMenu(false);
  setStation(null);
  vi.clearAllMocks();
  history.replaceState(null, "", "#/");
});

async function show(station = state("w1", "unnamed-workstream#1")) {
  render(<QueryClientProvider client={client}><StationTabs station={station} /></QueryClientProvider>);
  const tabs = screen.getByRole("navigation", { name: "Station tabs" });
  await within(tabs).findByRole("button", { name: "beta" });
  return tabs;
}

const names = (tabs: HTMLElement) => within(tabs).getAllByRole("button").map((tab) => tab.textContent);

test("each station is a tab, in the order they were made, the one on show current", async () => {
  const tabs = await show();
  expect(names(tabs)).toEqual(["unnamed-workstream#1", "Alpha trial", "beta"]);
  expect(within(tabs).getByRole("button", { name: "unnamed-workstream#1" }).getAttribute("aria-current")).toBe("true");
  expect(within(tabs).getByRole("button", { name: "beta" }).getAttribute("aria-current")).toBeNull();
});

test("before the list comes, the station on show is a tab of its own", () => {
  vi.mocked(listStations).mockReturnValue(new Promise(() => {}));
  render(<QueryClientProvider client={client}><StationTabs station={state("w1", "unnamed-workstream#1")} /></QueryClientProvider>);
  expect(names(screen.getByRole("navigation", { name: "Station tabs" }))).toEqual(["unnamed-workstream#1"]);
});

test("a click on another tab opens its station", async () => {
  const tabs = await show();
  fireEvent.click(within(tabs).getByRole("button", { name: "beta" }));
  await waitFor(() => expect(currentStation().id).toBe("w3"));
  expect(getWorkspace).toHaveBeenCalledWith("w3");
  expect(renameStation).not.toHaveBeenCalled();
});

test("a click on the current tab edits its name in place; Enter saves and the cache follows", async () => {
  vi.mocked(renameStation).mockResolvedValue(state("w1", "my work"));
  const tabs = await show();
  fireEvent.click(within(tabs).getByRole("button", { name: "unnamed-workstream#1" }));
  const name = within(tabs).getByRole("textbox", { name: "Station name" }) as HTMLInputElement;
  expect(document.activeElement).toBe(name);
  expect(name.value).toBe("unnamed-workstream#1");
  fireEvent.change(name, { target: { value: "  my work " } });
  fireEvent.keyDown(name, { key: "Enter" });
  await waitFor(() => expect(renameStation).toHaveBeenCalledWith("w1", "my work"));
  await waitFor(() => expect(within(tabs).queryByRole("textbox", { name: "Station name" })).toBeNull());
  expect(renameStation).toHaveBeenCalledTimes(1);
  expect(client.getQueryData<WorkspaceState>(["workspace", "w1"])?.name).toBe("my work");
});

test("a double click on another tab opens it and edits its name", async () => {
  const tabs = await show();
  fireEvent.doubleClick(within(tabs).getByRole("button", { name: "beta" }));
  await waitFor(() => expect(currentStation().id).toBe("w3"));
  expect(within(tabs).getByRole("textbox", { name: "Station name" })).toHaveProperty("value", "beta");
});

test("leaving the field saves the name, as Enter does", async () => {
  vi.mocked(renameStation).mockResolvedValue(state("w1", "elsewhere"));
  const tabs = await show();
  fireEvent.click(within(tabs).getByRole("button", { name: "unnamed-workstream#1" }));
  const name = within(tabs).getByRole("textbox", { name: "Station name" });
  fireEvent.change(name, { target: { value: "elsewhere" } });
  fireEvent.blur(name);
  await waitFor(() => expect(renameStation).toHaveBeenCalledWith("w1", "elsewhere"));
});

test("Esc, a blank name and an unchanged name leave the name as it was, with no request", async () => {
  const tabs = await show();
  // Esc stops at the field: it does not also close what holds the tabs.
  const outer = vi.fn((event: KeyboardEvent) => event.key);
  document.addEventListener("keydown", outer);
  for (const [value, key] of [["changed", "Escape"], ["   ", "Enter"], ["unnamed-workstream#1", "Enter"]] as const) {
    fireEvent.click(within(tabs).getByRole("button", { name: "unnamed-workstream#1" }));
    const name = within(tabs).getByRole("textbox", { name: "Station name" });
    fireEvent.change(name, { target: { value } });
    fireEvent.keyDown(name, { key });
    await waitFor(() => expect(within(tabs).queryByRole("textbox", { name: "Station name" })).toBeNull());
  }
  expect(renameStation).not.toHaveBeenCalled();
  expect(outer.mock.results.map((result) => result.value)).not.toContain("Escape");
  document.removeEventListener("keydown", outer);
  expect(document.activeElement).toBe(within(tabs).getByRole("button", { name: "unnamed-workstream#1" }));
});

test("a name that is taken says so and keeps the field; typing again clears the complaint", async () => {
  vi.mocked(renameStation).mockRejectedValue(new ApiError(409, "That name is taken"));
  const tabs = await show();
  fireEvent.click(within(tabs).getByRole("button", { name: "unnamed-workstream#1" }));
  const name = within(tabs).getByRole("textbox", { name: "Station name" });
  fireEvent.change(name, { target: { value: "beta" } });
  fireEvent.keyDown(name, { key: "Enter" });
  expect((await screen.findByRole("alert")).textContent).toBe("That name is taken");
  expect(within(tabs).getByRole("textbox", { name: "Station name" })).toBeTruthy();
  fireEvent.change(name, { target: { value: "beta 2" } });
  expect(screen.queryByRole("alert")).toBeNull();
});

test("+ makes a fresh station and switches to it", async () => {
  vi.mocked(createStation).mockResolvedValue(state("w4", "unnamed-workstream#2"));
  await show();
  fireEvent.click(screen.getByRole("button", { name: "New station" }));
  await waitFor(() => expect(currentStation().id).toBe("w4"));
  expect(client.getQueryData<WorkspaceState>(["workspace", "w4"])?.name).toBe("unnamed-workstream#2");
});

test("a + that fails says so and stays", async () => {
  vi.mocked(createStation).mockRejectedValue(new ApiError(500, "boom"));
  await show();
  fireEvent.click(screen.getByRole("button", { name: "New station" }));
  expect((await screen.findByRole("alert")).textContent).toBe("Could not make a station.");
  expect(currentStation().id).toBe("w1");
});

test("a tab that fails to open says so", async () => {
  vi.mocked(getWorkspace).mockRejectedValue(new ApiError(500, "boom"));
  const tabs = await show();
  fireEvent.click(within(tabs).getByRole("button", { name: "beta" }));
  expect((await screen.findByRole("alert")).textContent).toBe("Could not open that station.");
  expect(currentStation().id).toBe("w1");
});

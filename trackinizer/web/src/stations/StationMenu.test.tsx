import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { ApiError } from "../api/client";
import { createStation, listStations, renameStation, type StationSummary } from "../api/stations";
import { getWorkspace, type WorkspaceState } from "../api/workspaces";
import { setStationMenu } from "./actions";
import { currentStation, setStation } from "./store";
import { StationMenu } from "./StationMenu";

vi.mock("../api/stations", () => ({ listStations: vi.fn(), renameStation: vi.fn(), createStation: vi.fn() }));
vi.mock("../api/workspaces", () => ({ getWorkspace: vi.fn() }));

const state = (id: string, name: string): WorkspaceState => ({ id, name, conversation_id: null, page: null, revision: 1, focused_instance: null, visuals: [] });
const summary = (id: string, name: string): StationSummary => ({
  id, name, conversation_id: null, page: null, created_at: "2026-10-01T10:00:00Z",
});
const STATIONS = [summary("w1", "unnamed-workstream#1"), summary("w2", "Alpha trial"), summary("w3", "beta")];

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

function show() {
  render(<QueryClientProvider client={client}><StationMenu station={state("w1", "unnamed-workstream#1")} /></QueryClientProvider>);
  return screen.getByRole("button", { name: "unnamed-workstream#1" });
}

async function opened() {
  fireEvent.click(show());
  const menu = screen.getByRole("dialog", { name: "Stations" });
  await within(menu).findByRole("button", { name: "beta" });
  return menu;
}

test("the station's name is a button that opens a menu with a search, every station, Rename and New station", async () => {
  const button = show();
  expect(button.getAttribute("aria-expanded")).toBe("false");
  expect(screen.queryByRole("dialog", { name: "Stations" })).toBeNull();
  fireEvent.click(button);
  const menu = screen.getByRole("dialog", { name: "Stations" });
  expect(button.getAttribute("aria-expanded")).toBe("true");
  expect(document.activeElement).toBe(within(menu).getByRole("searchbox", { name: "Search stations" }));
  await within(menu).findByRole("button", { name: "beta" });
  expect(within(menu).getAllByRole("listitem").map((item) => item.textContent)).toEqual(["unnamed-workstream#1", "Alpha trial", "beta"]);
  expect(within(menu).getByRole("button", { name: "unnamed-workstream#1" }).getAttribute("aria-current")).toBe("true");
  expect(within(menu).getByRole("button", { name: "Rename" })).toBeTruthy();
  expect(within(menu).getByRole("button", { name: "New station" })).toBeTruthy();
});

test("typing filters the list by name in any case, and Enter opens the first match", async () => {
  const menu = await opened();
  const search = within(menu).getByRole("searchbox", { name: "Search stations" });
  fireEvent.change(search, { target: { value: "ALPHA" } });
  expect(within(menu).getAllByRole("listitem").map((item) => item.textContent)).toEqual(["Alpha trial"]);
  fireEvent.keyDown(search, { key: "Enter" });
  await waitFor(() => expect(currentStation().id).toBe("w2"));
  expect(getWorkspace).toHaveBeenCalledWith("w2");
  expect(screen.queryByRole("dialog", { name: "Stations" })).toBeNull();
  // The station opened moves the page to the one it showed last: the graph when it showed none.
  expect(location.hash).toBe("#/graph");
});

test("the arrow keys move the highlight, and Enter opens the one highlighted", async () => {
  const menu = await opened();
  const search = within(menu).getByRole("searchbox", { name: "Search stations" });
  fireEvent.keyDown(search, { key: "ArrowDown" });
  fireEvent.keyDown(search, { key: "ArrowDown" });
  fireEvent.keyDown(search, { key: "ArrowDown" });
  fireEvent.keyDown(search, { key: "ArrowUp" });
  fireEvent.keyDown(search, { key: "Enter" });
  await waitFor(() => expect(currentStation().id).toBe("w2"));
});

test("a click on a station opens it", async () => {
  const menu = await opened();
  fireEvent.click(within(menu).getByRole("button", { name: "beta" }));
  await waitFor(() => expect(currentStation().id).toBe("w3"));
});

test("a search that finds nothing says so, and Enter opens nothing", async () => {
  const menu = await opened();
  const search = within(menu).getByRole("searchbox", { name: "Search stations" });
  fireEvent.change(search, { target: { value: "zzz" } });
  expect(within(menu).getByText("No station by that name.")).toBeTruthy();
  fireEvent.keyDown(search, { key: "Enter" });
  expect(getWorkspace).not.toHaveBeenCalled();
});

test("Esc closes the menu and returns the keyboard to the name, and it does not go further", async () => {
  const outer = vi.fn();
  document.addEventListener("keydown", outer);
  const menu = await opened();
  fireEvent.keyDown(within(menu).getByRole("searchbox", { name: "Search stations" }), { key: "Escape" });
  expect(screen.queryByRole("dialog", { name: "Stations" })).toBeNull();
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "unnamed-workstream#1" }));
  expect(outer).not.toHaveBeenCalled();
  document.removeEventListener("keydown", outer);
});

test("Rename edits the name in place: Enter saves, and the list and the cache follow", async () => {
  vi.mocked(renameStation).mockResolvedValue(state("w1", "my work"));
  const menu = await opened();
  fireEvent.click(within(menu).getByRole("button", { name: "Rename" }));
  const name = within(menu).getByRole("textbox", { name: "Station name" }) as HTMLInputElement;
  expect(name.value).toBe("unnamed-workstream#1");
  fireEvent.change(name, { target: { value: "my work" } });
  fireEvent.submit(name.closest("form")!);
  await waitFor(() => expect(renameStation).toHaveBeenCalledWith("w1", "my work"));
  await waitFor(() => expect(within(menu).queryByRole("textbox", { name: "Station name" })).toBeNull());
  expect(client.getQueryData<WorkspaceState>(["workspace", "w1"])?.name).toBe("my work");
});

test("a name that is taken says so and keeps the field open", async () => {
  vi.mocked(renameStation).mockRejectedValue(new ApiError(409, "That name is taken"));
  const menu = await opened();
  fireEvent.click(within(menu).getByRole("button", { name: "Rename" }));
  const name = within(menu).getByRole("textbox", { name: "Station name" });
  fireEvent.change(name, { target: { value: "beta" } });
  fireEvent.submit(name.closest("form")!);
  expect((await within(menu).findByRole("alert")).textContent).toBe("That name is taken");
  expect(within(menu).getByRole("textbox", { name: "Station name" })).toBeTruthy();
  // Typing again clears the complaint; Esc leaves the field and not the menu.
  fireEvent.change(name, { target: { value: "beta 2" } });
  expect(within(menu).queryByRole("alert")).toBeNull();
  fireEvent.keyDown(name, { key: "Escape" });
  expect(within(menu).queryByRole("textbox", { name: "Station name" })).toBeNull();
  expect(screen.getByRole("dialog", { name: "Stations" })).toBeTruthy();
});

test("a blank name cannot be saved", async () => {
  const menu = await opened();
  fireEvent.click(within(menu).getByRole("button", { name: "Rename" }));
  fireEvent.change(within(menu).getByRole("textbox", { name: "Station name" }), { target: { value: "   " } });
  expect(within(menu).getByRole("button", { name: "Save" })).toHaveProperty("disabled", true);
});

test("New station makes one, switches to it and closes the menu", async () => {
  vi.mocked(createStation).mockResolvedValue(state("w4", "unnamed-workstream#2"));
  const menu = await opened();
  fireEvent.click(within(menu).getByRole("button", { name: "New station" }));
  await waitFor(() => expect(currentStation().id).toBe("w4"));
  expect(screen.queryByRole("dialog", { name: "Stations" })).toBeNull();
  expect(client.getQueryData<WorkspaceState>(["workspace", "w4"])?.name).toBe("unnamed-workstream#2");
});

test("a failed open says so and keeps the menu", async () => {
  vi.mocked(getWorkspace).mockRejectedValue(new ApiError(500, "boom"));
  const menu = await opened();
  fireEvent.click(within(menu).getByRole("button", { name: "beta" }));
  expect((await within(menu).findByRole("alert")).textContent).toBe("Could not open that station.");
  expect(currentStation().id).toBe("w1");
});

test("the menu is open when something else opens it, with the search focused", async () => {
  show();
  act(() => setStationMenu(true));
  expect(document.activeElement).toBe(screen.getByRole("searchbox", { name: "Search stations" }));
});

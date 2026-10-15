import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { ApiError } from "../api/client";
import { listStations, type StationSummary } from "../api/stations";
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
  return screen.getByRole("button", { name: "All stations" });
}

async function opened() {
  fireEvent.click(show());
  const menu = screen.getByRole("dialog", { name: "Stations" });
  await within(menu).findByRole("button", { name: "beta" });
  return menu;
}

test("All stations opens a menu with a search and every station; renaming and making one are the tabs' work", async () => {
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
  expect(within(menu).queryByRole("button", { name: "Rename" })).toBeNull();
  expect(within(menu).queryByRole("button", { name: "New station" })).toBeNull();
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

test("Esc closes the menu and returns the keyboard to its button, and it does not go further", async () => {
  const outer = vi.fn();
  document.addEventListener("keydown", outer);
  const menu = await opened();
  fireEvent.keyDown(within(menu).getByRole("searchbox", { name: "Search stations" }), { key: "Escape" });
  expect(screen.queryByRole("dialog", { name: "Stations" })).toBeNull();
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "All stations" }));
  expect(outer).not.toHaveBeenCalled();
  document.removeEventListener("keydown", outer);
});

test("a blur that names no new focus keeps the menu, so Safari's click on a station still lands", async () => {
  // Safari focuses no button that is clicked: the search blurs toward nothing before the click comes.
  const menu = await opened();
  fireEvent.blur(within(menu).getByRole("searchbox", { name: "Search stations" }), { relatedTarget: null });
  expect(screen.getByRole("dialog", { name: "Stations" })).toBeTruthy();
  fireEvent.click(within(menu).getByRole("button", { name: "beta" }));
  await waitFor(() => expect(currentStation().id).toBe("w3"));
});

test("a press outside the menu closes it, and so does the keyboard leaving it; a press inside does not", async () => {
  const outside = document.body.appendChild(document.createElement("button"));
  let menu = await opened();
  fireEvent.pointerDown(within(menu).getByRole("button", { name: "beta" }));
  expect(screen.getByRole("dialog", { name: "Stations" })).toBeTruthy();
  fireEvent.pointerDown(outside);
  expect(screen.queryByRole("dialog", { name: "Stations" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "All stations" }));
  menu = screen.getByRole("dialog", { name: "Stations" });
  fireEvent.blur(within(menu).getByRole("searchbox", { name: "Search stations" }), { relatedTarget: outside });
  expect(screen.queryByRole("dialog", { name: "Stations" })).toBeNull();
  outside.remove();
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

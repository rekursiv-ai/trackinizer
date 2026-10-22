import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { StationSummary } from "../api/stations";
import type { Workstream } from "../api/workstreams";
import { setStation } from "../stations/store";
import { WorkstreamBar } from "./WorkstreamBar";

const mocks = vi.hoisted(() => ({
  listWorkstreams: vi.fn(), createWorkstream: vi.fn(), updateWorkstream: vi.fn(), setWorkstreamOrder: vi.fn(), workstreamInfo: vi.fn(),
  createStation: vi.fn(), listStations: vi.fn(), openStation: vi.fn(), showStation: vi.fn(),
}));

vi.mock("../api/workstreams", () => ({
  listWorkstreams: mocks.listWorkstreams, createWorkstream: mocks.createWorkstream, updateWorkstream: mocks.updateWorkstream,
  setWorkstreamOrder: mocks.setWorkstreamOrder, workstreamInfo: mocks.workstreamInfo, createStation: mocks.createStation,
}));
vi.mock("../api/stations", () => ({ listStations: mocks.listStations }));
vi.mock("../stations/actions", () => ({
  stationsQuery: { queryKey: ["stations"], queryFn: mocks.listStations }, openStation: mocks.openStation,
}));
vi.mock("../stations/useStation", () => ({
  useStation: () => ({ data: { id: "current", name: "Chat", workstream_id: "ws-a" } }),
  showStation: mocks.showStation,
}));

const workstreams: Workstream[] = [
  { id: "scratch", title: "Scratch", seq: 3, scratch: true, hidden: false, pinned: true },
  { id: "ws-a", title: "Alpha", seq: 1, scratch: false, hidden: false, pinned: true },
  { id: "ws-b", title: "Beta", seq: 2, scratch: false, hidden: false, pinned: false },
  { id: "ws-hidden", title: "Hidden", seq: 4, scratch: false, hidden: true, pinned: false },
];
const station = (id: string, workstream_id: string | null, modified_at: string, extra: Partial<StationSummary> = {}): StationSummary => ({
  id, name: id, conversation_id: `c-${id}`, created_at: modified_at, modified_at, page: null,
  hidden: false, pinned: false, workstream_id, ...extra,
});
const stations: StationSummary[] = [
  station("a-recent", "ws-a", "2026-10-02T00:00:00Z"),
  station("a-pinned", "ws-a", "2026-10-01T00:00:00Z", { pinned: true }),
  station("b-hidden", "ws-b", "2026-10-04T00:00:00Z", { hidden: true }),
  station("b-recent", "ws-b", "2026-10-03T00:00:00Z"),
  station("scratch-chat", null, "2026-10-01T00:00:00Z"),
];

let client: QueryClient;

beforeEach(() => {
  setStation("current");
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  mocks.listWorkstreams.mockResolvedValue(workstreams);
  mocks.listStations.mockResolvedValue(stations);
  mocks.createWorkstream.mockResolvedValue({ ...workstreams[2]!, id: "ws-new", title: "Workstream 1" });
  mocks.createStation.mockResolvedValue({ id: "new", name: "New", revision: 1, visuals: [] });
  mocks.updateWorkstream.mockResolvedValue(workstreams[1]);
  mocks.setWorkstreamOrder.mockResolvedValue(workstreams);
  mocks.workstreamInfo.mockResolvedValue({ id: "ws-a", title: "Alpha", seq: 1, scratch: false, hidden: false, pinned: true, stations: [
    { id: "a-recent", name: "a-recent", conversation_id: "c-a-recent", hidden: false, pinned: false, session_ids: ["session-1"] },
  ] });
  mocks.openStation.mockResolvedValue(undefined);
  mocks.showStation.mockReturnValue(undefined);
  vi.clearAllMocks();
});

afterEach(() => { cleanup(); setStation(null); });

async function show(stationRows: readonly StationSummary[] = stations) {
  client.setQueryData(["workstreams"], workstreams);
  client.setQueryData(["stations"], stationRows);
  const { container } = render(<QueryClientProvider client={client}><WorkstreamBar /></QueryClientProvider>);
  return container.querySelector<HTMLElement>(".workstream-bar")!;
}

function workstream(name: string) {
  return screen.getByRole("button", { name: new RegExp(name) });
}

test("pins render first with a divider, followed by saved order; hidden workstreams stay out", async () => {
  const strip = await show();
  expect([...strip.querySelectorAll<HTMLButtonElement>(".workstream-title")].map((button) => button.getAttribute("aria-label"))).toEqual(["Scratch", "Alpha", "Beta"]);
  expect(strip.querySelectorAll(".workstream-divider")).toHaveLength(1);
  expect(strip.querySelector('[aria-label="Hidden"]')).toBeNull();
});

test("click opens a pinned chat first, else the newest visible chat, else creates one", async () => {
  await show();
  fireEvent.click(workstream("Alpha"));
  await waitFor(() => expect(mocks.openStation).toHaveBeenCalledWith(client, "a-pinned"));
  fireEvent.click(workstream("Beta"));
  await waitFor(() => expect(mocks.openStation).toHaveBeenCalledWith(client, "b-recent"));
  fireEvent.click(workstream("Scratch"));
  await waitFor(() => expect(mocks.openStation).toHaveBeenCalledWith(client, "scratch-chat"));
  mocks.listStations.mockResolvedValueOnce(stations);
  mocks.createStation.mockResolvedValueOnce({ id: "new", name: "New", revision: 1, visuals: [] });
});

test("an empty workstream creates a chat in that workstream and shows the returned state", async () => {
  await show([]);
  fireEvent.click(workstream("Beta"));
  await waitFor(() => expect(mocks.createStation).toHaveBeenCalledWith("ws-b"));
  expect(mocks.showStation).toHaveBeenCalledWith(client, { id: "new", name: "New", revision: 1, visuals: [] });
});

test.each(["station", "workstream"] as const)("delayed %s creation cannot replace a later station selection", async (stage) => {
  let finish!: () => void;
  const waiting = new Promise<void>((resolve) => { finish = resolve; });
  if (stage === "station") mocks.createStation.mockImplementationOnce(async () => {
    await waiting;
    return { id: "old-selection", name: "New", revision: 1, visuals: [] };
  });
  else mocks.createWorkstream.mockImplementationOnce(async () => {
    await waiting;
    return { ...workstreams[2]!, id: "ws-new", title: "New workstream" };
  });
  await show([]);
  fireEvent.click(stage === "station" ? workstream("Beta") : screen.getByRole("button", { name: "New workstream" }));
  act(() => setStation("latest-chat"));
  await act(async () => { finish(); await waiting; });
  expect(mocks.showStation).not.toHaveBeenCalled();
});

test("Info shows the Issue link and each chat session from the info route", async () => {
  await show();
  fireEvent.contextMenu(workstream("Alpha"));
  fireEvent.click(screen.getByRole("menuitem", { name: /Info/ }));
  const drawer = await screen.findByRole("dialog", { name: "Alpha info" });
  expect(mocks.workstreamInfo).toHaveBeenCalledWith("ws-a");
  expect(within(drawer).getByRole("link", { name: "Issue 1" }).getAttribute("href")).toBe("#/lookup/ws-a");
  expect(within(drawer).getByRole("link", { name: "Workstream settings" }).getAttribute("href")).toBe("#/lookup/ws-a");
  expect(within(drawer).getByRole("link", { name: "session-1" }).getAttribute("href")).toBe("#/lookup/session-1");
});

test("Scratch has no Hide action; other workstreams hide without confirmation", async () => {
  await show();
  fireEvent.contextMenu(workstream("Scratch"));
  expect(screen.queryByRole("menuitem", { name: /Hide/ })).toBeNull();
  fireEvent.keyDown(document, { key: "Escape" });
  fireEvent.contextMenu(workstream("Beta"));
  fireEvent.click(screen.getByRole("menuitem", { name: /Hide/ }));
  await waitFor(() => expect(mocks.updateWorkstream).toHaveBeenCalledWith("ws-b", { hidden: true }));
  expect(screen.queryByRole("alertdialog")).toBeNull();
});

test("moving left or right across the divider updates pin state and saves order", async () => {
  await show();
  fireEvent.contextMenu(workstream("Alpha"));
  fireEvent.click(screen.getByRole("menuitem", { name: /Move right/ }));
  await waitFor(() => expect(mocks.updateWorkstream).toHaveBeenCalledWith("ws-a", { pinned: false }));
  expect(mocks.setWorkstreamOrder).toHaveBeenCalledWith(["scratch", "ws-b", "ws-a", "ws-hidden"]);
  mocks.updateWorkstream.mockClear();
  mocks.setWorkstreamOrder.mockClear();
  fireEvent.contextMenu(workstream("Beta"));
  fireEvent.click(screen.getByRole("menuitem", { name: /Move left/ }));
  await waitFor(() => expect(mocks.updateWorkstream).toHaveBeenCalledWith("ws-b", { pinned: true }));
  expect(mocks.setWorkstreamOrder).toHaveBeenCalledWith(["scratch", "ws-b", "ws-a", "ws-hidden"]);
});

test("dropping on an unpinned workstream unpins the dragged workstream and saves order", async () => {
  await show();
  const beta = workstream("Beta");
  vi.spyOn(beta, "getBoundingClientRect").mockReturnValue({
    x: -100, y: 0, left: -100, right: 0, top: 0, bottom: 30, width: 100, height: 30, toJSON: () => ({}),
  });
  const transfer = { setData: vi.fn(), getData: vi.fn(() => "ws-a"), effectAllowed: "move" };
  fireEvent.dragStart(workstream("Alpha"), { dataTransfer: transfer });
  fireEvent.drop(beta, { dataTransfer: transfer, clientX: 100 });
  await waitFor(() => expect(mocks.updateWorkstream).toHaveBeenCalledWith("ws-a", { pinned: false }));
  expect(mocks.setWorkstreamOrder).toHaveBeenCalledWith(["scratch", "ws-a", "ws-b", "ws-hidden"]);
  mocks.updateWorkstream.mockClear();
  mocks.setWorkstreamOrder.mockClear();
  const alpha = workstream("Alpha");
  fireEvent.dragStart(beta, { dataTransfer: { setData: vi.fn(), getData: vi.fn(() => "ws-b"), effectAllowed: "move" } });
  fireEvent.drop(alpha, { dataTransfer: { setData: vi.fn(), getData: vi.fn(() => "ws-b"), effectAllowed: "move" }, clientX: 0 });
  await waitFor(() => expect(mocks.updateWorkstream).toHaveBeenCalledWith("ws-b", { pinned: true }));
  expect(mocks.setWorkstreamOrder).toHaveBeenCalledWith(["scratch", "ws-b", "ws-a", "ws-hidden"]);
});

test("context menu pins and renames a workstream inline", async () => {
  await show();
  fireEvent.contextMenu(workstream("Beta"));
  fireEvent.click(screen.getByRole("menuitem", { name: /Pin/ }));
  await waitFor(() => expect(mocks.updateWorkstream).toHaveBeenCalledWith("ws-b", { pinned: true }));
  fireEvent.contextMenu(workstream("Beta"));
  fireEvent.click(screen.getByRole("menuitem", { name: /Rename/ }));
  const name = screen.getByRole<HTMLInputElement>("textbox", { name: "Workstream name" });
  expect(document.activeElement).toBe(name);
  expect(name.value.slice(name.selectionStart!, name.selectionEnd!)).toBe("Beta");
  fireEvent.change(name, { target: { value: "  Roadmap  " } });
  fireEvent.keyDown(name, { key: "Enter" });
  expect(mocks.updateWorkstream).toHaveBeenCalledWith("ws-b", { title: "Roadmap" });
});

test("a click creates and opens a workstream with its name selected inline", async () => {
  const created: Workstream = { ...workstreams[2]!, id: "ws-new", title: "Workstream 1" };
  mocks.createWorkstream.mockResolvedValue(created);
  mocks.listWorkstreams.mockResolvedValue([...workstreams, created]);
  await show();
  fireEvent.click(screen.getByRole("button", { name: "New workstream" }));
  const name = await screen.findByRole<HTMLInputElement>("textbox", { name: "Workstream name" });
  expect(mocks.createWorkstream).toHaveBeenCalledWith("Workstream 1");
  await waitFor(() => expect(mocks.createStation).toHaveBeenCalledWith(created.id));
  expect(mocks.showStation).toHaveBeenCalledWith(client, { id: "new", name: "New", revision: 1, visuals: [] });
  expect(document.activeElement).toBe(name);
  expect(name.value.slice(name.selectionStart!, name.selectionEnd!)).toBe(created.title);
  fireEvent.change(name, { target: { value: "Roadmap" } });
  fireEvent.keyDown(name, { key: "Enter" });
  expect(mocks.updateWorkstream).toHaveBeenCalledWith(created.id, { title: "Roadmap" });
});

test("double-click renames inline; Escape cancels and blur saves", async () => {
  await show();
  fireEvent.doubleClick(workstream("Beta"));
  let name = screen.getByRole<HTMLInputElement>("textbox", { name: "Workstream name" });
  fireEvent.change(name, { target: { value: "Cancelled" } });
  fireEvent.keyDown(name, { key: "Escape" });
  expect(mocks.updateWorkstream).not.toHaveBeenCalled();
  expect(screen.queryByRole("textbox")).toBeNull();
  fireEvent.doubleClick(workstream("Beta"));
  name = screen.getByRole<HTMLInputElement>("textbox", { name: "Workstream name" });
  fireEvent.change(name, { target: { value: "Roadmap" } });
  fireEvent.blur(name);
  expect(mocks.updateWorkstream).toHaveBeenCalledWith("ws-b", { title: "Roadmap" });
});

test("failed creation leaves the bar usable and reports the failure", async () => {
  mocks.createWorkstream.mockRejectedValueOnce(new Error("offline"));
  await show();
  fireEvent.click(screen.getByRole("button", { name: "New workstream" }));
  expect((await screen.findByRole("alert")).textContent).toContain("Could not create that workstream.");
  expect(screen.getByRole<HTMLButtonElement>("button", { name: "New workstream" }).disabled).toBe(false);
});

test("double-clicking an empty workstream opens one chat and edits its name", async () => {
  let finish = () => {};
  const pending = new Promise<void>((resolve) => { finish = resolve; });
  mocks.createStation.mockImplementationOnce(async () => {
    await pending;
    return { id: "new", name: "New", revision: 1, visuals: [] };
  });
  await show([]);
  const beta = workstream("Beta");
  fireEvent.click(beta, { detail: 1 });
  fireEvent.click(beta, { detail: 2 });
  fireEvent.doubleClick(beta);
  expect(mocks.createStation).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("textbox", { name: "Workstream name" })).toBeTruthy();
  await act(async () => { finish(); await pending; });
});

test("Enter while composing a workstream name leaves the editor open", async () => {
  await show();
  fireEvent.doubleClick(workstream("Beta"));
  const name = screen.getByRole("textbox", { name: "Workstream name" });
  fireEvent.change(name, { target: { value: "研究" } });
  fireEvent.keyDown(name, { key: "Enter", isComposing: true });
  expect(mocks.updateWorkstream).not.toHaveBeenCalled();
  expect(document.activeElement).toBe(name);
  fireEvent.keyDown(name, { key: "Enter" });
  expect(mocks.updateWorkstream).toHaveBeenCalledWith("ws-b", { title: "研究" });
});

test("an older list response cannot remove a new workstream or its draft name", async () => {
  let finishList = (_rows: Workstream[]) => {};
  const pendingList = new Promise<Workstream[]>((resolve) => { finishList = resolve; });
  let failStation = () => {};
  const pendingStation = new Promise<never>((_resolve, reject) => { failStation = () => reject(new Error("offline")); });
  const created: Workstream = { ...workstreams[2]!, id: "ws-new", title: "Workstream 1" };
  mocks.listWorkstreams.mockReturnValueOnce(pendingList);
  mocks.createWorkstream.mockResolvedValue(created);
  mocks.createStation.mockReturnValueOnce(pendingStation);
  render(<QueryClientProvider client={client}><WorkstreamBar /></QueryClientProvider>);
  fireEvent.click(screen.getByRole("button", { name: "New workstream" }));
  const name = await screen.findByRole<HTMLInputElement>("textbox", { name: "Workstream name" });
  fireEvent.change(name, { target: { value: "My draft" } });
  await act(async () => { finishList([workstreams[0]!]); await pendingList; });
  expect(client.getQueryData<Workstream[]>(["workstreams"])?.map((stream) => stream.id)).toContain(created.id);
  expect(screen.getByRole<HTMLInputElement>("textbox", { name: "Workstream name" }).value).toBe("My draft");
  await act(async () => { failStation(); await pendingStation.catch(() => {}); });
  expect(screen.getByRole("alert").textContent).toContain("Could not open that workstream.");
  expect(screen.getByRole<HTMLInputElement>("textbox", { name: "Workstream name" }).value).toBe("My draft");
});

test("Scratch stays first and offers no unsupported pin or move actions", async () => {
  await show();
  const scratch = workstream("Scratch");
  expect(scratch.draggable).toBe(false);
  fireEvent.contextMenu(scratch);
  expect(screen.queryByRole("menuitem", { name: /Unpin|Pin|Move left|Move right|Hide/ })).toBeNull();
  fireEvent.keyDown(document, { key: "Escape" });
  fireEvent.contextMenu(workstream("Alpha"));
  expect(screen.getByRole<HTMLButtonElement>("menuitem", { name: "Move left" }).disabled).toBe(true);
  fireEvent.keyDown(document, { key: "Escape" });
  fireEvent.dragStart(workstream("Beta"), { dataTransfer: { setData: vi.fn(), effectAllowed: "move" } });
  fireEvent.drop(scratch, { dataTransfer: { getData: vi.fn(() => "ws-b") } });
  expect(mocks.updateWorkstream).not.toHaveBeenCalled();
  expect(mocks.setWorkstreamOrder).not.toHaveBeenCalled();
});

test("renaming refreshes the cached workstream Info heading", async () => {
  await show();
  fireEvent.contextMenu(workstream("Alpha"));
  fireEvent.click(screen.getByRole("menuitem", { name: "Info" }));
  await screen.findByRole("dialog", { name: "Alpha info" });
  fireEvent.click(screen.getByRole("button", { name: "Close info" }));
  mocks.listWorkstreams.mockResolvedValue(workstreams.map((stream) => stream.id === "ws-a" ? { ...stream, title: "Renamed" } : stream));
  mocks.workstreamInfo.mockResolvedValue({ ...workstreams[1], title: "Renamed", stations: [] });
  fireEvent.doubleClick(workstream("Alpha"));
  const name = screen.getByRole("textbox", { name: "Workstream name" });
  fireEvent.change(name, { target: { value: "Renamed" } });
  fireEvent.keyDown(name, { key: "Enter" });
  const renamed = await screen.findByRole("button", { name: "Renamed" });
  fireEvent.contextMenu(renamed);
  fireEvent.click(screen.getByRole("menuitem", { name: "Info" }));
  await screen.findByRole("dialog", { name: "Renamed info" });
  expect(mocks.workstreamInfo).toHaveBeenCalledTimes(2);
});

test("a workstream's tab carries a dot while a chat in it waits on the person, and the others do not", async () => {
  const strip = await show([...stations, station("b-waiting", "ws-b", "2026-10-05T00:00:00Z", { state: "requires_action" }),
    station("scratch-hidden", null, "2026-10-05T00:00:00Z", { state: "requires_action", hidden: true }),
    station("a-quiet", "ws-a", "2026-10-05T00:00:00Z", { state: "running" })]);
  const dots = [...strip.querySelectorAll<HTMLButtonElement>(".workstream-title")].map((tab) => [tab.getAttribute("aria-label"), tab.querySelector(".workstream-waiting")?.getAttribute("title") ?? null]);
  // A chat the person hid is not one they are waited on in.
  expect(dots).toEqual([["Scratch", null], ["Alpha", null], ["Beta", "A chat in Beta is waiting for your answer"]]);
});

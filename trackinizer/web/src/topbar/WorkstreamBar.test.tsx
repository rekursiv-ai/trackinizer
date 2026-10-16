import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { StationSummary } from "../api/stations";
import type { Workstream } from "../api/workstreams";
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
  { id: "ws-a", title: "Alpha", seq: 1, scratch: false, hidden: false, pinned: true },
  { id: "ws-b", title: "Beta", seq: 2, scratch: false, hidden: false, pinned: false },
  { id: "scratch", title: "Scratch", seq: 3, scratch: true, hidden: false, pinned: false },
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
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  mocks.listWorkstreams.mockResolvedValue(workstreams);
  mocks.listStations.mockResolvedValue(stations);
  mocks.createWorkstream.mockResolvedValue(workstreams[2]);
  mocks.createStation.mockResolvedValue({ id: "new", name: "New", revision: 1, visuals: [] });
  mocks.updateWorkstream.mockResolvedValue(workstreams[0]);
  mocks.setWorkstreamOrder.mockResolvedValue(workstreams);
  mocks.workstreamInfo.mockResolvedValue({ id: "ws-a", title: "Alpha", seq: 1, scratch: false, hidden: false, pinned: true, stations: [
    { id: "a-recent", name: "a-recent", conversation_id: "c-a-recent", hidden: false, pinned: false, session_ids: ["session-1"] },
  ] });
  mocks.openStation.mockResolvedValue(undefined);
  mocks.showStation.mockReturnValue(undefined);
  vi.clearAllMocks();
});

afterEach(() => cleanup());

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
  expect([...strip.querySelectorAll<HTMLButtonElement>(".workstream-title")].map((button) => button.getAttribute("aria-label"))).toEqual(["Alpha", "Beta", "Scratch"]);
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

test("Info shows the Issue link and each chat session from the info route", async () => {
  await show();
  fireEvent.contextMenu(workstream("Alpha"));
  fireEvent.click(screen.getByRole("menuitem", { name: /Info/ }));
  const drawer = await screen.findByRole("dialog", { name: "Alpha info" });
  expect(mocks.workstreamInfo).toHaveBeenCalledWith("ws-a");
  expect(within(drawer).getByRole("link", { name: "Issue 1" }).getAttribute("href")).toBe("#/lookup/ws-a");
  expect(drawer.textContent).toContain("session-1");
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
  expect(mocks.setWorkstreamOrder).toHaveBeenCalledWith(["ws-b", "ws-a", "scratch", "ws-hidden"]);
  mocks.updateWorkstream.mockClear();
  mocks.setWorkstreamOrder.mockClear();
  fireEvent.contextMenu(workstream("Beta"));
  fireEvent.click(screen.getByRole("menuitem", { name: /Move left/ }));
  await waitFor(() => expect(mocks.updateWorkstream).toHaveBeenCalledWith("ws-b", { pinned: true }));
  expect(mocks.setWorkstreamOrder).toHaveBeenCalledWith(["ws-b", "ws-a", "scratch", "ws-hidden"]);
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
  expect(mocks.setWorkstreamOrder).toHaveBeenCalledWith(["ws-a", "ws-b", "scratch", "ws-hidden"]);
  mocks.updateWorkstream.mockClear();
  mocks.setWorkstreamOrder.mockClear();
  const alpha = workstream("Alpha");
  fireEvent.dragStart(beta, { dataTransfer: { setData: vi.fn(), getData: vi.fn(() => "ws-b"), effectAllowed: "move" } });
  fireEvent.drop(alpha, { dataTransfer: { setData: vi.fn(), getData: vi.fn(() => "ws-b"), effectAllowed: "move" }, clientX: 0 });
  await waitFor(() => expect(mocks.updateWorkstream).toHaveBeenCalledWith("ws-b", { pinned: true }));
  expect(mocks.setWorkstreamOrder).toHaveBeenCalledWith(["ws-b", "ws-a", "scratch", "ws-hidden"]);
});

test("context menu pins and renames through PATCH; the add button creates a workstream", async () => {
  vi.stubGlobal("prompt", vi.fn(() => "Roadmap"));
  await show();
  fireEvent.contextMenu(workstream("Beta"));
  fireEvent.click(screen.getByRole("menuitem", { name: /Pin/ }));
  await waitFor(() => expect(mocks.updateWorkstream).toHaveBeenCalledWith("ws-b", { pinned: true }));
  fireEvent.contextMenu(workstream("Beta"));
  fireEvent.click(screen.getByRole("menuitem", { name: /Rename/ }));
  expect(mocks.updateWorkstream).toHaveBeenCalledWith("ws-b", { title: "Roadmap" });
  fireEvent.click(screen.getByRole("button", { name: "New workstream" }));
  await waitFor(() => expect(mocks.createWorkstream).toHaveBeenCalledWith("Roadmap"));
});

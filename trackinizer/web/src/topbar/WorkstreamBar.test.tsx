import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { StationSummary } from "../api/stations";
import type { Workstream } from "../api/workstreams";
import { colourOf, FOLD_AFTER_MS, PEEK_AFTER_MS, peekAt, WorkstreamBar } from "./WorkstreamBar";

const mocks = vi.hoisted(() => ({
  listWorkstreams: vi.fn(), createWorkstream: vi.fn(), updateWorkstream: vi.fn(), setWorkstreamOrder: vi.fn(), workstreamInfo: vi.fn(),
  createStation: vi.fn(), listStations: vi.fn(), openStation: vi.fn(), showStation: vi.fn(), nameStation: vi.fn(),
}));

vi.mock("../api/workstreams", () => ({
  listWorkstreams: mocks.listWorkstreams, createWorkstream: mocks.createWorkstream, updateWorkstream: mocks.updateWorkstream,
  setWorkstreamOrder: mocks.setWorkstreamOrder, workstreamInfo: mocks.workstreamInfo, createStation: mocks.createStation,
}));
vi.mock("../api/stations", () => ({ listStations: mocks.listStations }));
vi.mock("../stations/actions", () => ({
  stationsQuery: { queryKey: ["stations"], queryFn: mocks.listStations }, openStation: mocks.openStation, nameStation: mocks.nameStation,
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

afterEach(() => cleanup());

async function show(stationRows: readonly StationSummary[] = stations) {
  client.setQueryData(["workstreams"], workstreams);
  client.setQueryData(["stations"], stationRows);
  const { container } = render(<QueryClientProvider client={client}><WorkstreamBar /></QueryClientProvider>);
  return container.querySelector<HTMLElement>(".workstream-bar")!;
}

function workstream(name: string) {
  return screen.getByRole("button", { name });
}

test("workstreams stand in the saved order with no pin controls; hidden ones stay out", async () => {
  const strip = await show();
  expect([...strip.querySelectorAll<HTMLButtonElement>(".workstream-title")].map((button) => button.getAttribute("aria-label"))).toEqual(["Scratch", "Alpha", "Beta"]);
  expect(strip.querySelector(".workstream-divider")).toBeNull();
  expect(screen.queryByRole("button", { name: /^(Pin|Unpin) / })).toBeNull();
  expect(strip.querySelector('[aria-label="Hidden"]')).toBeNull();
});

test("click opens a pinned chat first, else the newest visible chat, else creates one", async () => {
  await show();
  fireEvent.click(workstream("Beta"));
  await waitFor(() => expect(mocks.openStation).toHaveBeenCalledWith(client, "b-recent"));
  fireEvent.click(workstream("Scratch"));
  await waitFor(() => expect(mocks.openStation).toHaveBeenCalledWith(client, "scratch-chat"));
});

function group(name: string) {
  return screen.getByRole("group", { name });
}

test("a group lists the chat on show, then pinned chats, then recent ones; hidden chats stay out", async () => {
  await show();
  const chats = within(group("Alpha")).getAllByRole("button").filter((button) => button.classList.contains("workstream-chat"));
  expect(chats.map((button) => button.textContent)).toEqual(["Chat", "a-pinned", "a-recent"]);
  expect(chats[0]!.getAttribute("aria-current")).toBe("true");
  expect(within(group("Beta")).queryByRole("button", { name: "b-hidden" })).toBeNull();
});

test("a click on the current workstream keeps its chats out and a second folds them, opening no chat", async () => {
  await show();
  const alpha = workstream("Alpha");
  expect(alpha.getAttribute("aria-expanded")).toBe("false");
  expect(alpha.textContent).toContain("· Chat");
  fireEvent.click(alpha);
  expect(alpha.getAttribute("aria-expanded")).toBe("true");
  expect(group("Alpha").className).toContain("is-open");
  expect(alpha.textContent).not.toContain("· Chat");
  fireEvent.pointerLeave(document.querySelector(".workstream-bar")!);
  fireEvent.click(alpha);
  expect(alpha.getAttribute("aria-expanded")).toBe("false");
  expect(mocks.openStation).not.toHaveBeenCalled();
});

test("resting on a group peeks its chats; passing over opens nothing; leaving the bar folds them", async () => {
  await show();
  vi.useFakeTimers();
  try {
    fireEvent.pointerEnter(group("Beta"));
    act(() => { vi.advanceTimersByTime(PEEK_AFTER_MS - 1); });
    expect(workstream("Beta").getAttribute("aria-expanded")).toBe("false");
    act(() => { vi.advanceTimersByTime(1); });
    expect(workstream("Beta").getAttribute("aria-expanded")).toBe("true");
    fireEvent.pointerLeave(document.querySelector(".workstream-bar")!);
    act(() => { vi.advanceTimersByTime(FOLD_AFTER_MS - 1); });
    expect(workstream("Beta").getAttribute("aria-expanded")).toBe("true");
    act(() => { vi.advanceTimersByTime(1); });
    expect(workstream("Beta").getAttribute("aria-expanded")).toBe("false");
    // A pointer crossing a group on its way elsewhere opens nothing.
    fireEvent.pointerEnter(group("Scratch"));
    act(() => { vi.advanceTimersByTime(PEEK_AFTER_MS / 2); });
    fireEvent.pointerEnter(group("Beta"));
    act(() => { vi.advanceTimersByTime(PEEK_AFTER_MS); });
    expect(workstream("Scratch").getAttribute("aria-expanded")).toBe("false");
    expect(workstream("Beta").getAttribute("aria-expanded")).toBe("true");
  } finally {
    vi.useRealTimers();
  }
});

test("a workstream keeps its colour wherever it stands", async () => {
  await show();
  const colour = group("Beta").style.getPropertyValue("--group");
  expect(colour).toBe(`var(--group-${colourOf("ws-b")})`);
  fireEvent.contextMenu(workstream("Beta"));
  fireEvent.click(screen.getByRole("menuitem", { name: "Move left" }));
  await waitFor(() => expect(mocks.setWorkstreamOrder).toHaveBeenCalled());
  expect(group("Beta").style.getPropertyValue("--group")).toBe(colour);
  expect(new Set(["a", "b", "c", "d", "e"].map(colourOf)).size).toBeGreaterThan(1);
});

test("a peek keeps the groups left of it out, so nothing slides under the pointer, and folds those right of it", () => {
  const order = [{ id: "a" }, { id: "b" }, { id: "c" }];
  expect(peekAt([], "b", order)).toEqual(["b"]);
  expect(peekAt(["a"], "c", order)).toEqual(["a", "c"]);
  expect(peekAt(["a", "c"], "b", order)).toEqual(["a", "b"]);
  expect(peekAt(["b"], "b", order)).toEqual(["b"]);
});

test("a chat opens on a click and renames inline on a double click", async () => {
  mocks.nameStation.mockResolvedValue(undefined);
  await show();
  fireEvent.click(workstream("Alpha"));
  fireEvent.click(within(group("Alpha")).getByRole("button", { name: /a-recent/ }));
  await waitFor(() => expect(mocks.openStation).toHaveBeenCalledWith(client, "a-recent"));
  fireEvent.doubleClick(within(group("Alpha")).getByRole("button", { name: /a-recent/ }));
  const name = screen.getByRole<HTMLInputElement>("textbox", { name: "Chat name" });
  expect(document.activeElement).toBe(name);
  fireEvent.change(name, { target: { value: "  Diverged run  " } });
  fireEvent.keyDown(name, { key: "Enter" });
  await waitFor(() => expect(mocks.nameStation).toHaveBeenCalledWith(client, "a-recent", "Diverged run"));
});

test("a chat name already taken reports the conflict", async () => {
  const { ApiError } = await import("../api/client");
  mocks.nameStation.mockRejectedValue(new ApiError(409, "taken"));
  await show();
  fireEvent.doubleClick(within(group("Alpha")).getByRole("button", { name: /a-recent/ }));
  const name = screen.getByRole("textbox", { name: "Chat name" });
  fireEvent.change(name, { target: { value: "a-pinned" } });
  fireEvent.keyDown(name, { key: "Enter" });
  expect((await screen.findByRole("alert")).textContent).toContain("You already have a chat with that name");
});

test("+ makes a chat in that workstream, opens it and selects its name", async () => {
  const made = { id: "made", name: "unnamed-workstream#4", revision: 1, visuals: [] };
  mocks.createStation.mockResolvedValue(made);
  mocks.listStations.mockResolvedValue([...stations, station("made", "ws-b", "2026-10-05T00:00:00Z", { name: made.name })]);
  await show();
  fireEvent.click(screen.getByRole("button", { name: "New chat in Beta" }));
  await waitFor(() => expect(mocks.createStation).toHaveBeenCalledWith("ws-b"));
  expect(mocks.showStation).toHaveBeenCalledWith(client, made);
  // The mocked station on show stays in Alpha, so Beta stays folded (inert) here; opened, the new chat would be on show.
  const name = await screen.findByRole<HTMLInputElement>("textbox", { name: "Chat name", hidden: true });
  expect(name.value).toBe(made.name);
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

test("moving left or right saves the order and nothing else", async () => {
  await show();
  fireEvent.contextMenu(workstream("Alpha"));
  fireEvent.click(screen.getByRole("menuitem", { name: /Move right/ }));
  await waitFor(() => expect(mocks.setWorkstreamOrder).toHaveBeenCalledWith(["scratch", "ws-b", "ws-a", "ws-hidden"]));
  mocks.setWorkstreamOrder.mockClear();
  // The list read again still has the old order, so Beta moves left of Alpha from there.
  fireEvent.contextMenu(workstream("Beta"));
  fireEvent.click(screen.getByRole("menuitem", { name: /Move left/ }));
  await waitFor(() => expect(mocks.setWorkstreamOrder).toHaveBeenCalledWith(["scratch", "ws-b", "ws-a", "ws-hidden"]));
  expect(mocks.updateWorkstream).not.toHaveBeenCalled();
});

test("dropping a workstream on another saves the order and nothing else", async () => {
  await show();
  // jsdom's drag events carry no pointer position, so a drop lands before its target.
  const transfer = { setData: vi.fn(), getData: vi.fn(() => "ws-b"), effectAllowed: "move" };
  fireEvent.dragStart(workstream("Beta"), { dataTransfer: transfer });
  fireEvent.drop(workstream("Alpha"), { dataTransfer: transfer });
  await waitFor(() => expect(mocks.setWorkstreamOrder).toHaveBeenCalledWith(["scratch", "ws-b", "ws-a", "ws-hidden"]));
  expect(mocks.updateWorkstream).not.toHaveBeenCalled();
});

test("the context menu renames a workstream inline and offers no pin", async () => {
  await show();
  fireEvent.contextMenu(workstream("Beta"));
  expect(screen.queryByRole("menuitem", { name: /Pin/ })).toBeNull();
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

test("Scratch moves and drags as any workstream, but offers no Hide", async () => {
  await show();
  const scratch = workstream("Scratch");
  expect(scratch.draggable).toBe(true);
  fireEvent.contextMenu(scratch);
  expect(screen.queryByRole("menuitem", { name: "Hide" })).toBeNull();
  fireEvent.click(screen.getByRole("menuitem", { name: "Move right" }));
  await waitFor(() => expect(mocks.setWorkstreamOrder).toHaveBeenCalledWith(["ws-a", "scratch", "ws-b", "ws-hidden"]));
});

test("Scratch drags onto another workstream's place like any other", async () => {
  await show();
  // jsdom's drag events carry no pointer position, so a drop lands before its target.
  const transfer = { setData: vi.fn(), getData: vi.fn(() => "scratch"), effectAllowed: "move" };
  fireEvent.dragStart(workstream("Scratch"), { dataTransfer: transfer });
  fireEvent.drop(workstream("Beta"), { dataTransfer: transfer });
  await waitFor(() => expect(mocks.setWorkstreamOrder).toHaveBeenCalledWith(["ws-a", "scratch", "ws-b", "ws-hidden"]));
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

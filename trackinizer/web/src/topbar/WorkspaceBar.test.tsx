import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { CanvasSummary } from "../api/canvasManagement";
import type { Workspace } from "../api/workspaces";
import { setCanvas } from "../canvases/store";
import { WorkspaceBar } from "./WorkspaceBar";

const mocks = vi.hoisted(() => ({
  listWorkspaces: vi.fn(), createWorkspace: vi.fn(), updateWorkspace: vi.fn(), setWorkspaceOrder: vi.fn(), workspaceInfo: vi.fn(),
  createCanvas: vi.fn(), listCanvases: vi.fn(), openCanvas: vi.fn(), showCanvas: vi.fn(),
}));

vi.mock("../api/workspaces", () => ({
  listWorkspaces: mocks.listWorkspaces, createWorkspace: mocks.createWorkspace, updateWorkspace: mocks.updateWorkspace,
  setWorkspaceOrder: mocks.setWorkspaceOrder, workspaceInfo: mocks.workspaceInfo, createCanvas: mocks.createCanvas,
}));
vi.mock("../api/canvasManagement", () => ({ listCanvases: mocks.listCanvases }));
vi.mock("../canvases/actions", () => ({
  canvasesQuery: { queryKey: ["canvases"], queryFn: mocks.listCanvases }, openCanvas: mocks.openCanvas,
}));
vi.mock("../canvases/useCanvas", () => ({
  useCanvas: () => ({ data: { id: "current", name: "Chat", workspace_id: "ws-a" } }),
  showCanvas: mocks.showCanvas,
}));

const workspaces: Workspace[] = [
  { id: "scratch", title: "Scratch", seq: 3, scratch: true, hidden: false, pinned: true },
  { id: "ws-a", title: "Alpha", seq: 1, scratch: false, hidden: false, pinned: true },
  { id: "ws-b", title: "Beta", seq: 2, scratch: false, hidden: false, pinned: false },
  { id: "ws-hidden", title: "Hidden", seq: 4, scratch: false, hidden: true, pinned: false },
];
const canvas = (id: string, workspace_id: string | null, modified_at: string, extra: Partial<CanvasSummary> = {}): CanvasSummary => ({
  id, name: id, conversation_id: `c-${id}`, created_at: modified_at, modified_at, page: null,
  hidden: false, pinned: false, workspace_id, ...extra,
});
const canvases: CanvasSummary[] = [
  canvas("a-recent", "ws-a", "2026-10-02T00:00:00Z"),
  canvas("a-pinned", "ws-a", "2026-10-01T00:00:00Z", { pinned: true }),
  canvas("b-hidden", "ws-b", "2026-10-04T00:00:00Z", { hidden: true }),
  canvas("b-recent", "ws-b", "2026-10-03T00:00:00Z"),
  canvas("scratch-chat", null, "2026-10-01T00:00:00Z"),
];

let client: QueryClient;

beforeEach(() => {
  setCanvas("current");
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  mocks.listWorkspaces.mockResolvedValue(workspaces);
  mocks.listCanvases.mockResolvedValue(canvases);
  mocks.createWorkspace.mockResolvedValue({ ...workspaces[2]!, id: "ws-new", title: "Workspace 1" });
  mocks.createCanvas.mockResolvedValue({ id: "new", name: "New", revision: 1, visuals: [] });
  mocks.updateWorkspace.mockResolvedValue(workspaces[1]);
  mocks.setWorkspaceOrder.mockResolvedValue(workspaces);
  mocks.workspaceInfo.mockResolvedValue({ id: "ws-a", title: "Alpha", seq: 1, scratch: false, hidden: false, pinned: true, canvases: [
    { id: "a-recent", name: "a-recent", conversation_id: "c-a-recent", hidden: false, pinned: false, session_ids: ["session-1"] },
  ] });
  mocks.openCanvas.mockResolvedValue(undefined);
  mocks.showCanvas.mockReturnValue(undefined);
  vi.clearAllMocks();
});

afterEach(() => { cleanup(); setCanvas(null); });

async function show(canvasRows: readonly CanvasSummary[] = canvases) {
  client.setQueryData(["workspaces"], workspaces);
  client.setQueryData(["canvases"], canvasRows);
  const { container } = render(<QueryClientProvider client={client}><WorkspaceBar /></QueryClientProvider>);
  return container.querySelector<HTMLElement>(".workspace-bar")!;
}

function workspace(name: string) {
  return screen.getByRole("button", { name: new RegExp(name) });
}

test("pins render first with a divider, followed by saved order; hidden workspaces stay out", async () => {
  const strip = await show();
  expect([...strip.querySelectorAll<HTMLButtonElement>(".workspace-title")].map((button) => button.getAttribute("aria-label"))).toEqual(["Scratch", "Alpha", "Beta"]);
  expect(strip.querySelectorAll(".workspace-divider")).toHaveLength(1);
  expect(strip.querySelector('[aria-label="Hidden"]')).toBeNull();
});

test("click opens a pinned chat first, else the newest visible chat, else creates one", async () => {
  await show();
  fireEvent.click(workspace("Alpha"));
  await waitFor(() => expect(mocks.openCanvas).toHaveBeenCalledWith(client, "a-pinned"));
  fireEvent.click(workspace("Beta"));
  await waitFor(() => expect(mocks.openCanvas).toHaveBeenCalledWith(client, "b-recent"));
  fireEvent.click(workspace("Scratch"));
  await waitFor(() => expect(mocks.openCanvas).toHaveBeenCalledWith(client, "scratch-chat"));
  mocks.listCanvases.mockResolvedValueOnce(canvases);
  mocks.createCanvas.mockResolvedValueOnce({ id: "new", name: "New", revision: 1, visuals: [] });
});

test("an empty workspace creates a chat in that workspace and shows the returned state", async () => {
  await show([]);
  fireEvent.click(workspace("Beta"));
  await waitFor(() => expect(mocks.createCanvas).toHaveBeenCalledWith("ws-b"));
  expect(mocks.showCanvas).toHaveBeenCalledWith(client, { id: "new", name: "New", revision: 1, visuals: [] });
});

test.each(["canvas", "workspace"] as const)("delayed %s creation cannot replace a later canvas selection", async (stage) => {
  let finish!: () => void;
  const waiting = new Promise<void>((resolve) => { finish = resolve; });
  if (stage === "canvas") mocks.createCanvas.mockImplementationOnce(async () => {
    await waiting;
    return { id: "old-selection", name: "New", revision: 1, visuals: [] };
  });
  else mocks.createWorkspace.mockImplementationOnce(async () => {
    await waiting;
    return { ...workspaces[2]!, id: "ws-new", title: "New workspace" };
  });
  await show([]);
  fireEvent.click(stage === "canvas" ? workspace("Beta") : screen.getByRole("button", { name: "New workspace" }));
  act(() => setCanvas("latest-chat"));
  await act(async () => { finish(); await waiting; });
  expect(mocks.showCanvas).not.toHaveBeenCalled();
});

test("Info shows the Issue link and each chat session from the info route", async () => {
  await show();
  fireEvent.contextMenu(workspace("Alpha"));
  fireEvent.click(screen.getByRole("menuitem", { name: /Info/ }));
  const drawer = await screen.findByRole("dialog", { name: "Alpha info" });
  expect(mocks.workspaceInfo).toHaveBeenCalledWith("ws-a");
  expect(within(drawer).getByRole("link", { name: "Issue 1" }).getAttribute("href")).toBe("/trax/lookup/ws-a");
  expect(within(drawer).getByRole("link", { name: "Workspace settings" }).getAttribute("href")).toBe("/trax/lookup/ws-a");
  expect(within(drawer).getByRole("link", { name: "session-1" }).getAttribute("href")).toBe("/trax/lookup/session-1");
});

test("Scratch has no Hide action; other workspaces hide without confirmation", async () => {
  await show();
  fireEvent.contextMenu(workspace("Scratch"));
  expect(screen.queryByRole("menuitem", { name: /Hide/ })).toBeNull();
  fireEvent.keyDown(document, { key: "Escape" });
  fireEvent.contextMenu(workspace("Beta"));
  fireEvent.click(screen.getByRole("menuitem", { name: /Hide/ }));
  await waitFor(() => expect(mocks.updateWorkspace).toHaveBeenCalledWith("ws-b", { hidden: true }));
  expect(screen.queryByRole("alertdialog")).toBeNull();
});

test("moving left or right across the divider updates pin state and saves order", async () => {
  await show();
  fireEvent.contextMenu(workspace("Alpha"));
  fireEvent.click(screen.getByRole("menuitem", { name: /Move right/ }));
  await waitFor(() => expect(mocks.updateWorkspace).toHaveBeenCalledWith("ws-a", { pinned: false }));
  expect(mocks.setWorkspaceOrder).toHaveBeenCalledWith(["scratch", "ws-b", "ws-a", "ws-hidden"]);
  mocks.updateWorkspace.mockClear();
  mocks.setWorkspaceOrder.mockClear();
  fireEvent.contextMenu(workspace("Beta"));
  fireEvent.click(screen.getByRole("menuitem", { name: /Move left/ }));
  await waitFor(() => expect(mocks.updateWorkspace).toHaveBeenCalledWith("ws-b", { pinned: true }));
  expect(mocks.setWorkspaceOrder).toHaveBeenCalledWith(["scratch", "ws-b", "ws-a", "ws-hidden"]);
});

test("dropping on an unpinned workspace unpins the dragged workspace and saves order", async () => {
  await show();
  const beta = workspace("Beta");
  vi.spyOn(beta, "getBoundingClientRect").mockReturnValue({
    x: -100, y: 0, left: -100, right: 0, top: 0, bottom: 30, width: 100, height: 30, toJSON: () => ({}),
  });
  const transfer = { setData: vi.fn(), getData: vi.fn(() => "ws-a"), effectAllowed: "move" };
  fireEvent.dragStart(workspace("Alpha"), { dataTransfer: transfer });
  fireEvent.drop(beta, { dataTransfer: transfer, clientX: 100 });
  await waitFor(() => expect(mocks.updateWorkspace).toHaveBeenCalledWith("ws-a", { pinned: false }));
  expect(mocks.setWorkspaceOrder).toHaveBeenCalledWith(["scratch", "ws-a", "ws-b", "ws-hidden"]);
  mocks.updateWorkspace.mockClear();
  mocks.setWorkspaceOrder.mockClear();
  const alpha = workspace("Alpha");
  fireEvent.dragStart(beta, { dataTransfer: { setData: vi.fn(), getData: vi.fn(() => "ws-b"), effectAllowed: "move" } });
  fireEvent.drop(alpha, { dataTransfer: { setData: vi.fn(), getData: vi.fn(() => "ws-b"), effectAllowed: "move" }, clientX: 0 });
  await waitFor(() => expect(mocks.updateWorkspace).toHaveBeenCalledWith("ws-b", { pinned: true }));
  expect(mocks.setWorkspaceOrder).toHaveBeenCalledWith(["scratch", "ws-b", "ws-a", "ws-hidden"]);
});

test("context menu pins and renames a workspace inline", async () => {
  await show();
  fireEvent.contextMenu(workspace("Beta"));
  fireEvent.click(screen.getByRole("menuitem", { name: /Pin/ }));
  await waitFor(() => expect(mocks.updateWorkspace).toHaveBeenCalledWith("ws-b", { pinned: true }));
  fireEvent.contextMenu(workspace("Beta"));
  fireEvent.click(screen.getByRole("menuitem", { name: /Rename/ }));
  const name = screen.getByRole<HTMLInputElement>("textbox", { name: "Workspace name" });
  expect(document.activeElement).toBe(name);
  expect(name.value.slice(name.selectionStart!, name.selectionEnd!)).toBe("Beta");
  fireEvent.change(name, { target: { value: "  Roadmap  " } });
  fireEvent.keyDown(name, { key: "Enter" });
  expect(mocks.updateWorkspace).toHaveBeenCalledWith("ws-b", { title: "Roadmap" });
});

test("a click creates and opens a workspace with its name selected inline", async () => {
  const created: Workspace = { ...workspaces[2]!, id: "ws-new", title: "Workspace 1" };
  mocks.createWorkspace.mockResolvedValue(created);
  mocks.listWorkspaces.mockResolvedValue([...workspaces, created]);
  await show();
  fireEvent.click(screen.getByRole("button", { name: "New workspace" }));
  const name = await screen.findByRole<HTMLInputElement>("textbox", { name: "Workspace name" });
  expect(mocks.createWorkspace).toHaveBeenCalledWith("Workspace 1");
  await waitFor(() => expect(mocks.createCanvas).toHaveBeenCalledWith(created.id));
  expect(mocks.showCanvas).toHaveBeenCalledWith(client, { id: "new", name: "New", revision: 1, visuals: [] });
  expect(document.activeElement).toBe(name);
  expect(name.value.slice(name.selectionStart!, name.selectionEnd!)).toBe(created.title);
  fireEvent.change(name, { target: { value: "Roadmap" } });
  fireEvent.keyDown(name, { key: "Enter" });
  expect(mocks.updateWorkspace).toHaveBeenCalledWith(created.id, { title: "Roadmap" });
});

test("double-click renames inline; Escape cancels and blur saves", async () => {
  await show();
  fireEvent.doubleClick(workspace("Beta"));
  let name = screen.getByRole<HTMLInputElement>("textbox", { name: "Workspace name" });
  fireEvent.change(name, { target: { value: "Cancelled" } });
  fireEvent.keyDown(name, { key: "Escape" });
  expect(mocks.updateWorkspace).not.toHaveBeenCalled();
  expect(screen.queryByRole("textbox")).toBeNull();
  fireEvent.doubleClick(workspace("Beta"));
  name = screen.getByRole<HTMLInputElement>("textbox", { name: "Workspace name" });
  fireEvent.change(name, { target: { value: "Roadmap" } });
  fireEvent.blur(name);
  expect(mocks.updateWorkspace).toHaveBeenCalledWith("ws-b", { title: "Roadmap" });
});

test("failed creation leaves the bar usable and reports the failure", async () => {
  mocks.createWorkspace.mockRejectedValueOnce(new Error("offline"));
  await show();
  fireEvent.click(screen.getByRole("button", { name: "New workspace" }));
  expect((await screen.findByRole("alert")).textContent).toContain("Could not create that workspace.");
  expect(screen.getByRole<HTMLButtonElement>("button", { name: "New workspace" }).disabled).toBe(false);
});

test("double-clicking an empty workspace opens one chat and edits its name", async () => {
  let finish = () => {};
  const pending = new Promise<void>((resolve) => { finish = resolve; });
  mocks.createCanvas.mockImplementationOnce(async () => {
    await pending;
    return { id: "new", name: "New", revision: 1, visuals: [] };
  });
  await show([]);
  const beta = workspace("Beta");
  fireEvent.click(beta, { detail: 1 });
  fireEvent.click(beta, { detail: 2 });
  fireEvent.doubleClick(beta);
  expect(mocks.createCanvas).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("textbox", { name: "Workspace name" })).toBeTruthy();
  await act(async () => { finish(); await pending; });
});

test("Enter while composing a workspace name leaves the editor open", async () => {
  await show();
  fireEvent.doubleClick(workspace("Beta"));
  const name = screen.getByRole("textbox", { name: "Workspace name" });
  fireEvent.change(name, { target: { value: "研究" } });
  fireEvent.keyDown(name, { key: "Enter", isComposing: true });
  expect(mocks.updateWorkspace).not.toHaveBeenCalled();
  expect(document.activeElement).toBe(name);
  fireEvent.keyDown(name, { key: "Enter" });
  expect(mocks.updateWorkspace).toHaveBeenCalledWith("ws-b", { title: "研究" });
});

test("an older list response cannot remove a new workspace or its draft name", async () => {
  let finishList = (_rows: Workspace[]) => {};
  const pendingList = new Promise<Workspace[]>((resolve) => { finishList = resolve; });
  let failCanvas = () => {};
  const pendingCanvas = new Promise<never>((_resolve, reject) => { failCanvas = () => reject(new Error("offline")); });
  const created: Workspace = { ...workspaces[2]!, id: "ws-new", title: "Workspace 1" };
  mocks.listWorkspaces.mockReturnValueOnce(pendingList);
  mocks.createWorkspace.mockResolvedValue(created);
  mocks.createCanvas.mockReturnValueOnce(pendingCanvas);
  render(<QueryClientProvider client={client}><WorkspaceBar /></QueryClientProvider>);
  fireEvent.click(screen.getByRole("button", { name: "New workspace" }));
  const name = await screen.findByRole<HTMLInputElement>("textbox", { name: "Workspace name" });
  fireEvent.change(name, { target: { value: "My draft" } });
  await act(async () => { finishList([workspaces[0]!]); await pendingList; });
  expect(client.getQueryData<Workspace[]>(["workspaces"])?.map((stream) => stream.id)).toContain(created.id);
  expect(screen.getByRole<HTMLInputElement>("textbox", { name: "Workspace name" }).value).toBe("My draft");
  await act(async () => { failCanvas(); await pendingCanvas.catch(() => {}); });
  expect(screen.getByRole("alert").textContent).toContain("Could not open that workspace.");
  expect(screen.getByRole<HTMLInputElement>("textbox", { name: "Workspace name" }).value).toBe("My draft");
});

test("Scratch stays first and offers no unsupported pin or move actions", async () => {
  await show();
  const scratch = workspace("Scratch");
  expect(scratch.draggable).toBe(false);
  fireEvent.contextMenu(scratch);
  expect(screen.queryByRole("menuitem", { name: /Unpin|Pin|Move left|Move right|Hide/ })).toBeNull();
  fireEvent.keyDown(document, { key: "Escape" });
  fireEvent.contextMenu(workspace("Alpha"));
  expect(screen.getByRole<HTMLButtonElement>("menuitem", { name: "Move left" }).disabled).toBe(true);
  fireEvent.keyDown(document, { key: "Escape" });
  fireEvent.dragStart(workspace("Beta"), { dataTransfer: { setData: vi.fn(), effectAllowed: "move" } });
  fireEvent.drop(scratch, { dataTransfer: { getData: vi.fn(() => "ws-b") } });
  expect(mocks.updateWorkspace).not.toHaveBeenCalled();
  expect(mocks.setWorkspaceOrder).not.toHaveBeenCalled();
});

test("renaming refreshes the cached workspace Info heading", async () => {
  await show();
  fireEvent.contextMenu(workspace("Alpha"));
  fireEvent.click(screen.getByRole("menuitem", { name: "Info" }));
  await screen.findByRole("dialog", { name: "Alpha info" });
  fireEvent.click(screen.getByRole("button", { name: "Close info" }));
  mocks.listWorkspaces.mockResolvedValue(workspaces.map((stream) => stream.id === "ws-a" ? { ...stream, title: "Renamed" } : stream));
  mocks.workspaceInfo.mockResolvedValue({ ...workspaces[1], title: "Renamed", canvases: [] });
  fireEvent.doubleClick(workspace("Alpha"));
  const name = screen.getByRole("textbox", { name: "Workspace name" });
  fireEvent.change(name, { target: { value: "Renamed" } });
  fireEvent.keyDown(name, { key: "Enter" });
  const renamed = await screen.findByRole("button", { name: "Renamed" });
  fireEvent.contextMenu(renamed);
  fireEvent.click(screen.getByRole("menuitem", { name: "Info" }));
  await screen.findByRole("dialog", { name: "Renamed info" });
  expect(mocks.workspaceInfo).toHaveBeenCalledTimes(2);
});

test("a workspace's tab carries a dot while a chat in it waits on the person, and the others do not", async () => {
  const strip = await show([...canvases, canvas("b-waiting", "ws-b", "2026-10-05T00:00:00Z", { state: "requires_action" }),
    canvas("scratch-hidden", null, "2026-10-05T00:00:00Z", { state: "requires_action", hidden: true }),
    canvas("a-quiet", "ws-a", "2026-10-05T00:00:00Z", { state: "running" })]);
  const dots = [...strip.querySelectorAll<HTMLButtonElement>(".workspace-title")].map((tab) => [tab.getAttribute("aria-label"), tab.querySelector(".workspace-waiting")?.getAttribute("title") ?? null]);
  // A chat the person hid is not one they are waited on in.
  expect(dots).toEqual([["Scratch", null], ["Alpha", null], ["Beta", "A chat in Beta is waiting for your answer"]]);
});

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { createDefaultWorkspace, openWorkspaceEvents, type WorkspaceEventListener, type WorkspaceState } from "../api/workspaces";
import { recentTimings, resetTimings } from "../debug/timings";
import { META } from "../detail/testing";
import { LiveContext } from "../live";
import { LiveHub as LiveHubClass, type LiveHub } from "../live/hub";
import { useChatFeed } from "../visuals/chatFeed";
import { MetaContext } from "./boot";
import { acceptWorkspace, CanvasStream, newerWorkspace } from "./canvasStream";
import { useHighlighted } from "./highlights";

vi.mock("../api/workspaces", () => ({ createDefaultWorkspace: vi.fn(), openWorkspaceEvents: vi.fn() }));

const workspace: WorkspaceState = { id: "w1", revision: 3, focused_instance: null, visuals: [] };

const hub = { change: vi.fn(), open: vi.fn(), drop: vi.fn(), refuse: vi.fn() };
let client: QueryClient;
const close = vi.fn();

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  vi.mocked(createDefaultWorkspace).mockResolvedValue(workspace);
  vi.mocked(openWorkspaceEvents).mockReturnValue(close);
  resetTimings();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  history.replaceState(null, "", "#/");
});

function Probe() {
  const { state } = useChatFeed();
  return <output>{JSON.stringify([state.openVersion, state.request])}</output>;
}

function show(enabled: boolean, child: React.ReactNode = <Probe />) {
  const ui = (on: boolean, inner: React.ReactNode) => <QueryClientProvider client={client}>
    <LiveContext value={hub as unknown as LiveHub}><MetaContext value={META}>
      <CanvasStream enabled={on}>{inner}</CanvasStream></MetaContext></LiveContext></QueryClientProvider>;
  const view = render(ui(enabled, child));
  return { view, again: (on: boolean, inner: React.ReactNode = child) => view.rerender(ui(on, inner)) };
}

async function listener(): Promise<WorkspaceEventListener> {
  await waitFor(() => expect(openWorkspaceEvents).toHaveBeenCalled(), { interval: 1 });
  return vi.mocked(openWorkspaceEvents).mock.calls.at(-1)![1];
}

test("a user with the canvas off creates no workspace and opens no stream", async () => {
  show(false);
  await new Promise((resolve) => setTimeout(resolve, 10));
  expect(createDefaultWorkspace).not.toHaveBeenCalled();
  expect(openWorkspaceEvents).not.toHaveBeenCalled();
});

test("with the canvas on, one stream opens on the user's workspace, and closes when the canvas goes off", async () => {
  const { again } = show(true);
  await listener();
  expect(openWorkspaceEvents).toHaveBeenCalledWith("w1", expect.any(Object));
  expect(openWorkspaceEvents).toHaveBeenCalledOnce();
  again(false);
  expect(close).toHaveBeenCalledOnce();
});

test("the stream outlives what it wraps: the canvas can unmount (Settings) and it stays open", async () => {
  const { again } = show(true, <Probe />);
  await listener();
  again(true, <p>Settings</p>);
  expect(screen.getByText("Settings")).toBeTruthy();
  expect(close).not.toHaveBeenCalled();
  expect(openWorkspaceEvents).toHaveBeenCalledOnce();
});

test("the live layer takes its inquiry ids and its gaps from the stream", async () => {
  show(true);
  const stream = await listener();
  stream.open();
  stream.changed("row-1", 1);
  stream.drop();
  stream.refuse();
  expect(hub.open).toHaveBeenCalledOnce();
  expect(hub.change).toHaveBeenCalledWith("row-1");
  expect(hub.drop).toHaveBeenCalledOnce();
  expect(hub.refuse).toHaveBeenCalledOnce();
});

test("a workspace frame is applied under both keys, and an older one never replaces a newer", async () => {
  show(true);
  const stream = await listener();
  act(() => stream.workspace({ ...workspace, revision: 6 }, 10, null));
  act(() => stream.workspace({ ...workspace, revision: 4, focused_instance: "stale" }, 11, null));
  for (const key of [["workspace", "default"], ["workspace", "w1"]]) {
    expect(client.getQueryData<WorkspaceState>(key)).toMatchObject({ revision: 6, focused_instance: null });
  }
  expect(recentTimings().map((frame) => [frame.frame, frame.revision, frame.t])).toEqual([["workspace", 6, 10], ["workspace", 4, 11]]);
});

test("an agent's navigation moves the page when the router knows the route, and is recorded", async () => {
  show(true);
  const stream = await listener();
  const record = "61d3a095-c7f1-4d27-a4c4-a5b1c218a31e";
  act(() => stream.navigate(`#/lookup/${record}`, 20));
  expect(window.location.hash).toBe(`#/lookup/${record}`);
  act(() => stream.navigate("#/list/Issue", 21));
  expect(window.location.hash).toBe("#/list/Issue");
  for (const route of ["#/nowhere", "#/list/NoSuchKind", "lookup/x", "#/ref/Issue/notanumber"]) {
    act(() => stream.navigate(route, 22));
    expect(window.location.hash).toBe("#/list/Issue");
  }
  expect(recentTimings().map((frame) => [frame.frame, frame.route, frame.t])).toEqual([
    ["navigate", `#/lookup/${record}`, 20], ["navigate", "#/list/Issue", 21],
  ]);
});

test("a navigation to where the page already is changes nothing", async () => {
  history.replaceState(null, "", "#/list/Issue");
  show(true);
  const stream = await listener();
  // Let a hash change an earlier test made arrive first.
  await new Promise((resolve) => setTimeout(resolve, 20));
  const listen = vi.fn();
  window.addEventListener("hashchange", listen);
  act(() => stream.navigate("#/list/Issue", 1));
  await new Promise((resolve) => setTimeout(resolve, 5));
  expect(listen).not.toHaveBeenCalled();
  window.removeEventListener("hashchange", listen);
});

function Aside() {
  return <output data-testid="aside">{useChatFeed().state.aside}</output>;
}

test("an agent that moves the page or shows a visual sends Chat aside; nothing else does", async () => {
  show(true, <Aside />);
  const stream = await listener();
  const aside = () => screen.getByTestId("aside").textContent;
  const shown: WorkspaceState = { ...workspace, revision: 4, visuals: [
    { id: "graph", type: "trax.subgraph", version: 1 }, { id: "chat", type: "trax.chat", version: 1 }] };
  // The owner's own change, an agent's show of Chat itself, and a route the router refuses.
  act(() => stream.workspace(shown, 1, null));
  act(() => stream.workspace({ ...shown, revision: 5 }, 2, "chat"));
  act(() => stream.navigate("#/nowhere", 3));
  act(() => stream.highlight(["row-1"], 4));
  expect(aside()).toBe("0");
  act(() => stream.workspace({ ...shown, revision: 6 }, 5, "graph"));
  expect(aside()).toBe("1");
  act(() => stream.navigate("#/list/Issue", 6));
  expect(aside()).toBe("2");
});

function Marks() {
  return <output data-testid="marks">{[...useHighlighted()].join(",")}</output>;
}

test("a highlight frame is held for the tab: the newest wins, an empty list clears, and a closed stream leaves none", async () => {
  const { again } = show(true, <Marks />);
  const stream = await listener();
  const marks = () => screen.getByTestId("marks").textContent;
  expect(marks()).toBe("");
  act(() => stream.highlight(["a", "b"], 1));
  expect(marks()).toBe("a,b");
  act(() => stream.highlight(["c"], 2));
  expect(marks()).toBe("c");
  act(() => stream.highlight([], 3));
  expect(marks()).toBe("");
  act(() => stream.highlight(["d"], 4));
  again(false);
  expect(marks()).toBe("");
});

test("a highlight moves neither the page nor the canvas", async () => {
  history.replaceState(null, "", "#/list/Issue");
  show(true, <Marks />);
  const stream = await listener();
  act(() => stream.highlight(["a"], 1));
  expect(window.location.hash).toBe("#/list/Issue");
  expect(client.getQueryData(["workspace", "w1"])).toBeUndefined();
  expect(recentTimings()).toEqual([]);
});

test("a chat has no frames of its own: its session's change reaches the live layer like any inquiry's", async () => {
  show(true);
  const stream = await listener();
  const invalidate = vi.spyOn(client, "invalidateQueries");
  act(() => stream.changed("session-1", 1));
  act(() => stream.changed("session-1", 2));
  expect(hub.change).toHaveBeenCalledTimes(2);
  expect(hub.change).toHaveBeenCalledWith("session-1");
  expect(invalidate).not.toHaveBeenCalled();
  expect(Object.keys(stream).toSorted()).toEqual(["changed", "drop", "highlight", "navigate", "open", "refuse", "workspace"]);
});

test("the stream's state drives the live layer's paused bar: down for 10 s pauses it, an open clears it", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  const real = new LiveHubClass(client);
  const live = { change: real.change, open: real.open, drop: real.drop, refuse: real.refuse };
  vi.mocked(createDefaultWorkspace).mockResolvedValue(workspace);
  render(<QueryClientProvider client={client}><LiveContext value={live as unknown as LiveHub}>
    <CanvasStream enabled><Probe /></CanvasStream></LiveContext></QueryClientProvider>);
  await vi.waitFor(() => expect(openWorkspaceEvents).toHaveBeenCalled());
  const stream = vi.mocked(openWorkspaceEvents).mock.calls.at(-1)![1];
  stream.open();
  expect(real.status()).toBe("connected");
  stream.drop();
  vi.advanceTimersByTime(10_000);
  expect(real.status()).toBe("paused");
  stream.open();
  expect(real.status()).toBe("connected");
  vi.useRealTimers();
});

test("newerWorkspace keeps the newer revision, a tie going to the incoming one", () => {
  const older = { ...workspace, revision: 2 };
  expect(newerWorkspace(workspace, older)).toBe(workspace);
  expect(newerWorkspace(older, workspace)).toBe(workspace);
  expect(newerWorkspace(undefined, older)).toBe(older);
  const tie = { ...workspace };
  expect(newerWorkspace(workspace, tie)).toBe(tie);
});

test("acceptWorkspace fills both keys", () => {
  acceptWorkspace(client, workspace);
  expect(client.getQueryData(["workspace", "default"])).toBe(workspace);
  expect(client.getQueryData(["workspace", "w1"])).toBe(workspace);
});

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { useContext, useEffect } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { stubFetch } from "../api/testing";
import { answerCapture, createDefaultCanvas, openCanvasEvents, type CanvasEventListener, type CanvasState } from "../api/canvases";
import { captureStage } from "../canvasCapture/captureStage";
import { CaptureContext } from "../canvasCapture/stageRegistry";
import { recentTimings, resetTimings } from "../debug/timings";
import { META } from "../detail/testing";
import { LiveContext } from "../live";
import { LiveHub as LiveHubClass, type LiveHub } from "../live/hub";
import { preferences } from "../preferences";
import { currentRoute } from "../router/addressBar";
import { currentChatColumn, setChatFullscreen, setChatSide } from "../visuals/chatColumnState";
import { ChatFeedContext, useChatFeed } from "../visuals/chatFeed";
import { MetaContext } from "./boot";
import { resetPreferences } from "../state/testing";
import { setCanvas } from "../canvases/store";
import { CanvasStream } from "./canvasStream";
import { useHighlighted } from "./highlights";

vi.mock("../api/canvases", () => ({ createDefaultCanvas: vi.fn(), openCanvasEvents: vi.fn(), answerCapture: vi.fn() }));
vi.mock("../canvasCapture/captureStage", () => ({ captureStage: vi.fn() }));

const canvas: CanvasState = { id: "w1", name: "unnamed-canvas#1", conversation_id: null, page: null, revision: 3, focused_instance: null, visuals: [] };

const hub = { change: vi.fn(), open: vi.fn(), drop: vi.fn(), refuse: vi.fn() };
let client: QueryClient;
const close = vi.fn();

beforeEach(() => {
  setCanvas(null);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  vi.mocked(createDefaultCanvas).mockResolvedValue(canvas);
  vi.mocked(openCanvasEvents).mockReturnValue(close);
  // A choice of Chat's side is kept in the preferences: the server takes it.
  stubFetch(async (request) => Response.json({ ...preferences(), ...(await request.json()) }));
  resetTimings();
});

afterEach(async () => {
  cleanup();
  vi.clearAllMocks();
  history.replaceState(null, "", "#/");
  // A save still on its way would make the next test's page ignore a preferences frame.
  await resetPreferences();
  vi.unstubAllGlobals();
});

function Probe() {
  const { state } = useChatFeed();
  return <output role="status">{JSON.stringify(state.request)}</output>;
}

function show(enabled: boolean, child: React.ReactNode = <Probe />) {
  const ui = (on: boolean, inner: React.ReactNode) => <QueryClientProvider client={client}>
    <LiveContext value={hub as unknown as LiveHub}><MetaContext value={META}>
      <CanvasStream enabled={on}>{inner}</CanvasStream></MetaContext></LiveContext></QueryClientProvider>;
  const view = render(ui(enabled, child));
  return { view, again: (on: boolean, inner: React.ReactNode = child) => view.rerender(ui(on, inner)) };
}

async function listener(): Promise<CanvasEventListener> {
  await waitFor(() => expect(openCanvasEvents).toHaveBeenCalled(), { interval: 1 });
  return vi.mocked(openCanvasEvents).mock.calls.at(-1)![1];
}

test("a user with the canvas off creates no canvas and opens no stream", async () => {
  show(false);
  await new Promise((resolve) => setTimeout(resolve, 10));
  expect(createDefaultCanvas).not.toHaveBeenCalled();
  expect(openCanvasEvents).not.toHaveBeenCalled();
});

test("with the canvas on, one stream opens on the user's canvas, and closes when the canvas goes off", async () => {
  const { again } = show(true);
  await listener();
  expect(openCanvasEvents).toHaveBeenCalledWith("w1", expect.any(Object));
  expect(openCanvasEvents).toHaveBeenCalledOnce();
  again(false);
  expect(close).toHaveBeenCalledOnce();
});

test("the stream outlives what it wraps: the canvas can unmount (Settings) and it stays open", async () => {
  const { again } = show(true, <Probe />);
  await listener();
  again(true, <p>Settings</p>);
  expect(screen.getByText("Settings")).toBeTruthy();
  expect(close).not.toHaveBeenCalled();
  expect(openCanvasEvents).toHaveBeenCalledOnce();
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

test("a canvas frame is applied under its canvas's key, and an older one never replaces a newer", async () => {
  show(true);
  const stream = await listener();
  act(() => stream.canvas({ ...canvas, revision: 6 }, 10));
  act(() => stream.canvas({ ...canvas, revision: 4, focused_instance: "stale" }, 11));
  expect(client.getQueryData<CanvasState>(["canvas", "w1"])).toMatchObject({ revision: 6, focused_instance: null });
  expect(recentTimings().map((frame) => [frame.frame, frame.revision, frame.t])).toEqual([["canvas", 6, 10], ["canvas", 4, 11]]);
});

test("an agent's navigation moves the page when the router knows the route, and is recorded", async () => {
  show(true);
  const stream = await listener();
  const record = "61d3a095-c7f1-4d27-a4c4-a5b1c218a31e";
  act(() => stream.navigate(`#/lookup/${record}`, 20));
  expect(currentRoute()).toBe(`#/lookup/${record}`);
  act(() => stream.navigate("#/list/Issue", 21));
  expect(currentRoute()).toBe("#/list/Issue");
  for (const route of ["#/nowhere", "#/list/NoSuchKind", "lookup/x", "#/ref/Issue/notanumber"]) {
    act(() => stream.navigate(route, 22));
    expect(currentRoute()).toBe("#/list/Issue");
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

test("the stream follows the canvas: switching to another closes it and opens one on the new canvas", async () => {
  show(true);
  await listener();
  expect(openCanvasEvents).toHaveBeenCalledWith("w1", expect.any(Object));
  client.setQueryData(["canvas", "w2"], { ...canvas, id: "w2" });
  act(() => setCanvas("w2"));
  await waitFor(() => expect(openCanvasEvents).toHaveBeenCalledWith("w2", expect.any(Object)), { interval: 1 });
  expect(close).toHaveBeenCalledOnce();
  expect(openCanvasEvents).toHaveBeenCalledTimes(2);
});

test("a chat frame hides or shows Chat's column in this tab; a hide leaves full screen, a show leaves the side", async () => {
  setChatSide("right");
  setChatFullscreen(true);
  show(true);
  const stream = await listener();
  act(() => stream.chat(true, 1));
  expect(currentChatColumn()).toEqual({ side: "right", hidden: true, fullscreen: false });
  act(() => stream.chat(false, 2));
  expect(currentChatColumn()).toEqual({ side: "right", hidden: false, fullscreen: false });
  // A frame the tab already agrees with changes nothing.
  const before = currentChatColumn();
  act(() => stream.chat(false, 3));
  expect(currentChatColumn()).toBe(before);
});

test("a preferences frame, from the user's other tab or the canvas's agent, is what the page shows", async () => {
  show(true);
  const stream = await listener();
  act(() => stream.preferences({ ...preferences(), theme: "light", chat_side: "right" }, 1));
  expect(preferences()).toMatchObject({ theme: "light", chat_side: "right" });
  expect(currentChatColumn().side).toBe("right");
});

function Feed() {
  return <output data-testid="feed">{useContext(ChatFeedContext) ? "feed" : "none"}</output>;
}

test("an agent that moves the page or shows a visual leaves Chat where it is: the stream holds no state of Chat's", async () => {
  show(true, <Probe />);
  const stream = await listener();
  const feed = () => screen.getByRole("status").textContent;
  const before = feed();
  const shown: CanvasState = { ...canvas, revision: 4, visuals: [{ id: "graph", type: "trax.subgraph", version: 1 }] };
  act(() => stream.canvas(shown, 1));
  act(() => stream.navigate("#/list/Issue", 2));
  expect(feed()).toBe(before);
});

test("the shell's Chat feed is there for a user with the canvas on, and for no one else", async () => {
  const { again } = show(true, <Feed />);
  expect(screen.getByTestId("feed").textContent).toBe("feed");
  again(false);
  expect(screen.getByTestId("feed").textContent).toBe("none");
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
  expect(currentRoute()).toBe("#/list/Issue");
  expect(client.getQueryData(["canvas", "w1"])).toBe(canvas);
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
  expect(Object.keys(stream).toSorted()).toEqual(["canvas", "capture", "changed", "chat", "drop", "highlight", "navigate", "open", "preferences", "refuse"]);
});

test("the stream's state drives the live layer's paused bar: down for 10 s pauses it, an open clears it", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  const real = new LiveHubClass(client);
  const live = { change: real.change, open: real.open, drop: real.drop, refuse: real.refuse };
  vi.mocked(createDefaultCanvas).mockResolvedValue(canvas);
  render(<QueryClientProvider client={client}><LiveContext value={live as unknown as LiveHub}>
    <CanvasStream enabled><Probe /></CanvasStream></LiveContext></QueryClientProvider>);
  await vi.waitFor(() => expect(openCanvasEvents).toHaveBeenCalled());
  const stream = vi.mocked(openCanvasEvents).mock.calls.at(-1)![1];
  stream.open();
  expect(real.status()).toBe("connected");
  stream.drop();
  vi.advanceTimersByTime(10_000);
  expect(real.status()).toBe("paused");
  stream.open();
  expect(real.status()).toBe("connected");
  vi.useRealTimers();
});

/** A canvas, as far as the stream knows it: it offers a stage to the registry while mounted. */
function OpenCanvas() {
  const registry = useContext(CaptureContext);
  useEffect(() => {
    registry?.set({ stage: document.createElement("div") });
    return () => registry?.set(null);
  }, [registry]);
  return null;
}

test("an agent's request for a picture is answered with the open canvas, under the request's id", async () => {
  const picture = { error: "stands in for a picture" };
  vi.mocked(captureStage).mockResolvedValue(picture);
  show(true, <OpenCanvas />);
  const stream = await listener();
  stream.capture("req-1", 40);
  await waitFor(() => expect(answerCapture).toHaveBeenCalledWith("w1", "req-1", picture), { interval: 1 });
  expect(captureStage).toHaveBeenCalledOnce();
});

test("a request for a picture while the canvas is not on screen is answered with why not", async () => {
  show(true, <p>Settings</p>);
  const stream = await listener();
  stream.capture("req-2", 41);
  await waitFor(() => expect(answerCapture).toHaveBeenCalledWith("w1", "req-2", { error: expect.stringContaining("No canvas is on screen") }), { interval: 1 });
  expect(captureStage).not.toHaveBeenCalled();
});

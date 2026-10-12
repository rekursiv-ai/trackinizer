import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { listStations, setStationLayout, setStationPage, type StationSummary } from "../api/stations";
import { createDefaultWorkspace, getWorkspace, type WorkspaceState } from "../api/workspaces";
import { CommandRegistry, CommandRegistryContext, Shortcuts, useCommandList } from "../commands/registry";
import { setStationMenu, useStationMenu } from "../stations/actions";
import { currentStation, setStation } from "../stations/store";
import { ChatColumn } from "./ChatColumn";
import { currentChatColumn, setChatFullscreen, setChatHidden, setChatSide } from "./chatColumn";
import { FOLD_AFTER_MS, OPEN_AFTER_MS } from "./hoverOpen";
import { flushLayouts, forgetLayouts, type Layout } from "./stationLayout";

vi.mock("../api/workspaces", () => ({ createDefaultWorkspace: vi.fn(), getWorkspace: vi.fn() }));
vi.mock("../api/stations", () => ({ listStations: vi.fn(), setStationPage: vi.fn(), setStationLayout: vi.fn(), createStation: vi.fn() }));
// Chat itself is tested in Chat.test.tsx; here it is a panel with a box.
vi.mock("./Chat", () => ({
  Chat: ({ workspace }: { workspace: WorkspaceState | null }) => (
    <section aria-label="Chat"><span>{workspace?.id ?? "no workspace"}</span><textarea aria-label="Message" /></section>
  ),
}));

const SIZES_KEY = "trackinizer.v2.canvas.sizes";
const workspace: WorkspaceState = { id: "w1", name: "unnamed-workstream#1", conversation_id: null, page: null, revision: 1, focused_instance: null, visuals: [] };

/** The layout keys Chat has written to its station so far, later writes over earlier ones. */
function written(): Layout {
  flushLayouts();
  return Object.assign({}, ...vi.mocked(setStationLayout).mock.calls.map(([, patch]) => patch)) as Layout;
}

beforeEach(() => {
  vi.mocked(createDefaultWorkspace).mockResolvedValue(workspace);
  vi.mocked(listStations).mockResolvedValue([]);
  vi.mocked(setStationPage).mockResolvedValue(undefined);
  vi.mocked(setStationLayout).mockResolvedValue(undefined);
  Element.prototype.setPointerCapture = vi.fn();
});

afterEach(() => {
  setStation(null);
  forgetLayouts();
  setStationMenu(false);
  history.replaceState(null, "", "#/");
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  localStorage.clear();
  setChatSide("left");
  setChatHidden(false);
  setChatFullscreen(false);
});

function Palette() {
  return <output aria-label="Palette">{useCommandList().filter((command) => command.section === "Chat").map((command) => command.title).join("|")}</output>;
}

async function show(state: WorkspaceState = workspace) {
  vi.mocked(createDefaultWorkspace).mockResolvedValue(state);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><CommandRegistryContext value={new CommandRegistry()}>
    <Shortcuts />
    <Palette />
    <ChatColumn><main>Workbench</main></ChatColumn>
  </CommandRegistryContext></QueryClientProvider>);
  await screen.findByText("w1");
  return {
    user: userEvent.setup(),
    row: document.querySelector<HTMLElement>(".chat-row")!,
    column: document.querySelector<HTMLElement>(".chat-column")!,
    workbench: document.querySelector<HTMLElement>(".chat-workbench")!,
  };
}

/** What stands in the row, in order. */
const order = (row: HTMLElement) => [...row.children].map((child) => child.className.split(" ")[0]);

test("Chat is a column beside the workbench, on the left, and has the user's workspace", async () => {
  const { row, column, workbench } = await show();
  expect(order(row)).toEqual(["chat-column", "chat-column-edge", "chat-workbench"]);
  expect(column.contains(screen.getByRole("region", { name: "Chat" }))).toBe(true);
  expect(workbench.contains(screen.getByText("Workbench"))).toBe(true);
  // Never set by the user: the stylesheet's `min(33.333vw, 80ch + padding)` stands.
  expect(column.style.getPropertyValue("--chat-width")).toBe("");
});

test("on the right Chat stands after the workbench, with the divider between them", async () => {
  setChatSide("right");
  const { row } = await show();
  expect(order(row)).toEqual(["chat-workbench", "chat-column-edge", "chat-column"]);
});

test("the divider resizes by pointer, never under 280 px or over seven tenths of the row, and the width is kept", async () => {
  const { row, column } = await show();
  vi.spyOn(row, "getBoundingClientRect").mockReturnValue({ left: 100, right: 1100, width: 1000 } as DOMRect);
  Object.defineProperty(column, "offsetWidth", { configurable: true, get: () => Number.parseInt(column.style.getPropertyValue("--chat-width") || "400", 10) });
  const edge = screen.getByRole("separator", { name: "Resize Chat" });
  fireEvent.pointerDown(edge, { pointerId: 1, clientX: 500, clientY: 300 });
  fireEvent.pointerMove(edge, { pointerId: 1, clientX: 600, clientY: 300 });
  expect(column.style.getPropertyValue("--chat-width")).toBe("500px");
  fireEvent.pointerMove(edge, { pointerId: 1, clientX: 110, clientY: 300 });
  expect(column.style.getPropertyValue("--chat-width")).toBe("280px");
  fireEvent.pointerMove(edge, { pointerId: 1, clientX: 1090, clientY: 300 });
  expect(column.style.getPropertyValue("--chat-width")).toBe("700px");
  fireEvent.pointerMove(edge, { pointerId: 1, clientX: 540, clientY: 300 });
  fireEvent.pointerUp(edge, { pointerId: 1, clientX: 540, clientY: 300 });
  // Kept in `ch`, here 8 px each: 55 of them.
  expect(written().chat_ch).toBe(55);
  expect(edge.getAttribute("aria-valuenow")).toBe("440");
});

test("on the right a drag toward the workbench widens Chat", async () => {
  setChatSide("right");
  const { row, column } = await show();
  vi.spyOn(row, "getBoundingClientRect").mockReturnValue({ left: 0, right: 1000, width: 1000 } as DOMRect);
  const edge = screen.getByRole("separator", { name: "Resize Chat" });
  fireEvent.pointerDown(edge, { pointerId: 1, clientX: 700, clientY: 300 });
  fireEvent.pointerMove(edge, { pointerId: 1, clientX: 650, clientY: 300 });
  expect(column.style.getPropertyValue("--chat-width")).toBe("350px");
  fireEvent.pointerUp(edge, { pointerId: 1, clientX: 650, clientY: 300 });
  expect(written().chat_ch).toBe(43.75);
});

test("the arrow keys move the divider 16 px, 48 with Shift, toward the side the arrow points", async () => {
  const { row, column } = await show();
  Object.defineProperty(row, "clientWidth", { configurable: true, value: 1000 });
  Object.defineProperty(column, "offsetWidth", { configurable: true, get: () => Number.parseInt(column.style.getPropertyValue("--chat-width") || "400", 10) });
  const edge = screen.getByRole("separator", { name: "Resize Chat" });
  fireEvent.keyDown(edge, { key: "ArrowRight" });
  expect(column.style.getPropertyValue("--chat-width")).toBe("416px");
  fireEvent.keyDown(edge, { key: "ArrowLeft", shiftKey: true });
  expect(column.style.getPropertyValue("--chat-width")).toBe("368px");
  for (let press = 0; press < 5; press += 1) fireEvent.keyDown(edge, { key: "ArrowLeft", shiftKey: true });
  expect(column.style.getPropertyValue("--chat-width")).toBe("280px");
  expect(written().chat_ch).toBe(35);
});

test("the width the station keeps, in ch, is the width Chat opens at", async () => {
  const { column } = await show({ ...workspace, layout: { chat_ch: 64 } });
  expect(column.style.getPropertyValue("--chat-width")).toBe("512px");
  expect(written()).toEqual({});
});

test("a row too narrow for the stored width shows Chat fitted to seven tenths of it, and writes nothing", async () => {
  const { column, row } = await show({ ...workspace, layout: { chat_ch: 200 } });
  Object.defineProperty(row, "clientWidth", { configurable: true, value: 400 });
  fireEvent(window, new Event("resize"));
  expect(column.style.getPropertyValue("--chat-width")).toBe("280px");
  Object.defineProperty(row, "clientWidth", { configurable: true, value: 1000 });
  fireEvent(window, new Event("resize"));
  expect(column.style.getPropertyValue("--chat-width")).toBe("700px");
  expect(written()).toEqual({});
});

test("a browser that kept Chat's width in pixels hands it to the first station it opens, in ch, and forgets it", async () => {
  localStorage.setItem(SIZES_KEY, JSON.stringify({ chat: 512, side: 400, floating: {}, share: {} }));
  const { column } = await show();
  expect(column.style.getPropertyValue("--chat-width")).toBe("512px");
  expect(written()).toEqual({ chat_ch: 64 });
  expect(JSON.parse(localStorage.getItem(SIZES_KEY)!)).toEqual({ side: 400, floating: {}, share: {} });
});

test("a station that already holds Chat's width keeps it over the pixels the browser kept", async () => {
  localStorage.setItem(SIZES_KEY, JSON.stringify({ chat: 512 }));
  const { column } = await show({ ...workspace, layout: { chat_ch: 50 } });
  expect(column.style.getPropertyValue("--chat-width")).toBe("400px");
  expect(written()).toEqual({});
  expect(localStorage.getItem(SIZES_KEY)).toBeNull();
});

test("the shortcut hides Chat to a strip at its edge, even from the box, and the strip shows it again", async () => {
  const { user, row, column } = await show();
  await user.click(screen.getByRole("textbox", { name: "Message" }));
  await user.keyboard("{Control>}\\{/Control}");
  expect(column.hidden).toBe(true);
  expect(currentChatColumn().hidden).toBe(true);
  expect(order(row)).toEqual(["chat-column", "chat-strip", "chat-workbench"]);
  // Still mounted: its draft and its stream stay.
  expect(document.contains(column)).toBe(true);
  await user.click(screen.getByRole("button", { name: "Show Chat" }));
  expect(column.hidden).toBe(false);
  expect(screen.queryByRole("button", { name: "Show Chat" })).toBeNull();
});

test("the strip stands at the edge Chat stands at", async () => {
  setChatSide("right");
  setChatHidden(true);
  const { row } = await show();
  expect(order(row)).toEqual(["chat-workbench", "chat-strip", "chat-column"]);
});

test("Alt+Enter takes Chat full screen, with the workbench kept and hidden; Alt+Enter or Esc returns it", async () => {
  const { user, column, workbench } = await show();
  await user.click(screen.getByRole("textbox", { name: "Message" }));
  await user.keyboard("{Alt>}{Enter}{/Alt}");
  expect(currentChatColumn().fullscreen).toBe(true);
  expect(workbench.hidden).toBe(true);
  expect(column.hidden).toBe(false);
  expect(document.contains(workbench)).toBe(true);
  expect(screen.queryByRole("separator", { name: "Resize Chat" })).toBeNull();
  await user.keyboard("{Alt>}{Enter}{/Alt}");
  expect(currentChatColumn().fullscreen).toBe(false);
  expect(workbench.hidden).toBe(false);
  await user.keyboard("{Alt>}{Enter}{/Alt}");
  await user.keyboard("{Escape}");
  expect(currentChatColumn().fullscreen).toBe(false);
  expect(workbench.hidden).toBe(false);
});

test("Esc returns from full screen when the keyboard is outside Chat's box, and does nothing otherwise", async () => {
  const { user } = await show();
  await user.keyboard("{Escape}");
  expect(currentChatColumn().fullscreen).toBe(false);
  act(() => setChatFullscreen(true));
  (document.activeElement as HTMLElement | null)?.blur();
  await user.keyboard("{Escape}");
  expect(currentChatColumn().fullscreen).toBe(false);
});

test("the palette lists Chat's commands, and a hidden Chat offers to show it", async () => {
  await show();
  expect(screen.getByLabelText("Palette").textContent).toBe("Hide Chat|Chat full screen|Move Chat to the other side");
  act(() => setChatHidden(true));
  expect(screen.getByLabelText("Palette").textContent).toBe("Show Chat|Chat full screen|Move Chat to the other side");
});

test("the divider tells the width the stylesheet gave Chat before the user sets one", async () => {
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(431);
  await show();
  expect(screen.getByRole("separator", { name: "Resize Chat" }).getAttribute("aria-valuenow")).toBe("431");
});

/** Chat hidden, its strip under the pointer's reach, and timers the test advances. */
async function hidden() {
  setChatHidden(true);
  const shown = await show();
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  const strip = screen.getByRole("button", { name: "Show Chat" });
  const wait = (ms: number) => act(() => { vi.advanceTimersByTime(ms); });
  return { ...shown, strip, wait };
}

test("a pointer resting on the hidden strip opens Chat over the workbench at its column width, and the row is left as it was", async () => {
  const { row, column, strip, wait } = await hidden();
  expect(column.hidden).toBe(true);
  fireEvent.pointerEnter(strip);
  wait(OPEN_AFTER_MS - 1);
  expect(column.hidden).toBe(true);
  wait(1);
  expect(column.hidden).toBe(false);
  expect(column.classList).toContain("chat-column-peek");
  // Over the row, not in its flow: the strip, the workbench and no divider stand as they did.
  expect(order(row)).toEqual(["chat-column", "chat-strip", "chat-workbench"]);
  expect(currentChatColumn().hidden).toBe(true);
});

test("the opened Chat folds a moment after the pointer left it, and a pointer moving from the strip onto it keeps it open", async () => {
  const { column, strip, wait } = await hidden();
  fireEvent.pointerEnter(strip);
  wait(OPEN_AFTER_MS);
  fireEvent.pointerLeave(strip);
  fireEvent.pointerEnter(column);
  wait(FOLD_AFTER_MS);
  expect(column.hidden).toBe(false);
  fireEvent.pointerLeave(column);
  wait(FOLD_AFTER_MS - 1);
  expect(column.hidden).toBe(false);
  wait(1);
  expect(column.hidden).toBe(true);
});

test("the keyboard inside the opened Chat holds it open after the pointer has left", async () => {
  const { column, strip, wait } = await hidden();
  fireEvent.pointerEnter(strip);
  wait(OPEN_AFTER_MS);
  act(() => screen.getByRole("textbox", { name: "Message" }).focus());
  fireEvent.pointerLeave(column);
  wait(FOLD_AFTER_MS * 2);
  expect(column.hidden).toBe(false);
  act(() => (document.activeElement as HTMLElement).blur());
  wait(FOLD_AFTER_MS);
  expect(column.hidden).toBe(true);
});

test("a click on the strip, or the hide key, stands Chat beside the workbench again, and hiding afterwards starts folded", async () => {
  const { row, column, strip, wait } = await hidden();
  fireEvent.pointerEnter(strip);
  wait(OPEN_AFTER_MS);
  fireEvent.click(strip);
  expect(currentChatColumn().hidden).toBe(false);
  expect(order(row)).toEqual(["chat-column", "chat-column-edge", "chat-workbench"]);
  expect(column.classList).not.toContain("chat-column-peek");
  act(() => setChatHidden(true));
  expect(column.hidden).toBe(true);
});

test("full screen Chat is never a peek, and hidden Chat that a frame shows is a column", async () => {
  const { column, strip, wait } = await hidden();
  fireEvent.pointerEnter(strip);
  wait(OPEN_AFTER_MS);
  act(() => setChatFullscreen(true));
  expect(column.classList).not.toContain("chat-column-peek");
  expect(column.hidden).toBe(false);
  act(() => setChatHidden(false));
  expect(column.classList).not.toContain("chat-column-peek");
});

const summary = (id: string, name: string, created: number): StationSummary => ({
  id, name, conversation_id: null, page: null, created_at: `2026-10-0${created}T10:00:00Z`,
});
const STATIONS = [summary("w1", "unnamed-workstream#1", 1), summary("w2", "alpha", 2), summary("w3", "beta", 3)];

test("the tab is titled by its station", async () => {
  await show();
  expect(document.title).toBe("unnamed-workstream#1 · Trackinizer");
});

test("g ] and g [ open the renamed stations in the order they were made, wrapping, and nothing else", async () => {
  vi.mocked(listStations).mockResolvedValue(STATIONS);
  vi.mocked(getWorkspace).mockImplementation(async (id) => ({ ...workspace, id, name: STATIONS.find((each) => each.id === id)!.name }));
  const { user } = await show();
  await user.keyboard("g]");
  await vi.waitFor(() => expect(currentStation().id).toBe("w2"));
  await user.keyboard("g]");
  await vi.waitFor(() => expect(currentStation().id).toBe("w3"));
  await user.keyboard("g]");
  await vi.waitFor(() => expect(currentStation().id).toBe("w2"));
  await user.keyboard("g[[");
  await vi.waitFor(() => expect(currentStation().id).toBe("w3"));
  // The station that was never renamed is in no cycle, whatever opened it.
  expect(vi.mocked(getWorkspace).mock.calls.map(([id]) => id)).toEqual(["w2", "w3", "w2", "w3"]);
});

test("g s shows Chat and opens the station menu", async () => {
  function Menu() {
    return <output aria-label="Menu">{String(useStationMenu())}</output>;
  }
  setChatHidden(true);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { user } = await (async () => {
    render(<QueryClientProvider client={client}><CommandRegistryContext value={new CommandRegistry()}>
      <Shortcuts /><Menu /><ChatColumn><main>Workbench</main></ChatColumn>
    </CommandRegistryContext></QueryClientProvider>);
    await screen.findByText("w1");
    return { user: userEvent.setup() };
  })();
  expect(screen.getByLabelText("Menu").textContent).toBe("false");
  await user.keyboard("gs");
  expect(screen.getByLabelText("Menu").textContent).toBe("true");
  expect(currentChatColumn().hidden).toBe(false);
});

test("the palette lists each station as Open station <name>, and New station", async () => {
  vi.mocked(listStations).mockResolvedValue(STATIONS);
  function Titles() {
    return <output aria-label="Stations">{useCommandList().filter((command) => command.section === "Stations").map((command) => command.title).join("|")}</output>;
  }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><CommandRegistryContext value={new CommandRegistry()}>
    <Titles /><ChatColumn><main>Workbench</main></ChatColumn>
  </CommandRegistryContext></QueryClientProvider>);
  await vi.waitFor(() => expect(screen.getByLabelText("Stations").textContent).toContain("Open station beta"));
  expect(screen.getByLabelText("Stations").textContent).toBe(
    "Next renamed station|Previous renamed station|Search stations|New station|Open station unnamed-workstream#1|Open station alpha|Open station beta");
});

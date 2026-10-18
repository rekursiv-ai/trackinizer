import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { listStations, setStationLayout, setStationPage } from "../api/stations";
import { createDefaultWorkspace, type WorkspaceState } from "../api/workspaces";
import { CommandRegistry, CommandRegistryContext, Shortcuts, useCommandList } from "../commands/registry";
import { setStation } from "../stations/store";
import { ChatColumn } from "./ChatColumn";
import { preferences, receivePreferences } from "../preferences";
import { currentChatColumn, setChatFullscreen, setChatHidden } from "./chatColumnState";
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
  history.replaceState(null, "", "#/");
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
  // As the server's word, not the user's choice: a choice would be sent, and its answer outlive the test.
  receivePreferences({ ...preferences(), chat_side: "left" });
  setChatHidden(false);
  setChatFullscreen(false);
});

function Palette() {
  return <output aria-label="Palette">{useCommandList().filter((command) => command.section === "Chat").map((command) => command.title).join("|")}</output>;
}

async function show(
  state: WorkspaceState = workspace,
  { sheet = null }: { readonly sheet?: ReactNode } = {},
) {
  vi.mocked(createDefaultWorkspace).mockResolvedValue(state);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><CommandRegistryContext value={new CommandRegistry()}>
    <Shortcuts />
    <Palette />
    <ChatColumn sheet={sheet}><main>Workbench</main></ChatColumn>
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
  receivePreferences({ ...preferences(), chat_side: "right" });
  const { row } = await show();
  expect(order(row)).toEqual(["chat-workbench", "chat-column-edge", "chat-column"]);
});

test("a sheet opens in the stage, beside the page; full screen, over the row", async () => {
  const { row } = await show(workspace, { sheet: <aside className="sheet" /> });
  const stage = () => document.querySelector<HTMLElement>(".chat-stage")!;
  expect(order(stage())).toEqual(["", "sheet"]);
  act(() => setChatFullscreen(true));
  expect(order(row)).toEqual(["chat-column", "chat-workbench", "sheet"]);
});

test("on a phone Chat is the whole app: shown even if hidden, the workbench hidden, and no strip or divider", async () => {
  vi.stubGlobal("matchMedia", (query: string) => ({ matches: query === "(max-width: 760px)", addEventListener() {}, removeEventListener() {} }));
  setChatHidden(true);
  const { row, column, workbench } = await show(workspace);
  expect(order(row)).toEqual(["chat-column", "chat-workbench"]);
  expect(column.hidden).toBe(false);
  expect(workbench.hidden).toBe(true);
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

test("a press inside the divider's gap moves Chat's edge by as much as the pointer, with no jump", async () => {
  const { row, column } = await show();
  vi.spyOn(row, "getBoundingClientRect").mockReturnValue({ left: 100, right: 1100, width: 1000 } as DOMRect);
  Object.defineProperty(column, "offsetWidth", { configurable: true, value: 400 });
  const edge = screen.getByRole("separator", { name: "Resize Chat" });
  // Chat ends at 500; the press lands 4 px into the gap beyond it.
  fireEvent.pointerDown(edge, { pointerId: 1, clientX: 504, clientY: 300 });
  fireEvent.pointerMove(edge, { pointerId: 1, clientX: 604, clientY: 300 });
  expect(column.style.getPropertyValue("--chat-width")).toBe("500px");
});

test("on the right a drag toward the workbench widens Chat", async () => {
  receivePreferences({ ...preferences(), chat_side: "right" });
  const { row, column } = await show();
  vi.spyOn(row, "getBoundingClientRect").mockReturnValue({ left: 0, right: 1000, width: 1000 } as DOMRect);
  Object.defineProperty(column, "offsetWidth", { configurable: true, value: 300 });
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

test("Chat retains legacy width through a refused migration and retries it on reload", async () => {
  const sizes = { chat: 512, side: 400, floating: {}, share: {} };
  localStorage.setItem(SIZES_KEY, JSON.stringify(sizes));
  let reject: (error: Error) => void = () => { throw new Error("No held migration"); };
  vi.mocked(setStationLayout).mockImplementationOnce(() => new Promise<void>((_resolve, refuse) => { reject = refuse; }));
  await show();
  expect(JSON.parse(localStorage.getItem(SIZES_KEY)!)).toEqual(sizes);
  await act(async () => reject(new Error("offline")));
  expect(JSON.parse(localStorage.getItem(SIZES_KEY)!)).toEqual(sizes);
  cleanup();
  forgetLayouts();
  setStation(null);
  await show();
  await vi.waitFor(() => expect(JSON.parse(localStorage.getItem(SIZES_KEY)!)).toEqual({ side: 400, floating: {}, share: {} }), { interval: 1 });
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
  receivePreferences({ ...preferences(), chat_side: "right" });
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
  expect(screen.getByLabelText("Palette").textContent).toBe("Hide Chat|Chat full screen|Move Chat to the other side|New chat");
  act(() => setChatHidden(true));
  expect(screen.getByLabelText("Palette").textContent).toBe("Show Chat|Chat full screen|Move Chat to the other side|New chat");
});

test("the palette offers New terminal, bound to no key", async () => {
  function Terminals() {
    return <output aria-label="Terminals">{useCommandList().filter((command) => command.section === "Terminal").map((command) => `${command.title}:${command.keys?.length ?? 0}`).join("|")}</output>;
  }
  await show(workspace, { sheet: <Terminals /> });
  expect(screen.getByLabelText("Terminals").textContent).toBe("New terminal:0");
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
  fireEvent.pointerMove(strip);
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
  fireEvent.pointerMove(strip);
  wait(OPEN_AFTER_MS);
  fireEvent.pointerLeave(strip);
  fireEvent.pointerMove(column);
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
  fireEvent.pointerMove(strip);
  wait(OPEN_AFTER_MS);
  act(() => screen.getByRole("textbox", { name: "Message" }).focus());
  fireEvent.pointerLeave(column);
  wait(FOLD_AFTER_MS * 2);
  expect(column.hidden).toBe(false);
  act(() => (document.activeElement as HTMLElement).blur());
  wait(FOLD_AFTER_MS);
  expect(column.hidden).toBe(true);
});

test("a click anywhere in the opened Chat pins it beside the workbench, and it stays once the pointer leaves", async () => {
  const { row, column, strip, wait } = await hidden();
  fireEvent.pointerMove(strip);
  wait(OPEN_AFTER_MS);
  fireEvent.pointerMove(column);
  fireEvent.click(screen.getByText("w1"));
  expect(currentChatColumn().hidden).toBe(false);
  expect(order(row)).toEqual(["chat-column", "chat-column-edge", "chat-workbench"]);
  expect(column.classList).not.toContain("chat-column-peek");
  fireEvent.pointerLeave(column);
  wait(FOLD_AFTER_MS * 2);
  expect(column.hidden).toBe(false);
});

test("a click in a shown Chat changes nothing", async () => {
  const { column } = await show();
  fireEvent.click(screen.getByText("w1"));
  expect(currentChatColumn().hidden).toBe(false);
  expect(column.hidden).toBe(false);
});

test("a click on the strip, or the hide key, stands Chat beside the workbench again, and hiding afterwards starts folded", async () => {
  const { row, column, strip, wait } = await hidden();
  fireEvent.pointerMove(strip);
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
  fireEvent.pointerMove(strip);
  wait(OPEN_AFTER_MS);
  act(() => setChatFullscreen(true));
  expect(column.classList).not.toContain("chat-column-peek");
  expect(column.hidden).toBe(false);
  act(() => setChatHidden(false));
  expect(column.classList).not.toContain("chat-column-peek");
});

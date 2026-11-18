import { ApiError, send, TIMEOUT_MS } from "../api/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { attachTerminal, closeTerminal, connectTerminal, listTerminalMachines, openTerminal, type LinkOptions } from "../api/terminals";
import { ProfileContext } from "../app/boot";
import { bindShortcuts } from "../commands/registry";
import { receivePreferences, preferences } from "../preferences";
import { TerminalTile, TerminalView } from "./Terminal";
import { requestNewTerminal, takeNewTerminalRequest } from "./newTerminal";
import type { RendererProps } from "./registry";

const xterm = vi.hoisted(() => ({ events: [] as string[], focused: 0, fitted: null as { rows: number; cols: number } | null, fits: 0, providers: [] as unknown[], instances: [] as Array<{ options: { fontSize?: number }; written: Array<Uint8Array | string>; data: (data: string) => void; resize: (size: { rows: number; cols: number }) => void; disposed: boolean; keys: (event: KeyboardEvent) => boolean }> }));
vi.mock("@xterm/xterm", () => ({
  Terminal: class {
    options: { fontSize?: number } = {}; rows = 24; cols = 80; written: Array<Uint8Array | string> = []; disposed = false;
    buffer = { active: { type: "normal", cursorX: 2, cursorY: 0, baseY: 0, getLine: () => undefined } };
    data: (data: string) => void = () => {};
    resize: (size: { rows: number; cols: number }) => void = () => {};
    onData(handler: (data: string) => void) { xterm.events.push("onData"); this.data = handler; }
    onResize(handler: (size: { rows: number; cols: number }) => void) { xterm.events.push("onResize"); this.resize = handler; }
    constructor() { xterm.instances.push(this); }
    loadAddon(addon: { term?: unknown }) { addon.term = this; }
    open() { xterm.events.push("open"); }
    write(bytes: Uint8Array | string) { this.written.push(bytes); }
    dispose() { this.disposed = true; }
    focus() { xterm.focused += 1; }
    attachCustomKeyEventHandler(handler: (event: KeyboardEvent) => boolean) { this.keys = handler; }
    keys: (event: KeyboardEvent) => boolean = () => true;
  },
}));
vi.mock("@xterm/addon-fit", () => ({ FitAddon: class { term?: { rows: number; cols: number }; fit() { xterm.fits += 1; xterm.events.push("fit"); if (xterm.fitted && this.term) Object.assign(this.term, xterm.fitted); } } }));
vi.mock("@xterm/addon-clipboard", () => ({ ClipboardAddon: class { constructor(_base64: unknown, provider: unknown) { xterm.providers.push(provider); } } }));
vi.mock("@xterm/xterm/css/xterm.css", () => ({}));
vi.mock("../api/terminals", () => ({
  openTerminal: vi.fn(), attachTerminal: vi.fn(), closeTerminal: vi.fn(), connectTerminal: vi.fn(), listTerminalMachines: vi.fn(),
}));

const link = { input: vi.fn(), resize: vi.fn(), stop: vi.fn(), dispose: vi.fn() };
let options: LinkOptions;

beforeEach(() => {
  xterm.instances.length = 0;
  xterm.providers.length = 0;
  xterm.fits = 0;
  xterm.focused = 0;
  xterm.events.length = 0;
  xterm.fitted = null;
  vi.mocked(closeTerminal).mockResolvedValue(undefined);
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  vi.mocked(openTerminal).mockResolvedValue({ terminal_id: "t1", ticket: "tk" });
  vi.mocked(attachTerminal).mockResolvedValue({ ticket: "tk2", ice_servers: [] });
  vi.mocked(connectTerminal).mockImplementation((given) => { options = given; return link; });
});
afterEach(() => { cleanup(); takeNewTerminalRequest(); vi.clearAllMocks(); vi.unstubAllGlobals(); });

const start = () => fireEvent.click(screen.getByRole("button", { name: /Start terminal/ }));

test("nothing opens until Start is clicked", () => {
  render(<TerminalView machine="gx10" workspace="w1" />);
  expect(openTerminal).not.toHaveBeenCalled();
  expect(xterm.instances).toHaveLength(0);
  expect(screen.queryByText(/Recorded/)).toBeNull();
});

test("Start opens on the machine from the station with the screen's size, and connects with the ticket", async () => {
  render(<TerminalView machine="gx10" workspace="w1" />);
  start();
  await waitFor(() => expect(connectTerminal).toHaveBeenCalled());
  expect(openTerminal).toHaveBeenCalledWith("gx10", "w1", 24, 80, null);
  expect(options).toMatchObject({ id: "t1", ticket: "tk" });
});

test("typing becomes input, a resize becomes a resize frame, and output is written", async () => {
  render(<TerminalView machine="gx10" workspace="w1" />);
  start();
  await waitFor(() => expect(connectTerminal).toHaveBeenCalled());
  xterm.instances[0]!.data("ls\r");
  expect([...link.input.mock.calls[0]![0] as Uint8Array]).toEqual([108, 115, 13]);
  xterm.instances[0]!.resize({ rows: 30, cols: 100 });
  expect(link.resize).toHaveBeenCalledWith(30, 100);
  const bytes = new Uint8Array([65]);
  act(() => options.onOutput(bytes));
  expect(xterm.instances[0]!.written).toEqual([bytes]);
  expect(xterm.instances[0]!.written[0]).toBe(bytes);
});

test("on a slow link what is typed is drawn dim at once; the link's echo times turn that on", async () => {
  render(<TerminalView machine="gx10" workspace="w1" />);
  start();
  await waitFor(() => expect(connectTerminal).toHaveBeenCalled());
  const screen = xterm.instances[0]!;
  screen.data("x");
  screen.data("a");
  expect(screen.written).toEqual([]);
  act(() => options.onOutput(new Uint8Array([120, 97])));
  for (let i = 0; i < 5; i++) act(() => options.onEcho!(300, "relay"));
  screen.data("b");
  expect(screen.written.at(-1)).toContain("\x1b[2mb");
  expect(link.input).toHaveBeenCalledTimes(3);
});

test("a live terminal re-attaches once on mount, with a fresh ticket, and without a click", async () => {
  render(<TerminalView machine="gx10" workspace="w1" resume={{ terminal_id: "t9" }} />);
  await waitFor(() => expect(connectTerminal).toHaveBeenCalled());
  expect(attachTerminal).toHaveBeenCalledWith("t9");
  expect(openTerminal).not.toHaveBeenCalled();
  expect(options).toMatchObject({ id: "t9", ticket: "tk2" });
});

test("4001 shows Take over, which attaches again", async () => {
  render(<TerminalView machine="gx10" workspace="w1" />);
  start();
  await waitFor(() => expect(connectTerminal).toHaveBeenCalled());
  act(() => options.onStatus({ state: "taken", note: "Another window took this terminal over." }));
  fireEvent.click(screen.getByRole("button", { name: "Take over" }));
  await waitFor(() => expect(attachTerminal).toHaveBeenCalledWith("t1"));
});

test("an ended terminal offers Start new and links no transcript", async () => {
  render(<TerminalView machine="gx10" workspace="w1" />);
  start();
  await waitFor(() => expect(connectTerminal).toHaveBeenCalled());
  act(() => options.onStatus({ state: "ended", note: "The shell exited." }));
  expect(screen.queryByRole("link", { name: "Transcript" })).toBeNull();
  expect(screen.getByRole("button", { name: "Start new" })).toBeTruthy();
});

test("a failed open shows its message and keeps Start on offer", async () => {
  vi.mocked(openTerminal).mockRejectedValue(new Error("boom"));
  render(<TerminalView machine="gx10" workspace="w1" />);
  start();
  await screen.findByRole("alert");
  expect(screen.getByRole("button", { name: /Start terminal/ })).toBeTruthy();
});

test("Ctrl+K typed in the terminal does not reach the app's shortcuts", async () => {
  const run = vi.fn();
  const unbind = bindShortcuts(window, [{ id: "palette", title: "Palette", keys: ["Control+k"], global: true, run }]);
  render(<TerminalView machine="gx10" workspace="w1" />);
  start();
  await waitFor(() => expect(connectTerminal).toHaveBeenCalled());
  const section = screen.getByRole("region", { name: "Terminal" });
  fireEvent.keyDown(section, { key: "k", code: "KeyK", ctrlKey: true });
  expect(run).not.toHaveBeenCalled();
  fireEvent.keyDown(document.body, { key: "k", code: "KeyK", ctrlKey: true });
  expect(run).toHaveBeenCalled();
  unbind();
});

test("unmounting lets go of the socket and the screen, and leaves the shell running", async () => {
  const shown = render(<TerminalView machine="gx10" workspace="w1" />);
  start();
  await waitFor(() => expect(connectTerminal).toHaveBeenCalled());
  shown.unmount();
  await waitFor(() => expect(link.dispose).toHaveBeenCalled());
  expect(link.stop).not.toHaveBeenCalled();
  expect(xterm.instances[0]!.disposed).toBe(true);
});

const ready = { name: "gx10", status: "online", terminal_ready: true, confirmed: true };
const tileInstance = (id: string) => ({ id, type: "trax.terminal", version: 1, placement: "main", record_id: null, params: { machine: "gx10" } } as RendererProps["instance"]);
// Each test names its own tile: the terminals an agent or a request started are remembered per tile id for the page.
const renderTile = (id: string) => render(<QueryClientProvider client={new QueryClient()}><TerminalTile instance={tileInstance(id)} workspace={null} onWorkspaceChanged={vi.fn()} focused={false} /></QueryClientProvider>);

test("a tile opens its terminals with its own id as their surface", async () => {
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [ready], terminals: [] });
  renderTile("v-open");
  fireEvent.click(await screen.findByRole("button", { name: "Start terminal on gx10" }));
  await waitFor(() => expect(openTerminal).toHaveBeenCalledWith("gx10", null, 24, 80, "v-open"));
});

test("a reload re-attaches each live terminal this tile opened as a tab, and no other surface's", async () => {
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [ready], terminals: [
    { terminal_id: "ta", machine: "gx10", surface: "v-reload" }, { terminal_id: "tb", machine: "gx10", surface: "v-reload" }, { terminal_id: "tc", machine: "gx10", surface: "chat" },
  ] });
  renderTile("v-reload");
  await waitFor(() => expect(attachTerminal).toHaveBeenCalledTimes(2));
  expect(vi.mocked(attachTerminal).mock.calls.map(([id]) => id).toSorted()).toEqual(["ta", "tb"]);
  expect(screen.getAllByRole("tab")).toHaveLength(2);
});

test("New terminal opens a second shell beside the first, which keeps its socket", async () => {
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [ready], terminals: [] });
  vi.mocked(openTerminal).mockResolvedValueOnce({ terminal_id: "t1", ticket: "k1" }).mockResolvedValueOnce({ terminal_id: "t2", ticket: "k2" });
  const first = { ...link, dispose: vi.fn() };
  vi.mocked(connectTerminal).mockImplementationOnce((given) => { options = given; return first; });
  renderTile("v-new");
  fireEvent.click(await screen.findByRole("button", { name: "Start terminal on gx10" }));
  await waitFor(() => expect(connectTerminal).toHaveBeenCalledTimes(1));
  fireEvent.click(screen.getByRole("button", { name: "New terminal" }));
  await waitFor(() => expect(connectTerminal).toHaveBeenCalledTimes(2));
  expect(openTerminal).toHaveBeenCalledTimes(2);
  expect(screen.getAllByRole("tab")).toHaveLength(2);
  await new Promise((resolve) => setTimeout(resolve, 10));
  expect(first.dispose).not.toHaveBeenCalled();
  expect(closeTerminal).not.toHaveBeenCalled();
});

test("closing a tab stops its shell and drops the tab; the last one is replaced by one to Start", async () => {
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [ready], terminals: [{ terminal_id: "ta", machine: "gx10", surface: "v-close" }] });
  renderTile("v-close");
  await waitFor(() => expect(connectTerminal).toHaveBeenCalled());
  fireEvent.click(screen.getByRole("button", { name: "Close gx10 1" }));
  expect(closeTerminal).toHaveBeenCalledWith("ta");
  expect(await screen.findByRole("button", { name: "Start terminal on gx10" })).toBeTruthy();
  expect(screen.getAllByRole("tab")).toHaveLength(1);
});

// A tile an agent did not name a machine for: each tab keeps the machine it opened on.
const laptop = { ...ready, name: "laptop" };
const renderOpenTile = (id: string) => render(<QueryClientProvider client={new QueryClient()}><TerminalTile instance={{ ...tileInstance(id), params: {} }} workspace={null} onWorkspaceChanged={vi.fn()} focused={false} /></QueryClientProvider>);

test("tabs are named by their machine and count per machine", async () => {
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [ready, laptop], terminals: [
    { terminal_id: "ta", machine: "gx10", surface: "v-names" }, { terminal_id: "tb", machine: "laptop", surface: "v-names" }, { terminal_id: "tc", machine: "gx10", surface: "v-names" },
  ] });
  renderOpenTile("v-names");
  await waitFor(() => expect(screen.getAllByRole("tab").map((each) => each.textContent)).toEqual(["gx10 1", "laptop 1", "gx10 2"]));
  expect(screen.queryByRole("combobox", { name: "Machine" })).toBeNull();
});

test("with two machines ready + asks which, and the new tab keeps that machine", async () => {
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [ready, laptop], terminals: [] });
  renderOpenTile("v-which");
  await screen.findByRole("button", { name: "Start terminal on gx10" });
  fireEvent.click(screen.getByRole("button", { name: "New terminal" }));
  expect(openTerminal).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "laptop" }));
  await waitFor(() => expect(openTerminal).toHaveBeenCalledWith("laptop", null, 24, 80, "v-which"));
  expect(screen.getAllByRole("tab").map((each) => each.textContent)).toEqual(["gx10 1", "laptop 1"]);
  fireEvent.click(screen.getAllByRole("tab")[0]!);
  expect(screen.getByRole("button", { name: "Start terminal on gx10" })).toBeTruthy();
});

test("with one machine ready + adds a tab at once, on that machine", async () => {
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [ready], terminals: [] });
  renderOpenTile("v-one");
  await screen.findByRole("button", { name: "Start terminal on gx10" });
  fireEvent.click(screen.getByRole("button", { name: "New terminal" }));
  await waitFor(() => expect(openTerminal).toHaveBeenCalledWith("gx10", null, 24, 80, "v-one"));
});

test("the palette's request adds a tab that starts by itself, once", async () => {
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [ready], terminals: [] });
  renderTile("v-palette");
  await screen.findByRole("button", { name: "Start terminal on gx10" });
  act(() => requestNewTerminal());
  await waitFor(() => expect(openTerminal).toHaveBeenCalledTimes(1));
  expect(screen.getAllByRole("tab")).toHaveLength(2);
  expect(takeNewTerminalRequest()).toBe(false);
});

test("a request made before the tile mounts gives it one tab that starts, not a blank one beside it", async () => {
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [ready], terminals: [] });
  requestNewTerminal();
  renderTile("v-early");
  await waitFor(() => expect(openTerminal).toHaveBeenCalledTimes(1));
  expect(screen.getAllByRole("tab")).toHaveLength(1);
});

/** The error the page gets for a refusal, read by the real client from the body the server answers with. */
async function refused(status: number, code: string, message: string): Promise<ApiError> {
  const body = { detail: { code, message } };
  const failure = await send(TIMEOUT_MS.write, undefined, async () => ({ error: body, response: new Response(JSON.stringify(body), { status }) })).catch((error: unknown) => error);
  expect(failure).toBeInstanceOf(ApiError);
  return failure as ApiError;
}

test.each([
  ["terminal_not_enabled", 409, "Start the host with `trax machine connect`: that one command turns terminals on."],
  ["terminal_limit", 429, "Too many terminals opened just now; wait a minute."],
  ["terminal_unconfirmed", 409, "Allow terminals on this machine first: open Chat's provider card and click Allow."],
  ["terminal_forbidden", 403, "Terminals are not available on a server without sign-in."],
])("an open refused with %s says what to do", async (code, status, text) => {
  vi.mocked(openTerminal).mockRejectedValue(await refused(status, code, "the server's own words"));
  render(<TerminalView machine="gx10" workspace="w1" />);
  start();
  expect((await screen.findByRole("alert")).textContent).toBe(text);
});

test("OSC 52 writes only just after the user's own key or click here, and says so", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  render(<TerminalView machine="gx10" workspace="w1" />);
  start();
  await waitFor(() => expect(connectTerminal).toHaveBeenCalled());
  const provider = xterm.providers[0] as { readText: () => string; writeText: (selection: string, text: string) => Promise<void> | undefined };
  expect(provider.readText()).toBe("");
  provider.writeText("c", "unprompted");
  expect(writeText).not.toHaveBeenCalled();
  xterm.instances[0]!.data("y");
  await provider.writeText("c", "after a key");
  expect(writeText).toHaveBeenCalledWith("after a key");
  expect(await screen.findByText("Copied from terminal")).toBeTruthy();
  vi.unstubAllGlobals();
});

test("a click in the terminal opens the same window", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  render(<TerminalView machine="gx10" workspace="w1" />);
  start();
  await waitFor(() => expect(connectTerminal).toHaveBeenCalled());
  fireEvent.pointerDown(screen.getByRole("region", { name: "Terminal" }));
  await (xterm.providers[0] as { writeText: (selection: string, text: string) => Promise<void> | undefined }).writeText("c", "after a click");
  expect(writeText).toHaveBeenCalledWith("after a click");
  vi.unstubAllGlobals();
});

test("Maximize fills the canvas with the same screen and socket, refits both ways, and restores", async () => {
  render(<TerminalView machine="gx10" workspace="w1" />);
  start();
  await waitFor(() => expect(connectTerminal).toHaveBeenCalled());
  const fitsBefore = xterm.fits;
  fireEvent.click(screen.getByRole("button", { name: "Maximize terminal" }));
  const section = screen.getByRole("region", { name: "Terminal" });
  expect(section.className).toContain("terminal-maximized");
  expect(xterm.fits).toBeGreaterThan(fitsBefore);
  const fitsMaximized = xterm.fits;
  fireEvent.click(screen.getByRole("button", { name: "Restore terminal" }));
  expect(section.className).not.toContain("terminal-maximized");
  expect(xterm.fits).toBeGreaterThan(fitsMaximized);
  expect(xterm.instances).toHaveLength(1);
  expect(connectTerminal).toHaveBeenCalledTimes(1);
  expect(attachTerminal).not.toHaveBeenCalled();
});

test("no key leaves the shell: Escape typed in a maximized terminal is not handled above it", async () => {
  const above = vi.fn();
  render(<div onKeyDown={above}><TerminalView machine="gx10" workspace="w1" /></div>);
  start();
  await waitFor(() => expect(connectTerminal).toHaveBeenCalled());
  fireEvent.click(screen.getByRole("button", { name: "Maximize terminal" }));
  fireEvent.keyDown(screen.getByRole("region", { name: "Terminal" }), { key: "Escape" });
  expect(above).not.toHaveBeenCalled();
  expect(screen.getByRole("region", { name: "Terminal" }).className).toContain("terminal-maximized");
});

test("an agent's start opens the terminal once, without a click, and says who asked", async () => {
  const { rerender } = render(<TerminalView machine="gx10" workspace="w1" autoStart="tile-1" requestedBy="scout" />);
  await waitFor(() => expect(connectTerminal).toHaveBeenCalled());
  expect(openTerminal).toHaveBeenCalledTimes(1);
  expect(screen.getByText("Opened at scout's request.")).toBeTruthy();
  rerender(<TerminalView machine="gx10" workspace="w1" autoStart="tile-1" requestedBy="scout" />);
  cleanup();
  render(<TerminalView machine="gx10" workspace="w1" autoStart="tile-1" requestedBy="scout" />);
  await Promise.resolve();
  expect(openTerminal).toHaveBeenCalledTimes(1);
});

test("a tile asked to start opens only on a ready machine of the user's, and never once a terminal is placed", async () => {
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [{ name: "gx10", status: "online", terminal_ready: false, confirmed: true }], terminals: [] });
  const instance = (params: Record<string, string | boolean>, id: string) => ({ id, type: "trax.terminal", version: 1, placement: "main", record_id: null, params }) as RendererProps["instance"];
  const wrap = (each: RendererProps["instance"]) => <QueryClientProvider client={new QueryClient()}><TerminalTile instance={each} workspace={null} onWorkspaceChanged={vi.fn()} focused={false} /></QueryClientProvider>;
  render(wrap(instance({ machine: "gx10", start: true }, "t-notready")));
  await screen.findByRole("button", { name: "Start terminal on gx10" });
  expect(openTerminal).not.toHaveBeenCalled();
  cleanup();
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [{ name: "gx10", status: "online", terminal_ready: true, confirmed: true }], terminals: [] });
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [{ name: "gx10", status: "online", terminal_ready: true, confirmed: true }], terminals: [{ terminal_id: "t1", machine: "gx10", surface: "t-placed" }] });
  render(wrap(instance({ machine: "gx10", start: true }, "t-placed")));
  await waitFor(() => expect(attachTerminal).toHaveBeenCalledWith("t1"));
  expect(openTerminal).not.toHaveBeenCalled();
  cleanup();
  render(wrap(instance({ start: true }, "t-nameless")));
  await screen.findByRole("button", { name: "Start terminal on gx10" });
  expect(openTerminal).not.toHaveBeenCalled();
  cleanup();
  render(wrap(instance({ machine: "gx10", start: true }, "t-go")));
  await waitFor(() => expect(openTerminal).toHaveBeenCalledTimes(1));
});

test("the terminal's text size follows the user's preference as it changes", async () => {
  render(<TerminalView machine="gx10" workspace="w1" />);
  start();
  await waitFor(() => expect(connectTerminal).toHaveBeenCalled());
  act(() => receivePreferences({ ...preferences(), terminal_font_size: "18px" }));
  expect(xterm.instances[0]!.options.fontSize).toBe(18);
  act(() => receivePreferences({ ...preferences(), terminal_font_size: "13px" }));
});

test("who asked is the placing user when not the viewer, the shared assistant, or the viewer's own agent", async () => {
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [{ name: "gx10", status: "online", terminal_ready: true, confirmed: true }], terminals: [] });
  const tile = (id: string, placed_by: string | null, workspace: unknown) => render(
    <QueryClientProvider client={new QueryClient()}><ProfileContext value={{ email: "ada@example.com" } as never}>
      <TerminalTile instance={{ id, type: "trax.terminal", version: 1, placement: "main", record_id: null, params: { machine: "gx10", start: true }, placed_by } as RendererProps["instance"]}
        workspace={workspace as RendererProps["workspace"]} onWorkspaceChanged={vi.fn()} focused={false} /></ProfileContext></QueryClientProvider>);
  tile("q1", "grace@example.com", null);
  expect(await screen.findByText("Opened at grace@example.com's request.")).toBeTruthy();
  cleanup();
  tile("q2", null, { id: "w1", partner: { provider: "assistant", actor: "scout" } });
  expect(await screen.findByText("Opened at scout's request.")).toBeTruthy();
  cleanup();
  tile("q3", "ada@example.com", { id: "w1", partner: null });
  expect(await screen.findByText("Opened at your agent's request.")).toBeTruthy();
});

test("handlers are registered before the first fit, and the open sends the fitted size", async () => {
  xterm.fitted = { rows: 40, cols: 120 };
  render(<TerminalView machine="gx10" workspace="w1" />);
  start();
  await waitFor(() => expect(connectTerminal).toHaveBeenCalled());
  expect(xterm.events.slice(0, 4)).toEqual(["onData", "onResize", "open", "fit"]);
  expect(openTerminal).toHaveBeenCalledWith("gx10", "w1", 40, 120, null);
  expect(options.size?.()).toEqual({ rows: 40, cols: 120 });
});

test("an unmount while the terminal is still opening closes the one that arrives, and one being attached is left running", async () => {
  let arrive: (opened: { terminal_id: string; ticket: string }) => void = () => {};
  vi.mocked(openTerminal).mockReturnValue(new Promise((resolve) => { arrive = resolve; }));
  const first = render(<TerminalView machine="gx10" workspace="w1" />);
  start();
  await waitFor(() => expect(openTerminal).toHaveBeenCalled());
  first.unmount();
  await waitFor(() => expect(xterm.instances[0]!.disposed).toBe(true));
  arrive({ terminal_id: "t5", ticket: "tk" });
  await waitFor(() => expect(closeTerminal).toHaveBeenCalledWith("t5"));
  expect(connectTerminal).not.toHaveBeenCalled();
  let attached: (opened: { ticket: string; ice_servers: string[] }) => void = () => {};
  vi.mocked(attachTerminal).mockReturnValue(new Promise((resolve) => { attached = resolve; }));
  const second = render(<TerminalView machine="gx10" workspace="w1" resume={{ terminal_id: "t9" }} />);
  await waitFor(() => expect(attachTerminal).toHaveBeenCalledWith("t9"));
  second.unmount();
  attached({ ticket: "tk", ice_servers: [] });
  await Promise.resolve();
  expect(closeTerminal).toHaveBeenCalledTimes(1);
});

test("under StrictMode an agent's start opens one terminal and ends connected, not stuck at starting", async () => {
  render(<StrictMode><TerminalView machine="gx10" workspace="w1" autoStart="strict-1" requestedBy="scout" /></StrictMode>);
  await waitFor(() => expect(connectTerminal).toHaveBeenCalled());
  expect(openTerminal).toHaveBeenCalledTimes(1);
  expect(connectTerminal).toHaveBeenCalledTimes(1);
  expect(xterm.instances.at(-1)!.disposed).toBe(false);
  expect(closeTerminal).not.toHaveBeenCalled();
});

test("under StrictMode a terminal that is live re-attaches once", async () => {
  render(<StrictMode><TerminalView machine="gx10" workspace="w1" resume={{ terminal_id: "t9" }} /></StrictMode>);
  await waitFor(() => expect(connectTerminal).toHaveBeenCalled());
  expect(attachTerminal).toHaveBeenCalledTimes(1);
  expect(connectTerminal).toHaveBeenCalledTimes(1);
});

test("a tile asked to start on a machine its owner has not allowed stays inert", async () => {
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [{ name: "gx10", status: "online", terminal_ready: true, confirmed: false }], terminals: [] });
  const instance = { id: "t-unconfirmed", type: "trax.terminal", version: 1, placement: "main", record_id: null, params: { machine: "gx10", start: true } } as RendererProps["instance"];
  render(<QueryClientProvider client={new QueryClient()}><TerminalTile instance={instance} workspace={null} onWorkspaceChanged={vi.fn()} focused={false} /></QueryClientProvider>);
  await screen.findByRole("button", { name: "Start terminal on gx10" });
  expect(openTerminal).not.toHaveBeenCalled();
});

test("a shell the user starts or adds takes the keyboard as it opens", async () => {
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [ready], terminals: [] });
  vi.mocked(openTerminal).mockResolvedValueOnce({ terminal_id: "t1", ticket: "k1" }).mockResolvedValueOnce({ terminal_id: "t2", ticket: "k2" });
  renderTile("v-focus");
  fireEvent.click(await screen.findByRole("button", { name: "Start terminal on gx10" }));
  await waitFor(() => expect(connectTerminal).toHaveBeenCalledTimes(1));
  expect(xterm.focused).toBe(1);
  fireEvent.click(screen.getByRole("button", { name: "New terminal" }));
  await waitFor(() => expect(connectTerminal).toHaveBeenCalledTimes(2));
  expect(xterm.focused).toBeGreaterThanOrEqual(2);
});

test("a shell an agent starts, or a reload re-attaches, leaves the keyboard where it is", async () => {
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [ready], terminals: [{ terminal_id: "ta", machine: "gx10", surface: "v-keep" }] });
  renderTile("v-keep");
  await waitFor(() => expect(connectTerminal).toHaveBeenCalledTimes(1));
  const asked = { id: "v-asked", type: "trax.terminal", version: 1, placement: "main", record_id: null, params: { machine: "gx10", start: true } } as RendererProps["instance"];
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [ready], terminals: [] });
  render(<QueryClientProvider client={new QueryClient()}><TerminalTile instance={asked} workspace={null} onWorkspaceChanged={vi.fn()} focused={false} /></QueryClientProvider>);
  await waitFor(() => expect(connectTerminal).toHaveBeenCalledTimes(2));
  expect(xterm.focused).toBe(0);
});

/** What the screen's key handler answers a keydown, and whether the page cancelled the key. */
function press(index: number, init: KeyboardEventInit): { sent: boolean; cancelled: boolean } {
  const event = new KeyboardEvent("keydown", { cancelable: true, ...init });
  const sent = xterm.instances[index]!.keys(event);
  return { sent, cancelled: event.defaultPrevented };
}

test("Ctrl+Shift+T opens a tab and Ctrl+Shift+W closes it, the keys never reaching the shell", async () => {
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [ready], terminals: [{ terminal_id: "ta", machine: "gx10", surface: "v-keys" }] });
  renderTile("v-keys");
  await waitFor(() => expect(connectTerminal).toHaveBeenCalled());
  expect(press(0, { ctrlKey: true, shiftKey: true, key: "T" })).toEqual({ sent: false, cancelled: true });
  await waitFor(() => expect(screen.getAllByRole("tab")).toHaveLength(2));
  await waitFor(() => expect(connectTerminal).toHaveBeenCalledTimes(2));
  // The new tab is the shown one, so its screen is the one the keyboard is in.
  expect(press(1, { ctrlKey: true, shiftKey: true, key: "W" })).toEqual({ sent: false, cancelled: true });
  expect(closeTerminal).toHaveBeenCalledWith("t1");
  await waitFor(() => expect(screen.getAllByRole("tab")).toHaveLength(1));
});

test("Ctrl+Shift+PageDown and PageUp switch tabs, and the screen shown takes the keyboard", async () => {
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [ready], terminals: [
    { terminal_id: "ta", machine: "gx10", surface: "v-switch" }, { terminal_id: "tb", machine: "gx10", surface: "v-switch" },
  ] });
  renderTile("v-switch");
  await waitFor(() => expect(screen.getAllByRole("tab")).toHaveLength(2));
  await waitFor(() => expect(connectTerminal).toHaveBeenCalledTimes(2));
  const selected = () => screen.getAllByRole("tab").map((each) => each.getAttribute("aria-selected"));
  expect(selected()).toEqual(["true", "false"]);
  const focused = xterm.focused;
  press(0, { ctrlKey: true, shiftKey: true, key: "PageDown" });
  await waitFor(() => expect(selected()).toEqual(["false", "true"]));
  expect(xterm.focused).toBeGreaterThan(focused);
  press(1, { ctrlKey: true, shiftKey: true, key: "PageUp" });
  await waitFor(() => expect(selected()).toEqual(["true", "false"]));
});

test("plain Ctrl+T, Ctrl+W and Ctrl+Tab reach the shell until the setting is on, then act", async () => {
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [ready], terminals: [{ terminal_id: "ta", machine: "gx10", surface: "v-pass" }] });
  renderTile("v-pass");
  await waitFor(() => expect(connectTerminal).toHaveBeenCalled());
  for (const key of ["t", "w", "Tab"]) expect(press(0, { ctrlKey: true, key })).toEqual({ sent: true, cancelled: false });
  expect(screen.getAllByRole("tab")).toHaveLength(1);
  act(() => receivePreferences({ ...preferences(), terminal_tab_chords: true }));
  try {
    expect(press(0, { ctrlKey: true, key: "t" })).toEqual({ sent: false, cancelled: true });
    await waitFor(() => expect(screen.getAllByRole("tab")).toHaveLength(2));
    expect(press(0, { ctrlKey: true, key: "Tab" })).toEqual({ sent: false, cancelled: true });
    await waitFor(() => expect(screen.getAllByRole("tab")[1]!.getAttribute("aria-selected")).toBe("true"));
    expect(press(1, { ctrlKey: true, key: "w" })).toEqual({ sent: false, cancelled: true });
    await waitFor(() => expect(screen.getAllByRole("tab")).toHaveLength(1));
  } finally {
    act(() => receivePreferences({ ...preferences(), terminal_tab_chords: false }));
  }
});

test("a terminal outside the tile, with no tabs to act on, sends every chord to the shell", async () => {
  render(<TerminalView machine="gx10" workspace="w1" />);
  start();
  await waitFor(() => expect(connectTerminal).toHaveBeenCalled());
  expect(press(0, { ctrlKey: true, shiftKey: true, key: "T" })).toEqual({ sent: true, cancelled: false });
});

test("the STUN servers an open or an attach returns reach the link, for its direct channel", async () => {
  vi.mocked(openTerminal).mockResolvedValue({ terminal_id: "t1", ticket: "tk", ice_servers: ["stun:s.test:3478"] });
  render(<TerminalView machine="gx10" workspace="w1" />);
  start();
  await waitFor(() => expect(connectTerminal).toHaveBeenCalled());
  expect(options.iceServers).toEqual(["stun:s.test:3478"]);
  cleanup();

  vi.mocked(attachTerminal).mockResolvedValue({ ticket: "tk2", ice_servers: ["stun:a.test"] });
  render(<TerminalView machine="gx10" workspace="w1" resume={{ terminal_id: "t9" }} />);
  await waitFor(() => expect(connectTerminal).toHaveBeenCalledTimes(2));
  expect(options.iceServers).toEqual(["stun:a.test"]);
});

test("the view says when output goes direct, and for a while after the link falls back to the server", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  render(<TerminalView machine="gx10" workspace="w1" />);
  start();
  await waitFor(() => expect(connectTerminal).toHaveBeenCalled());
  expect(screen.queryByText("Direct link")).toBeNull();

  act(() => options.onPath!("direct"));
  expect(screen.getByText("Direct link")).toBeTruthy();
  act(() => options.onPath!("relay", "Direct link lost; using the server."));
  expect(screen.queryByText("Direct link")).toBeNull();
  expect(screen.getByText("Direct link lost; using the server.")).toBeTruthy();
  act(() => { vi.advanceTimersByTime(15_000); });
  expect(screen.queryByText(/Direct link lost/)).toBeNull();
  vi.useRealTimers();
});

import { ApiError } from "../api/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { attachTerminal, closeTerminal, connectTerminal, listTerminalMachines, openTerminal, type LinkOptions } from "../api/terminals";
import { ProfileContext } from "../app/boot";
import { bindShortcuts } from "../commands/registry";
import { receivePreferences, preferences } from "../preferences";
import { TerminalTile, TerminalView } from "./Terminal";
import { canvasActions } from "./testing";
import { WorkspaceActionsProvider } from "./workspaceActions";
import type { RendererProps } from "./registry";

const xterm = vi.hoisted(() => ({ events: [] as string[], fitted: null as { rows: number; cols: number } | null, fits: 0, providers: [] as unknown[], instances: [] as Array<{ options: { fontSize?: number }; written: Uint8Array[]; data: (data: string) => void; resize: (size: { rows: number; cols: number }) => void; disposed: boolean }> }));
vi.mock("@xterm/xterm", () => ({
  Terminal: class {
    options: { fontSize?: number } = {}; rows = 24; cols = 80; written: Uint8Array[] = []; disposed = false;
    data: (data: string) => void = () => {};
    resize: (size: { rows: number; cols: number }) => void = () => {};
    onData(handler: (data: string) => void) { xterm.events.push("onData"); this.data = handler; }
    onResize(handler: (size: { rows: number; cols: number }) => void) { xterm.events.push("onResize"); this.resize = handler; }
    constructor() { xterm.instances.push(this); }
    loadAddon(addon: { term?: unknown }) { addon.term = this; }
    open() { xterm.events.push("open"); }
    write(bytes: Uint8Array) { this.written.push(bytes); }
    dispose() { this.disposed = true; }
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
  xterm.events.length = 0;
  xterm.fitted = null;
  vi.mocked(closeTerminal).mockResolvedValue(undefined);
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  vi.mocked(openTerminal).mockResolvedValue({ terminal_id: "t1", session_id: "s1", ticket: "tk" });
  vi.mocked(attachTerminal).mockResolvedValue("tk2");
  vi.mocked(connectTerminal).mockImplementation((given) => { options = given; return link; });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); });

const start = () => fireEvent.click(screen.getByRole("button", { name: /Start terminal/ }));

test("nothing opens until Start is clicked", () => {
  render(<TerminalView machine="gx10" workspace="w1" />);
  expect(openTerminal).not.toHaveBeenCalled();
  expect(xterm.instances).toHaveLength(0);
  expect(screen.getByText("Recorded to your sessions (private).")).toBeTruthy();
});

test("Start opens on the machine from the station with the screen's size, and connects with the ticket", async () => {
  render(<TerminalView machine="gx10" workspace="w1" />);
  start();
  await waitFor(() => expect(connectTerminal).toHaveBeenCalled());
  expect(openTerminal).toHaveBeenCalledWith("gx10", "w1", 24, 80);
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
  act(() => options.onOutput(new Uint8Array([65])));
  expect(xterm.instances[0]!.written).toHaveLength(1);
});

test("a live terminal re-attaches once on mount, with a fresh ticket, and without a click", async () => {
  render(<TerminalView machine="gx10" workspace="w1" resume={{ terminal_id: "t9", session_id: "s9" }} />);
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

test("an ended terminal links its transcript and offers Start new", async () => {
  render(<TerminalView machine="gx10" workspace="w1" />);
  start();
  await waitFor(() => expect(connectTerminal).toHaveBeenCalled());
  act(() => options.onStatus({ state: "ended", note: "The shell exited." }));
  expect(screen.getByRole("link", { name: "Transcript" }).getAttribute("href")).toBe("#/lookup/s1");
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

test("the tile keeps the terminal in its params once it opens, and re-attaches from them", async () => {
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [{ name: "gx10", status: "online", terminal_ready: true, confirmed: true }], live: null });
  const actions = canvasActions();
  const instance = { id: "v1", type: "trax.terminal", version: 1, placement: "main", record_id: null, params: {} } as RendererProps["instance"];
  const props = { instance, workspace: null, onWorkspaceChanged: vi.fn(), focused: false };
  const wrap = (each: RendererProps) => <QueryClientProvider client={new QueryClient()}><WorkspaceActionsProvider value={actions}><TerminalTile {...each} /></WorkspaceActionsProvider></QueryClientProvider>;
  render(wrap(props));
  await screen.findByRole("button", { name: "Start terminal on gx10" });
  start();
  await waitFor(() => expect(actions.operate).toHaveBeenCalledWith(expect.objectContaining({ params: { machine: "gx10", terminal: "t1" } })));
  cleanup();
  render(wrap({ ...props, instance: { ...instance, params: { machine: "gx10", terminal: "t1" } } }));
  await waitFor(() => expect(attachTerminal).toHaveBeenCalledWith("t1"));
});

test("a 409 terminal_live on Start attaches to the station's live terminal instead of failing", async () => {
  vi.mocked(openTerminal).mockRejectedValue(new ApiError(409, "a terminal is live", "terminal_live"));
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [], live: { terminal_id: "t7", session_id: "s7", machine: "gx10" } });
  render(<TerminalView machine="gx10" workspace="w1" />);
  start();
  await waitFor(() => expect(connectTerminal).toHaveBeenCalled());
  expect(listTerminalMachines).toHaveBeenCalledWith("w1");
  expect(attachTerminal).toHaveBeenCalledWith("t7");
  expect(options).toMatchObject({ id: "t7", ticket: "tk2" });
});

test.each([
  ["terminal_not_enabled", 409, "Start the host with `trax machine connect`: that one command turns terminals on."],
  ["terminal_limit", 429, "Terminal limit reached; close one."],
  ["terminal_unconfirmed", 409, "Allow terminals on this machine first: open Chat's terminal side and click Allow."],
  ["terminal_forbidden", 403, "Terminals are not available on a server without sign-in."],
])("an open refused with %s says what to do", async (code, status, text) => {
  vi.mocked(openTerminal).mockRejectedValue(new ApiError(status, "refused", code));
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
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [{ name: "gx10", status: "online", terminal_ready: false, confirmed: true }], live: null });
  const instance = (params: Record<string, string | boolean>, id: string) => ({ id, type: "trax.terminal", version: 1, placement: "main", record_id: null, params }) as RendererProps["instance"];
  const wrap = (each: RendererProps["instance"]) => <QueryClientProvider client={new QueryClient()}><TerminalTile instance={each} workspace={null} onWorkspaceChanged={vi.fn()} focused={false} /></QueryClientProvider>;
  render(wrap(instance({ machine: "gx10", start: true }, "t-notready")));
  await screen.findByRole("button", { name: "Start terminal on gx10" });
  expect(openTerminal).not.toHaveBeenCalled();
  cleanup();
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [{ name: "gx10", status: "online", terminal_ready: true, confirmed: true }], live: null });
  render(wrap(instance({ machine: "gx10", start: true, terminal: "t1" }, "t-placed")));
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
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [{ name: "gx10", status: "online", terminal_ready: true, confirmed: true }], live: null });
  const tile = (id: string, placed_by: string | null, workspace: unknown) => render(
    <QueryClientProvider client={new QueryClient()}><ProfileContext value={{ email: "ada@example.com" } as never}>
      <TerminalTile instance={{ id, type: "trax.terminal", version: 1, placement: "main", record_id: null, params: { machine: "gx10", start: true }, placed_by } as RendererProps["instance"]}
        workspace={workspace as RendererProps["workspace"]} onWorkspaceChanged={vi.fn()} focused={false} /></ProfileContext></QueryClientProvider>);
  tile("q1", "grace@example.com", null);
  expect(await screen.findByText("Opened at grace@example.com's request.")).toBeTruthy();
  cleanup();
  tile("q2", null, { id: "w1", partner: { kind: "shared", actor: "scout" } });
  expect(await screen.findByText("Opened at scout's request.")).toBeTruthy();
  cleanup();
  tile("q3", "ada@example.com", { id: "w1", partner: { kind: "local", actor: null } });
  expect(await screen.findByText("Opened at your agent's request.")).toBeTruthy();
});

test("handlers are registered before the first fit, and the open sends the fitted size", async () => {
  xterm.fitted = { rows: 40, cols: 120 };
  render(<TerminalView machine="gx10" workspace="w1" />);
  start();
  await waitFor(() => expect(connectTerminal).toHaveBeenCalled());
  expect(xterm.events.slice(0, 4)).toEqual(["onData", "onResize", "open", "fit"]);
  expect(openTerminal).toHaveBeenCalledWith("gx10", "w1", 40, 120);
  expect(options.size?.()).toEqual({ rows: 40, cols: 120 });
});

test("an unmount while the terminal is still opening closes the one that arrives, and one being attached is left running", async () => {
  let arrive: (opened: { terminal_id: string; session_id: string; ticket: string }) => void = () => {};
  vi.mocked(openTerminal).mockReturnValue(new Promise((resolve) => { arrive = resolve; }));
  const first = render(<TerminalView machine="gx10" workspace="w1" />);
  start();
  await waitFor(() => expect(openTerminal).toHaveBeenCalled());
  first.unmount();
  await waitFor(() => expect(xterm.instances[0]!.disposed).toBe(true));
  arrive({ terminal_id: "t5", session_id: "s5", ticket: "tk" });
  await waitFor(() => expect(closeTerminal).toHaveBeenCalledWith("t5"));
  expect(connectTerminal).not.toHaveBeenCalled();
  let attached: (ticket: string) => void = () => {};
  vi.mocked(attachTerminal).mockReturnValue(new Promise((resolve) => { attached = resolve; }));
  const second = render(<TerminalView machine="gx10" workspace="w1" resume={{ terminal_id: "t9", session_id: "s9" }} />);
  await waitFor(() => expect(attachTerminal).toHaveBeenCalledWith("t9"));
  second.unmount();
  attached("tk");
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
  render(<StrictMode><TerminalView machine="gx10" workspace="w1" resume={{ terminal_id: "t9", session_id: "s9" }} /></StrictMode>);
  await waitFor(() => expect(connectTerminal).toHaveBeenCalled());
  expect(attachTerminal).toHaveBeenCalledTimes(1);
  expect(connectTerminal).toHaveBeenCalledTimes(1);
});

test("a tile asked to start on a machine its owner has not allowed stays inert", async () => {
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [{ name: "gx10", status: "online", terminal_ready: true, confirmed: false }], live: null });
  const instance = { id: "t-unconfirmed", type: "trax.terminal", version: 1, placement: "main", record_id: null, params: { machine: "gx10", start: true } } as RendererProps["instance"];
  render(<QueryClientProvider client={new QueryClient()}><TerminalTile instance={instance} workspace={null} onWorkspaceChanged={vi.fn()} focused={false} /></QueryClientProvider>);
  await screen.findByRole("button", { name: "Start terminal on gx10" });
  expect(openTerminal).not.toHaveBeenCalled();
});

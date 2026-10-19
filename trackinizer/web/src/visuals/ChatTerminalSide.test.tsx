import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { closeTerminal, confirmTerminalMachine, listTerminalMachines, type LiveTerminal, type TerminalMachine } from "../api/terminals";
import type { WorkspaceState } from "../api/workspaces";
import { listWorkstreams } from "../api/workstreams";
import { ChatTerminalSide, type TerminalHead } from "./ChatTerminalSide";

vi.mock("../api/terminals", () => ({ listTerminalMachines: vi.fn(), confirmTerminalMachine: vi.fn(), closeTerminal: vi.fn(async () => undefined) }));
vi.mock("../api/workstreams", () => ({ listWorkstreams: vi.fn() }));
const view = vi.fn();
vi.mock("./Terminal", () => ({ TerminalView: (props: object) => { view(props); return <div>terminal view</div>; } }));

const workspace = { id: "w1", workstream_id: "g1" } as WorkspaceState;
const machine = (name: string, extra: Partial<TerminalMachine> = {}): TerminalMachine => ({ name, status: "online", terminal_ready: true, confirmed: true, ...extra });

// What the flipped header was last given.
let head: TerminalHead;
function mount(machines: TerminalMachine[], terminals: LiveTerminal[] = []) {
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines, terminals });
  vi.mocked(listWorkstreams).mockResolvedValue([{ id: "g1", title: "ARC3", scratch: false } as never]);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return { client, ...render(<QueryClientProvider client={client}><ChatTerminalSide workspace={workspace} shown header={(terminal) => { head = terminal; return <header>chat header</header>; }} /></QueryClientProvider>) };
}

beforeEach(() => view.mockClear());
afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks(); });

test("one machine: the bar names it and the workstream, with no picker", async () => {
  mount([machine("gx10")]);
  await screen.findByText("terminal view");
  const bar = screen.getByRole("note");
  expect(bar.textContent).toContain("trax run claude");
  expect(bar.textContent).toContain("gx10");
  expect(bar.textContent).toContain("ARC3");
  expect(screen.queryByRole("combobox", { name: "Machine" })).toBeNull();
});

test("two machines: the first ready one is in view, and the header starts a new terminal on the other", async () => {
  mount([machine("gx10"), machine("laptop")]);
  await screen.findByText("terminal view");
  expect(screen.queryByRole("combobox", { name: "Machine" })).toBeNull();
  expect(head.label).toBe("Terminal");
  expect(head.machines).toEqual(["gx10", "laptop"]);
  act(() => head.create("laptop"));
  expect(view).toHaveBeenLastCalledWith(expect.objectContaining({ machine: "laptop", workspace: "w1", resume: null, autoStart: expect.stringContaining("w1:") }));
});

test("the header lists this side's terminals by machine and number, and switching re-attaches the other", async () => {
  const mine = [{ terminal_id: "a", machine: "gx10", surface: "chat" }, { terminal_id: "b", machine: "laptop", surface: "chat" }, { terminal_id: "c", machine: "gx10", surface: "chat" }];
  mount([machine("gx10"), machine("laptop")], [...mine, { terminal_id: "t", machine: "gx10", surface: "tile" }]);
  await screen.findByText("terminal view");
  expect(head.terminals.map((each) => each.label)).toEqual(["gx10 1", "laptop 1", "gx10 2"]);
  expect(head.label).toBe("gx10 2");
  expect(head.selected).toBe("c");
  act(() => head.select("b"));
  expect(head.label).toBe("laptop 1");
  expect(view).toHaveBeenLastCalledWith(expect.objectContaining({ machine: "laptop", resume: mine[1] }));
});

test("Close terminal stops the shell in view and leaves Start on offer; none in view, none to close", async () => {
  mount([machine("gx10")], [{ terminal_id: "a", machine: "gx10", surface: "chat" }]);
  await screen.findByText("terminal view");
  act(() => head.close!());
  await waitFor(() => expect(closeTerminal).toHaveBeenCalledWith("a"));
  await waitFor(() => expect(view).toHaveBeenLastCalledWith(expect.objectContaining({ resume: null, machine: "gx10" })));
  cleanup();
  mount([machine("gx10")]);
  await screen.findByText("terminal view");
  expect(head.close).toBeNull();
});

test("a machine that is not ready is not offered", async () => {
  mount([machine("old", { terminal_ready: false }), machine("gx10")]);
  await screen.findByText("terminal view");
  expect(screen.getByRole("note").textContent).not.toContain("old");
});

test("no machine: the bar offers `trax machine connect` and Copy writes it", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  mount([]);
  await screen.findByText(/Connect this laptop/);
  fireEvent.click(within(screen.getByRole("note")).getByRole("button", { name: "Copy" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith("trax machine connect"));
  vi.unstubAllGlobals();
});

test("the newest terminal this side opened re-attaches: its machine is chosen and it is the view's resume", async () => {
  const older = { terminal_id: "t8", machine: "gx10", surface: "chat" };
  const live = { terminal_id: "t9", machine: "laptop", surface: "chat" };
  const tile = { terminal_id: "t10", machine: "gx10", surface: "7f1c2a9e-0000-4000-8000-000000000000" };
  mount([machine("gx10"), machine("laptop")], [older, live, tile]);
  await screen.findByText("terminal view");
  expect(view).toHaveBeenLastCalledWith(expect.objectContaining({ machine: "laptop", resume: live, surface: "chat" }));
});

test("a terminal only a tile opened is not this side's to re-attach", async () => {
  mount([machine("gx10")], [{ terminal_id: "t10", machine: "gx10", surface: "tile-1" }]);
  await screen.findByText("terminal view");
  expect(view).toHaveBeenLastCalledWith(expect.objectContaining({ resume: null, surface: "chat" }));
});

test("dismissing the bar keeps it gone on the next mount", async () => {
  const first = mount([machine("gx10")]);
  await screen.findByText("terminal view");
  fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
  expect(screen.queryByRole("note")).toBeNull();
  first.unmount();
  mount([machine("gx10")]);
  await screen.findByText("terminal view");
  expect(screen.queryByRole("note")).toBeNull();
});

test("Chat's header tops the side, and the side has none of its own", async () => {
  mount([machine("gx10")]);
  await screen.findByText("terminal view");
  const side = screen.getByRole("region", { name: "Terminal" });
  expect(side.firstElementChild!.textContent).toBe("chat header");
  expect(side.querySelectorAll("header")).toHaveLength(1);
});

test("a machines refetch that fails does not unmount a live terminal view", async () => {
  const shown = mount([machine("gx10")]);
  await screen.findByText("terminal view");
  vi.mocked(listTerminalMachines).mockRejectedValue(new Error("offline"));
  await shown.client.refetchQueries({ queryKey: ["terminal-machines", "w1"] });
  await waitFor(() => expect(shown.client.getQueryState(["terminal-machines", "w1"])?.status).toBe("error"));
  expect(screen.getByText("terminal view")).toBeTruthy();
  expect(screen.queryByRole("alert")).toBeNull();
});

test("hidden, the side asks the server for nothing", async () => {
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [], terminals: [] });
  vi.mocked(listWorkstreams).mockResolvedValue([]);
  render(<QueryClientProvider client={new QueryClient()}><ChatTerminalSide workspace={workspace} shown={false} header={() => null} /></QueryClientProvider>);
  await Promise.resolve();
  expect(listTerminalMachines).not.toHaveBeenCalled();
});

test("a machine enrolled with a key is never offered until Allow: one click confirms it, and it is offered after", async () => {
  vi.mocked(confirmTerminalMachine).mockResolvedValue(undefined);
  const shown = mount([machine("fresh", { confirmed: false }), machine("gx10")]);
  await screen.findByText("terminal view");
  expect(screen.getByRole("group", { name: "Allow fresh" }).textContent).toContain("New machine fresh wants terminals.");
  expect(screen.getByRole("note").textContent).not.toContain("fresh");
  expect(view).toHaveBeenLastCalledWith(expect.objectContaining({ machine: "gx10" }));
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [machine("fresh"), machine("gx10")], terminals: [] });
  fireEvent.click(screen.getByRole("button", { name: "Allow" }));
  await waitFor(() => expect(confirmTerminalMachine).toHaveBeenCalledWith("fresh"));
  await waitFor(() => expect(screen.queryByRole("group", { name: "Allow fresh" })).toBeNull());
  expect(head.machines).toEqual(["fresh", "gx10"]);
  shown.unmount();
});

test("only unconfirmed machines: the bar points at Allow, and nothing is offered to Start", async () => {
  mount([machine("fresh", { confirmed: false })]);
  await screen.findByText("terminal view");
  expect(view).toHaveBeenLastCalledWith(expect.objectContaining({ machine: null }));
  expect(screen.getByRole("note").textContent).toContain("Allow a machine above");
});

test("the machines keep the server's newest-first order in the header's menu, and the first ready one is the default", async () => {
  mount([machine("newest"), machine("middle"), machine("oldest")]);
  await screen.findByText("terminal view");
  expect(head.machines).toEqual(["newest", "middle", "oldest"]);
  expect(view).toHaveBeenLastCalledWith(expect.objectContaining({ machine: "newest" }));
});

test("dismissing the bar leaves the machine choice in the header", async () => {
  mount([machine("gx10"), machine("laptop")]);
  await screen.findByText("terminal view");
  fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
  expect(head.machines).toEqual(["gx10", "laptop"]);
  expect(screen.queryByRole("note")).toBeNull();
});

test("a failed Allow says so and leaves the machine unoffered", async () => {
  vi.mocked(confirmTerminalMachine).mockRejectedValue(new Error("403"));
  mount([machine("fresh", { confirmed: false })]);
  await screen.findByText("terminal view");
  fireEvent.click(screen.getByRole("button", { name: "Allow" }));
  expect((await screen.findByRole("alert")).textContent).toBe("Could not allow fresh.");
});

const help = () => document.querySelector<HTMLDetailsElement>("details.chat-terminal-help")!;
const SECTIONS: readonly (readonly [string, readonly string[]])[] = [
  ["Connect this machine", [`uv tool install git+https://github.com/rekursiv-ai/trackinizer && trax profile url to ${location.origin} && trax profile token to <TOKEN>`]],
  ["Over ssh", ["ssh HOST"]],
  ["Detached agents", ["trax run --detach --name NAME claude", "trax run ls", "trax run attach NAME", "trax run log NAME --follow", "trax run stop NAME"]],
  ["Typing feels slow?", ["uv tool install --with aiortc git+https://github.com/rekursiv-ai/trackinizer"]],
];

test("the help has a section per job, each command in code with its own Copy that writes it", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  mount([machine("gx10")]);
  await screen.findByText("terminal view");
  const reconnecting = within(help()).getByRole("region", { name: "Reconnecting", hidden: true }).textContent;
  expect(reconnecting).toContain("no browser attached for a minute ends");
  expect(reconnecting).toContain("60 minutes");
  for (const [name, commands] of SECTIONS) {
    const section = within(help()).getByRole("region", { name, hidden: true });
    const buttons = within(section).getAllByRole("button", { name: "Copy", hidden: true });
    expect(Array.from(section.querySelectorAll("code")).map((each) => each.textContent)).toEqual(expect.arrayContaining([...commands]));
    expect(buttons).toHaveLength(commands.length);
    for (const [index, command] of commands.entries()) {
      fireEvent.click(buttons[index]!);
      await waitFor(() => expect(writeText).toHaveBeenLastCalledWith(command));
    }
  }
  vi.unstubAllGlobals();
});

test("the setup command names the page's own origin", async () => {
  mount([machine("gx10")]);
  await screen.findByText("terminal view");
  expect(help().textContent).toContain(`trax profile url to ${location.origin} `);
});

test("dismissing hides the help with the bar, and the dismissal is remembered", async () => {
  mount([machine("gx10")]);
  await screen.findByText("terminal view");
  fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
  expect(document.querySelector("details.chat-terminal-help")).toBeNull();
});

test("the help is a closed disclosure under the bar, open once reopened from the menu", async () => {
  const first = mount([machine("gx10")]);
  await screen.findByText("terminal view");
  expect(help().open).toBe(false);
  first.unmount();
  localStorage.setItem("trackinizer.chat.terminalBar.dismissed", "open");
  mount([machine("gx10")]);
  await screen.findByText("terminal view");
  expect(help().open).toBe(true);
});

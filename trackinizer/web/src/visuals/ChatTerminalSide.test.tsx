import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { confirmTerminalMachine, listTerminalMachines, type LiveTerminal, type TerminalMachine } from "../api/terminals";
import type { WorkspaceState } from "../api/workspaces";
import { listWorkstreams } from "../api/workstreams";
import { ChatTerminalSide } from "./ChatTerminalSide";

vi.mock("../api/terminals", () => ({ listTerminalMachines: vi.fn(), confirmTerminalMachine: vi.fn() }));
vi.mock("../api/workstreams", () => ({ listWorkstreams: vi.fn() }));
const view = vi.fn();
vi.mock("./Terminal", () => ({ TerminalView: (props: object) => { view(props); return <div>terminal view</div>; } }));

const workspace = { id: "w1", workstream_id: "g1" } as WorkspaceState;
const machine = (name: string, extra: Partial<TerminalMachine> = {}): TerminalMachine => ({ name, status: "online", terminal_ready: true, confirmed: true, ...extra });

function mount(machines: TerminalMachine[], live: LiveTerminal | null = null) {
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines, live });
  vi.mocked(listWorkstreams).mockResolvedValue([{ id: "g1", title: "ARC3", scratch: false } as never]);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return { client, ...render(<QueryClientProvider client={client}><ChatTerminalSide workspace={workspace} shown onFlipBack={vi.fn()} /></QueryClientProvider>) };
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

test("two machines: a picker, first ready one chosen, and the choice reaches Start", async () => {
  mount([machine("gx10"), machine("laptop")]);
  await screen.findByText("terminal view");
  const picker = screen.getByRole("combobox", { name: "Machine" }) as HTMLSelectElement;
  expect(picker.value).toBe("gx10");
  fireEvent.change(picker, { target: { value: "laptop" } });
  expect(view).toHaveBeenLastCalledWith(expect.objectContaining({ machine: "laptop", workspace: "w1" }));
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
  fireEvent.click(screen.getByRole("button", { name: "Copy" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith("trax machine connect"));
  vi.unstubAllGlobals();
});

test("a terminal live on the station re-attaches: its machine is chosen and it is the view's resume", async () => {
  const live = { terminal_id: "t9", machine: "laptop" };
  mount([machine("gx10"), machine("laptop")], live);
  await screen.findByText("terminal view");
  expect(view).toHaveBeenLastCalledWith(expect.objectContaining({ machine: "laptop", resume: live }));
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

test("the chat icon flips back", async () => {
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [], live: null });
  vi.mocked(listWorkstreams).mockResolvedValue([]);
  const back = vi.fn();
  render(<QueryClientProvider client={new QueryClient()}><ChatTerminalSide workspace={workspace} shown onFlipBack={back} /></QueryClientProvider>);
  fireEvent.click(screen.getByRole("button", { name: "Back to chat" }));
  expect(back).toHaveBeenCalled();
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
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [], live: null });
  vi.mocked(listWorkstreams).mockResolvedValue([]);
  render(<QueryClientProvider client={new QueryClient()}><ChatTerminalSide workspace={workspace} shown={false} onFlipBack={vi.fn()} /></QueryClientProvider>);
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
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [machine("fresh"), machine("gx10")], live: null });
  fireEvent.click(screen.getByRole("button", { name: "Allow" }));
  await waitFor(() => expect(confirmTerminalMachine).toHaveBeenCalledWith("fresh"));
  await waitFor(() => expect(screen.queryByRole("group", { name: "Allow fresh" })).toBeNull());
  expect(screen.getByRole("combobox", { name: "Machine" })).toBeTruthy();
  shown.unmount();
});

test("only unconfirmed machines: the bar points at Allow, and nothing is offered to Start", async () => {
  mount([machine("fresh", { confirmed: false })]);
  await screen.findByText("terminal view");
  expect(view).toHaveBeenLastCalledWith(expect.objectContaining({ machine: null }));
  expect(screen.getByRole("note").textContent).toContain("Allow a machine above");
});

test("the machines keep the server's newest-first order in the picker, and the first ready one is the default", async () => {
  mount([machine("newest"), machine("middle"), machine("oldest")]);
  await screen.findByText("terminal view");
  const picker = screen.getByRole("combobox", { name: "Machine" }) as HTMLSelectElement;
  expect(Array.from(picker.options, (option) => option.value)).toEqual(["newest", "middle", "oldest"]);
  expect(picker.value).toBe("newest");
});

test("a failed Allow says so and leaves the machine unoffered", async () => {
  vi.mocked(confirmTerminalMachine).mockRejectedValue(new Error("403"));
  mount([machine("fresh", { confirmed: false })]);
  await screen.findByText("terminal view");
  fireEvent.click(screen.getByRole("button", { name: "Allow" }));
  expect((await screen.findByRole("alert")).textContent).toBe("Could not allow fresh.");
});

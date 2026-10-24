import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ProfileContext } from "../app/boot";
import type { Profile } from "../api/me";
import { listCompute, type ComputeNote } from "../api/compute";
import { confirmTerminalMachine, listTerminalMachines, type TerminalMachine } from "../api/terminals";
import { NO_AUTH_EMAIL } from "../app/signOut";
import { MachinesMenu } from "./MachinesMenu";
// The menu's body is a chunk the first open imports; loaded with the file, its import
// cost is the file's, not the first test's.
import "./MachinesPanel";

vi.mock("../api/terminals", () => ({ listTerminalMachines: vi.fn(), confirmTerminalMachine: vi.fn() }));
vi.mock("../api/compute", () => ({ listCompute: vi.fn() }));

const machine = (name: string, extra: Partial<TerminalMachine> = {}): TerminalMachine => ({ name, status: "online", terminal_ready: true, confirmed: true, ...extra });
const person = (role = "writer", email = "ada@example.com") => ({ email, role }) as Profile;

function mount(machines: TerminalMachine[], notes: ComputeNote[] = [], profile = person(), shown = true) {
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines, terminals: [] });
  vi.mocked(listCompute).mockResolvedValue(notes);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const menu = (bar: boolean) => <QueryClientProvider client={client}><ProfileContext value={profile}><MachinesMenu shown={bar} /></ProfileContext></QueryClientProvider>;
  const { rerender } = render(menu(shown));
  return Object.assign(client, { show: () => rerender(menu(true)) });
}

const button = () => screen.getByRole("button", { name: /^Machines/ });
const menu = () => screen.getByRole("dialog", { name: "Machines" });
const steps = () => menu().querySelector<HTMLDetailsElement>("details.machines-connect")!;
/** The row of the machine called `name`, once the machines are read. */
const row = async (root: HTMLElement, name: string) => (await within(root).findByText(name)).closest("li")!;

/** Open the menu and wait for its body, a chunk of its own, to draw. */
async function open() {
  fireEvent.click(button());
  await within(menu()).findByText("Your machines");
  return menu();
}

/** Wait, looking every few milliseconds, until the read under `key` has its answer. */
async function answered(client: QueryClient, key: readonly unknown[]) {
  await waitFor(() => expect(client.getQueryState(key)?.status).toBe("success"), { interval: 5 });
}

afterEach(() => { cleanup(); vi.clearAllMocks(); });

test("the button says where commands run and opens the menu with the focus in it", async () => {
  mount([machine("laptop")]);
  expect(button().title).toBe("Machines: where commands run");
  expect(button().getAttribute("aria-haspopup")).toBe("true");
  const shown = await open();
  expect(button().getAttribute("aria-expanded")).toBe("true");
  // The focus moves a frame after the menu draws, so look every few milliseconds.
  await waitFor(() => expect(shown.contains(document.activeElement)).toBe(true), { interval: 5 });
});

test("Esc closes the menu and gives the focus back to its button; a press outside closes it", async () => {
  mount([machine("laptop")]);
  await open();
  fireEvent.keyDown(document, { key: "Escape" });
  expect(screen.queryByRole("dialog", { name: "Machines" })).toBeNull();
  expect(document.activeElement).toBe(button());
  await open();
  fireEvent.pointerDown(document.body);
  expect(screen.queryByRole("dialog", { name: "Machines" })).toBeNull();
});

test("your machines are listed with their status, the cache shared with the terminal", async () => {
  const client = mount([machine("laptop"), machine("desk", { status: "offline", terminal_ready: false })]);
  const shown = await open();
  const laptop = await row(shown, "laptop");
  expect(laptop.textContent).toContain("Online");
  expect((await row(shown, "desk")).textContent).toContain("Offline");
  expect(listTerminalMachines).toHaveBeenCalledWith(undefined, expect.anything());
  expect(client.getQueryData(["terminal-machines", null])).toEqual({ machines: expect.any(Array), terminals: [] });
});

test("Allow confirms a machine enrolled with a token and reads the machines again", async () => {
  vi.mocked(confirmTerminalMachine).mockResolvedValue(undefined);
  mount([machine("fresh", { confirmed: false })]);
  const shown = await open();
  const fresh = await row(shown, "fresh");
  expect(fresh.textContent).toContain("Waiting for you to allow it");
  const calls = vi.mocked(listTerminalMachines).mock.calls.length;
  vi.mocked(listTerminalMachines).mockResolvedValue({ machines: [machine("fresh")], terminals: [] });
  fireEvent.click(within(fresh).getByRole("button", { name: "Allow" }));
  await waitFor(() => expect(confirmTerminalMachine).toHaveBeenCalledWith("fresh"));
  await waitFor(() => expect(within(shown).queryByRole("button", { name: "Allow" })).toBeNull());
  expect(vi.mocked(listTerminalMachines).mock.calls.length).toBeGreaterThan(calls);
});

test("a refused Allow says so and keeps the button", async () => {
  vi.mocked(confirmTerminalMachine).mockRejectedValue(new Error("403"));
  mount([machine("fresh", { confirmed: false })]);
  const shown = await open();
  fireEvent.click(await within(shown).findByRole("button", { name: "Allow" }));
  expect((await within(shown).findByRole("alert")).textContent).toBe("Could not allow fresh.");
  expect(within(shown).getByRole("button", { name: "Allow" })).toBeTruthy();
});

test("with no machine the menu says so and the steps to connect one are open", async () => {
  mount([]);
  const shown = await open();
  expect(await within(shown).findByText("No machine connected yet.")).toBeTruthy();
  expect(steps().open).toBe(true);
  expect(within(steps()).getByRole("link", { name: "Settings → API tokens" }).getAttribute("href")).toBe("/trax/settings");
  expect(within(steps()).getByText(/keep it connected in tmux or as a user service/)).toBeTruthy();
});

test("once a machine exists the steps fold away", async () => {
  mount([machine("laptop")]);
  const shown = await open();
  await row(shown, "laptop");
  expect(steps().open).toBe(false);
});

test("the setup command carries the page's own origin", async () => {
  mount([]);
  await open();
  expect(within(steps()).getByText(`uv tool install git+https://github.com/rekursiv-ai/trackinizer && trax profile url to ${location.origin} && trax profile token to <TOKEN>`)).toBeTruthy();
});

test("the dot shows only while one of your machines is online", async () => {
  await answered(mount([machine("laptop", { status: "offline", terminal_ready: false })]), ["terminal-machines", null]);
  expect(button().getAttribute("aria-label")).toBe("Machines");
  expect(button().querySelector(".machines-dot")).toBeNull();
  cleanup();
  await answered(mount([machine("desk", { status: "offline" }), machine("laptop")]), ["terminal-machines", null]);
  expect(button().querySelector(".machines-dot")).not.toBeNull();
  expect(button().getAttribute("aria-label")).toBe("Machines, 1 online");
});

test("a folded bar asks for nothing, so a page's first reads stay its own; shown, it reads the machines", async () => {
  const client = mount([machine("laptop")], [], person(), false);
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(listTerminalMachines).not.toHaveBeenCalled();
  expect(listCompute).not.toHaveBeenCalled();
  client.show();
  await answered(client, ["terminal-machines", null]);
  expect(button().querySelector(".machines-dot")).not.toBeNull();
});

test("the server's notes show under its name", async () => {
  mount([], [{ name: "GPU jobs", about: "Campaigns run their GPU work here." }, { name: "Workspace box", about: "Each chat has a shell of its own." }]);
  const shown = await open();
  const provided = await within(shown).findByRole("region", { name: /^Provided by / });
  expect(within(provided).getAllByRole("listitem").map((each) => each.textContent)).toEqual([
    "GPU jobsCampaigns run their GPU work here.",
    "Workspace boxEach chat has a shell of its own.",
  ]);
});

test("a server with no notes has no such section", async () => {
  const client = mount([]);
  const shown = await open();
  await answered(client, ["compute"]);
  await within(shown).findByText("No machine connected yet.");
  expect(within(shown).queryByRole("region", { name: /^Provided by / })).toBeNull();
});

test("a server without sign-in lists no machines and asks for none", async () => {
  mount([], [], person("admin", NO_AUTH_EMAIL));
  await open();
  expect(within(menu()).getByText(/without sign-in/)).toBeTruthy();
  expect(listTerminalMachines).not.toHaveBeenCalled();
  expect(menu().querySelector("details.machines-connect")).toBeNull();
});

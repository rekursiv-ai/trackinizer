import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { AGREED, stubFetch } from "../api/testing";
import { FakeEventSource } from "../live/testing";
import { setChatSide } from "../visuals/chatColumn";
import { FOLD_AFTER_MS, OPEN_AFTER_MS } from "../visuals/hoverOpen";
import { Icon } from "../ui/icons";
import { App } from "./App";
import { useWorkbenchMounted, useWorkbenchSlot } from "./workbenchSlot";

// Counts the times the canvas's code is loaded: a module's factory runs once,
// when the module is first imported.
// Its chunk arrives when a test releases `gate`.
const loaded = vi.hoisted(() => {
  let release = () => {};
  const gate = new Promise<void>((resolve) => { release = resolve; });
  return { canvas: 0, column: 0, gate, release };
});
vi.mock("../visuals/Canvas", async () => {
  loaded.canvas += 1;
  await loaded.gate;
  const { createPortal } = await import("react-dom");
  // The real canvas draws its controls into the sidebar's Workbench section.
  return {
    Canvas: ({ children }: { children: ReactNode }) => {
      useWorkbenchMounted();
      const slot = useWorkbenchSlot();
      return <section aria-label="Canvas">{children}{slot && createPortal(<label><input type="checkbox" />Note</label>, slot)}</section>;
    },
  };
});
// Chat's column around the canvas, which the shell loads with it.
vi.mock("../visuals/ChatColumn", () => {
  loaded.column += 1;
  return { ChatColumn: ({ children }: { children: ReactNode }) => <div data-testid="chat-column">{children}</div> };
});

/** Serve boot for a user whose canvas is `enabled`, and an empty list. */
function serve(enabled: boolean) {
  const bodies: { [path: string]: unknown } = {
    "/api/meta/enums": { inquiry_kind_all: ["Issue"], status: ["active"] },
    "/api/meta/fields": {},
    "/api/meta/edges": {},
    "/api/me/profile": { user_id: "u", email: "ada@example.com", name: "Ada", role: "writer", last_login: null, visual_workspace_enabled: enabled, ...AGREED },
    "/api/inquiries": [],
  };
  stubFetch((request) => Response.json(bodies[new URL(request.url).pathname]));
}

beforeEach(() => {
  history.replaceState(null, "", "#/list/Issue");
  sessionStorage.clear();
  localStorage.clear();
  setChatSide("left");
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

// In this order: once loaded, the canvas's module stays loaded for the file.
test("a user who opted out gets no canvas, and its code never loads", async () => {
  serve(false);
  render(<App assign={vi.fn()} />);
  expect((await screen.findByRole("heading", { level: 1 })).textContent).toBe("Issues");
  expect(screen.queryByRole("region", { name: "Canvas" })).toBeNull();
  expect(loaded.canvas).toBe(0);
});

test("while the canvas's chunk loads the shell holds its busy frame, and the view then mounts once, inside the canvas", async () => {
  serve(true);
  render(<App assign={vi.fn()} />);
  await waitFor(() => expect(loaded.canvas).toBe(1));
  expect(document.querySelector(".view[aria-busy=true]")).not.toBeNull();
  expect(screen.queryByRole("heading", { level: 1 })).toBeNull();
  expect(screen.queryByRole("region", { name: "Canvas" })).toBeNull();
  await act(async () => loaded.release());
  const canvas = await screen.findByRole("region", { name: "Canvas" });
  expect(canvas.querySelector("h1")?.textContent).toBe("Issues");
  expect(document.querySelector(".view[aria-busy=true]")).toBeNull();
  // Chat's column came with the canvas, around it, so neither first render suspends.
  expect(loaded.column).toBe(1);
  expect(screen.getByTestId("chat-column").contains(canvas)).toBe(true);
});

test("a user with the canvas on sees the list inside the canvas", async () => {
  serve(true);
  render(<App assign={vi.fn()} />);
  const canvas = await screen.findByRole("region", { name: "Canvas" });
  expect(canvas.querySelector("h1")?.textContent).toBe("Issues");
  expect(loaded.canvas).toBe(1);
});

test("the canvas wraps a list, not Settings, and wraps it again on the way back", async () => {
  serve(true);
  render(<App assign={vi.fn()} />);
  await screen.findByRole("region", { name: "Canvas" });
  act(() => { window.location.hash = "#/settings"; });
  await waitFor(() => expect(screen.queryByRole("region", { name: "Canvas" })).toBeNull(), { interval: 1 });
  act(() => { window.location.hash = "#/list/Issue"; });
  await screen.findByRole("region", { name: "Canvas" });
});

test("the sidebar starts as a rail of its entries, named, titled and in order; its button, or ⌘B / Ctrl+B, pins it open, and again folds it", async () => {
  serve(false);
  render(<App assign={vi.fn()} />);
  await screen.findByRole("heading", { level: 1 });
  const user = userEvent.setup();
  const sidebar = screen.getByRole("navigation", { name: "Sidebar" });
  const toggle = () => within(sidebar).getByRole("button", { name: /sidebar$/ });
  const entries = () => [...sidebar.querySelectorAll("a.nav-item")];
  const named = () => entries().map((entry) => [entry.getAttribute("aria-label"), entry.getAttribute("title")]);
  const names = entries().map((entry) => entry.textContent);
  expect(names).toEqual(["Graph", "Console", "Activity", "Issues", "Your settings"]);
  // The same nav as a rail: the button that pins it comes first, in the keyboard's order too; the rail hides the logo.
  expect(document.querySelector(".app")!.classList).toContain("sidebar-collapsed");
  expect(sidebar.querySelector("a:not(.ws-switch), button")).toBe(toggle());
  expect(toggle().getAttribute("aria-label")).toBe("Expand sidebar");
  expect(toggle().getAttribute("aria-expanded")).toBe("false");
  expect(toggle().getAttribute("aria-controls")).toBe(sidebar.id);
  // Each entry stays a link, its icon alone shown: named, with its name as its tooltip.
  expect(named()).toEqual(names.map((name) => [name, name]));
  expect(within(sidebar).getByRole("link", { current: "page" }).getAttribute("aria-label")).toBe("Issues");
  expect(within(sidebar).getByRole("group", { name: "Signed in" }).getAttribute("title")).toBe("ada@example.com (writer)");
  await user.click(toggle());
  expect(document.querySelector(".app")!.classList).not.toContain("sidebar-collapsed");
  expect(toggle().getAttribute("aria-label")).toBe("Collapse sidebar");
  expect(document.activeElement).toBe(toggle());
  expect(named()).toEqual(names.map(() => [null, null]));
  expect(within(sidebar).getByRole("group", { name: "Signed in" }).getAttribute("title")).toBeNull();
  // jsdom's platform is not a Mac's, so $mod is Ctrl.
  await user.keyboard("{Control>}b{/Control}");
  expect(document.querySelector(".app")!.classList).toContain("sidebar-collapsed");
  expect(toggle().getAttribute("aria-label")).toBe("Expand sidebar");
});

test("the sidebar stands opposite Chat, and moves with it when Chat's side flips", async () => {
  serve(false);
  render(<App assign={vi.fn()} />);
  await screen.findByRole("heading", { level: 1 });
  const app = document.querySelector(".app")!;
  const toggle = () => within(screen.getByRole("navigation", { name: "Sidebar" })).getByRole("button", { name: /sidebar$/ });
  // Chat is at the left by default; the sidebar is at the right, and its button points to that edge.
  const icon = (name: "panelLeftOpen" | "panelRightOpen") => render(<Icon name={name} />).container.querySelector("svg")!.innerHTML;
  // The button stands at the sidebar's outer edge, beside the logo; Search and New are the row under them.
  const header = () => [...document.querySelector(".ws-row")!.children].map((child) => child.getAttribute("aria-label") ?? child.textContent);
  const tools = () => [...document.querySelector(".sidebar-tools")!.children].map((child) => child.getAttribute("aria-label"));
  expect(app.classList).toContain("sidebar-right");
  expect(toggle().querySelector("svg")!.innerHTML).toBe(icon("panelRightOpen"));
  expect(header()).toEqual(["Trackinizer", "Expand sidebar"]);
  expect(tools()).toEqual(["Search", "New issue"]);
  act(() => setChatSide("right"));
  expect(app.classList).not.toContain("sidebar-right");
  expect(toggle().querySelector("svg")!.innerHTML).toBe(icon("panelLeftOpen"));
  expect(header()).toEqual(["Expand sidebar", "Trackinizer"]);
  expect(tools()).toEqual(["Search", "New issue"]);
});

test("a rail opens over the workbench after the pointer rests on it, and folds after it leaves; pinned, it does not", async () => {
  serve(false);
  render(<App assign={vi.fn()} />);
  await screen.findByRole("heading", { level: 1 });
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  const app = document.querySelector(".app")!;
  const dock = document.querySelector(".sidebar-dock")!;
  fireEvent.pointerEnter(dock);
  act(() => { vi.advanceTimersByTime(OPEN_AFTER_MS - 1); });
  expect(app.classList).not.toContain("sidebar-peek");
  act(() => { vi.advanceTimersByTime(1); });
  expect(app.classList).toContain("sidebar-peek");
  fireEvent.pointerLeave(dock);
  act(() => { vi.advanceTimersByTime(FOLD_AFTER_MS); });
  expect(app.classList).not.toContain("sidebar-peek");
  // Pinned open, there is nothing to peek at.
  fireEvent.click(within(dock as HTMLElement).getByRole("button", { name: "Expand sidebar" }));
  expect(app.classList).not.toContain("sidebar-collapsed");
  fireEvent.pointerEnter(dock);
  act(() => { vi.advanceTimersByTime(OPEN_AFTER_MS); });
  expect(app.classList).not.toContain("sidebar-peek");
});

test("the keyboard in an opened rail holds it open, and a link followed folds it", async () => {
  serve(false);
  render(<App assign={vi.fn()} />);
  await screen.findByRole("heading", { level: 1 });
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  const app = document.querySelector(".app")!;
  const dock = document.querySelector(".sidebar-dock")!;
  const link = within(dock as HTMLElement).getByRole("link", { name: "Console" });
  fireEvent.pointerEnter(dock);
  act(() => { vi.advanceTimersByTime(OPEN_AFTER_MS); });
  act(() => link.focus());
  fireEvent.pointerLeave(dock);
  act(() => { vi.advanceTimersByTime(FOLD_AFTER_MS); });
  expect(app.classList).toContain("sidebar-peek");
  fireEvent.click(link);
  expect(app.classList).not.toContain("sidebar-peek");
});

test("a canvas puts its controls in the sidebar's Workbench section, and the rail shows one icon that opens it there", async () => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  serve(true);
  render(<App assign={vi.fn()} />);
  await act(async () => loaded.release());
  await screen.findByRole("region", { name: "Canvas" });
  const sidebar = screen.getByRole("navigation", { name: "Sidebar" });
  const section = within(sidebar).getByText("Workbench", { selector: ".nav-section-h" }).closest(".nav-section") as HTMLElement;
  expect(section.hidden).toBe(false);
  expect(within(section).getByRole("checkbox", { name: "Note" })).toBeTruthy();
  const icon = within(section).getByTitle("Workbench");
  expect(document.querySelector(".app")!.classList).toContain("sidebar-collapsed");
  fireEvent.click(icon);
  expect(document.querySelector(".app")!.classList).toContain("sidebar-peek");
  await waitFor(() => expect(document.activeElement).toBe(within(section).getByRole("checkbox", { name: "Note" })), { interval: 1 });
});

test("without a canvas the sidebar has no Workbench section", async () => {
  serve(false);
  render(<App assign={vi.fn()} />);
  await screen.findByRole("heading", { level: 1 });
  const section = within(screen.getByRole("navigation", { name: "Sidebar" })).getByText("Workbench", { selector: ".nav-section-h" }).closest(".nav-section") as HTMLElement;
  expect(section.hidden).toBe(true);
});

test("the sidebar's theme button switches dark to light and back, kept for the browser, and stays in the rail", async () => {
  localStorage.clear();
  document.documentElement.dataset.theme = "dark";
  serve(false);
  render(<App assign={vi.fn()} />);
  await screen.findByRole("heading", { level: 1 });
  const user = userEvent.setup();
  const sidebar = within(screen.getByRole("navigation", { name: "Sidebar" }));
  await user.click(sidebar.getByRole("button", { name: "Switch to light theme" }));
  expect(document.documentElement.dataset.theme).toBe("light");
  expect(localStorage.getItem("trackinizer.theme")).toBe("light");
  await user.click(sidebar.getByRole("button", { name: "Expand sidebar" }));
  await user.click(sidebar.getByRole("button", { name: "Switch to dark theme" }));
  expect(document.documentElement.dataset.theme).toBe("dark");
  expect(localStorage.getItem("trackinizer.theme")).toBe("dark");
});

import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { stubFetch } from "../api/testing";
import { FakeEventSource } from "../live/testing";
import { App } from "./App";

// Counts the times the canvas's code is loaded: a module's factory runs once,
// when the module is first imported.
// Its chunk arrives when a test releases `gate`.
const loaded = vi.hoisted(() => {
  let release = () => {};
  const gate = new Promise<void>((resolve) => { release = resolve; });
  return { canvas: 0, preloaded: 0, gate, release };
});
vi.mock("../visuals/Canvas", async () => {
  loaded.canvas += 1;
  await loaded.gate;
  return { Canvas: ({ children }: { children: ReactNode }) => <section aria-label="Canvas">{children}</section> };
});
// The renderer the canvas shows first, which the shell loads with it.
vi.mock("../visuals/registry", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../visuals/registry")>()),
  preloadFirstRenderers: async () => {
    loaded.preloaded += 1;
  },
}));

/** Serve boot for a user whose canvas is `enabled`, and an empty list. */
function serve(enabled: boolean) {
  const bodies: { [path: string]: unknown } = {
    "/api/meta/enums": { inquiry_kind_all: ["Issue"], status: ["active"] },
    "/api/meta/fields": {},
    "/api/meta/edges": {},
    "/api/me/profile": { user_id: "u", email: "ada@example.com", name: "Ada", role: "writer", last_login: null, visual_workspace_enabled: enabled },
    "/api/inquiries": [],
  };
  stubFetch((request) => Response.json(bodies[new URL(request.url).pathname]));
}

beforeEach(() => {
  history.replaceState(null, "", "#/list/Issue");
  sessionStorage.clear();
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  cleanup();
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
  // The canvas came with its first renderer, so its first render suspends on nothing.
  expect(loaded.preloaded).toBe(1);
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

test("the sidebar's button, or ⌘B / Ctrl+B, collapses it to a rail of its entries, named, titled and in order, and expands it again", async () => {
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
  expect(toggle().getAttribute("aria-label")).toBe("Collapse sidebar");
  await user.click(toggle());
  expect(document.querySelector(".app")!.classList).toContain("sidebar-collapsed");
  // The same nav, as a rail: the button that expands it comes first, in the
  // keyboard's order too, and takes the focus of the one clicked.
  expect(screen.getAllByRole("navigation", { name: "Sidebar" })).toEqual([sidebar]);
  expect(sidebar.querySelector("a, button")).toBe(toggle());
  expect(toggle().getAttribute("aria-label")).toBe("Expand sidebar");
  expect(toggle().getAttribute("aria-expanded")).toBe("false");
  expect(toggle().getAttribute("aria-controls")).toBe(sidebar.id);
  expect(document.activeElement).toBe(toggle());
  // Each entry stays a link, its icon alone shown: named, with its name as its tooltip.
  expect(named()).toEqual(names.map((name) => [name, name]));
  expect(within(sidebar).getByRole("link", { current: "page" }).getAttribute("aria-label")).toBe("Issues");
  expect(within(sidebar).getByRole("group", { name: "Signed in" }).getAttribute("title")).toBe("ada@example.com (writer)");
  // jsdom's platform is not a Mac's, so $mod is Ctrl.
  await user.keyboard("{Control>}b{/Control}");
  expect(document.querySelector(".app")!.classList).not.toContain("sidebar-collapsed");
  expect(toggle().getAttribute("aria-label")).toBe("Collapse sidebar");
  expect(named()).toEqual(names.map(() => [null, null]));
  expect(within(sidebar).getByRole("group", { name: "Signed in" }).getAttribute("title")).toBeNull();
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
  await user.click(sidebar.getByRole("button", { name: "Collapse sidebar" }));
  await user.click(sidebar.getByRole("button", { name: "Switch to dark theme" }));
  expect(document.documentElement.dataset.theme).toBe("dark");
  expect(localStorage.getItem("trackinizer.theme")).toBe("dark");
});

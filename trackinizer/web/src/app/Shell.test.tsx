import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { AGREED, stubFetch } from "../api/testing";
import { FakeEventSource } from "../live/testing";
import { App } from "./App";

/** Serve boot for a user of `role`, and an empty list; a preference change answers as the server merges it. */
function serve(role = "writer") {
  const bodies: { [path: string]: unknown } = {
    "/api/meta/enums": { inquiry_kind_all: ["Issue"], status: ["active"] },
    "/api/meta/fields": {},
    "/api/meta/edges": {},
    "/api/me/profile": { user_id: "u", email: "ada@example.com", name: "Ada", role, last_login: null, visual_canvas_enabled: false, ...AGREED },
    "/api/inquiries": [],
  };
  return stubFetch(async (request) => {
    const path = new URL(request.url).pathname;
    if (request.method === "PATCH" && path === "/api/me/preferences") return Response.json({ theme: "dark", ...(await request.json()) });
    return Response.json(bodies[path]);
  });
}

beforeEach(() => {
  history.replaceState(null, "", "/trax/list/Issue");
  sessionStorage.clear();
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("the sidebar lists Graph, Console, Activity, each kind and Your settings; Admin is an admin's alone", async () => {
  serve();
  render(<App assign={vi.fn()} />);
  await screen.findByRole("heading", { level: 1 });
  const sidebar = screen.getByRole("navigation", { name: "Sidebar" });
  expect([...sidebar.querySelectorAll("a.nav-item")].map((entry) => entry.textContent)).toEqual(["Graph", "Console", "Activity", "Issues", "Your settings"]);
  expect(within(sidebar).getByRole("link", { current: "page" }).textContent).toBe("Issues");
  expect(within(sidebar).getByRole("link", { name: "Graph" }).getAttribute("href")).toBe("/trax/graph");
  cleanup();
  serve("admin");
  render(<App assign={vi.fn()} />);
  await screen.findByRole("heading", { level: 1 });
  expect(within(screen.getByRole("navigation", { name: "Sidebar" })).getByRole("link", { name: "Admin" }).getAttribute("href")).toBe("/trax/admin");
});

test("Your settings opens as the page, in place of the list", async () => {
  serve();
  render(<App assign={vi.fn()} />);
  await screen.findByRole("heading", { level: 1, name: "Issues" });
  await userEvent.setup().click(within(screen.getByRole("navigation", { name: "Sidebar" })).getByRole("link", { name: "Your settings" }));
  expect(await screen.findByRole("heading", { level: 1, name: "Your settings" })).toBeTruthy();
  expect(screen.queryByRole("heading", { level: 1, name: "Issues" })).toBeNull();
  expect(location.pathname).toBe("/trax/settings");
});

test("the sidebar's button, or ⌘B / Ctrl+B, collapses it to a rail of its entries, named, titled and in order, and expands it again", async () => {
  serve();
  render(<App assign={vi.fn()} />);
  await screen.findByRole("heading", { level: 1 });
  const user = userEvent.setup();
  const sidebar = screen.getByRole("navigation", { name: "Sidebar" });
  const toggle = () => within(sidebar).getByRole("button", { name: /sidebar$/ });
  const entries = () => [...sidebar.querySelectorAll("a.nav-item")];
  const named = () => entries().map((entry) => [entry.getAttribute("aria-label"), entry.getAttribute("title")]);
  const names = entries().map((entry) => entry.textContent);
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

test("the sidebar's theme button switches dark to light and back, kept in the user's preferences, and stays in the rail", async () => {
  document.documentElement.dataset.theme = "dark";
  const server = serve();
  render(<App assign={vi.fn()} />);
  await screen.findByRole("heading", { level: 1 });
  const user = userEvent.setup();
  const sidebar = within(screen.getByRole("navigation", { name: "Sidebar" }));
  const themes = () => server.filter(({ method, path }) => method === "PATCH" && path === "/api/me/preferences").map(({ body }) => body);
  await user.click(sidebar.getByRole("button", { name: "Switch to light theme" }));
  expect(document.documentElement.dataset.theme).toBe("light");
  await waitFor(() => expect(themes()).toEqual([{ theme: "light" }]));
  await user.click(sidebar.getByRole("button", { name: "Collapse sidebar" }));
  await user.click(sidebar.getByRole("button", { name: "Switch to dark theme" }));
  expect(document.documentElement.dataset.theme).toBe("dark");
  await waitFor(() => expect(themes()).toEqual([{ theme: "light" }, { theme: "dark" }]));
});

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, beforeEach, expect, test, vi } from "vitest";
import { stubFetch } from "../api/testing";
import { App } from "../app/App";
import { FakeEventSource } from "../live/testing";
import { AdminView } from "../router/views";

/** Serve boot for a user of `role`; every other read answers empty. */
function boot(role: string) {
  return stubFetch((request) => {
    const path = new URL(request.url).pathname;
    if (path === "/api/meta/enums") return Response.json({ inquiry_kind_all: ["Issue"], status: ["active"] });
    if (path === "/api/me/profile") return Response.json({ user_id: "u1", email: "ada@example.com", name: "Ada", role, last_login: null });
    if (path === "/api/admin/users") return Response.json({ users: [] });
    if (path === "/api/admin/allowlist") return Response.json({ entries: [] });
    if (path === "/api/me/tokens") return Response.json({ tokens: [] });
    return Response.json(path === "/api/inquiries" ? [] : {});
  });
}

/** The palette's commands whose titles start with "Go to". */
async function commandTitles(user: ReturnType<typeof userEvent.setup>) {
  await user.keyboard("{Control>}k{/Control}");
  const palette = await screen.findByRole("dialog", { name: "Command menu" });
  fireEvent.change(within(palette).getByRole("combobox", { name: "Command" }), { target: { value: "Go to" } });
  const titles = within(palette)
    .getAllByRole("option")
    .map((option) => option.textContent);
  await user.keyboard("{Escape}");
  return titles;
}

// Admin is a chunk of its own; loaded first, it shows at once, as it does once
// the app has loaded it.
beforeAll(() => AdminView.preload());

beforeEach(() => {
  history.replaceState(null, "", "#/list/Issue");
  localStorage.clear();
  sessionStorage.clear();
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("a non-admin has Your settings but no Admin entry or command", async () => {
  boot("writer");
  const user = userEvent.setup();
  render(<App assign={vi.fn()} />);
  await screen.findByLabelText("Signed in");
  const sidebar = within(screen.getByRole("navigation", { name: "Sidebar" }));
  expect(sidebar.getByRole("link", { name: "Your settings" }).getAttribute("href")).toBe("#/settings");
  expect(sidebar.queryByRole("link", { name: "Admin" })).toBeNull();
  const titles = await commandTitles(user);
  expect(titles).toContain("Go to Your settings");
  expect(titles.some((title) => title?.includes("Admin"))).toBe(false);
});

test("a non-admin's link to Admin is refused, and asks nothing of the admin routes", async () => {
  const sent = boot("writer");
  render(<App assign={vi.fn()} />);
  await screen.findByLabelText("Signed in");
  history.pushState(null, "", "#/admin");
  dispatchEvent(new HashChangeEvent("hashchange"));
  expect(await screen.findByRole("heading", { name: "Admins only" })).toBeTruthy();
  expect(sent.filter((request) => request.path.startsWith("/api/admin/"))).toEqual([]);
});

test("an admin demoted since boot opens Admin: it waits for the profile it refetches and asks nothing of the admin routes", async () => {
  let role = "admin";
  let release = () => {};
  const held = new Promise<void>((resolve) => (release = resolve));
  const sent = stubFetch(async (request) => {
    const path = new URL(request.url).pathname;
    if (path === "/api/me/profile") {
      if (role !== "admin") await held;
      return Response.json({ user_id: "u1", email: "ada@example.com", name: "Ada", role, last_login: null });
    }
    if (path === "/api/meta/enums") return Response.json({ inquiry_kind_all: ["Issue"], status: ["active"] });
    return Response.json(path === "/api/inquiries" ? [] : { users: [], entries: [] });
  });
  render(<App assign={vi.fn()} />);
  await screen.findByLabelText("Signed in");
  role = "viewer";
  history.pushState(null, "", "#/admin");
  dispatchEvent(new HashChangeEvent("hashchange"));
  await screen.findByRole("heading", { level: 1, name: "Admin" });
  await waitFor(() => expect(sent.filter((request) => request.path === "/api/me/profile")).toHaveLength(2));
  expect(screen.queryByRole("region", { name: "Users" })).toBeNull();
  release();
  expect(await screen.findByRole("heading", { name: "Admins only" })).toBeTruthy();
  expect(sent.filter((request) => request.path.startsWith("/api/admin/"))).toEqual([]);
});

test("an admin has the Admin command", async () => {
  boot("admin");
  const user = userEvent.setup();
  render(<App assign={vi.fn()} />);
  await screen.findByLabelText("Signed in");
  expect(await commandTitles(user)).toContain("Go to Admin");
});

test("an admin has the Admin entry, and the link opens users and the allowlist", async () => {
  boot("admin");
  const user = userEvent.setup();
  render(<App assign={vi.fn()} />);
  await screen.findByLabelText("Signed in");
  const sidebar = within(screen.getByRole("navigation", { name: "Sidebar" }));
  await user.click(sidebar.getByRole("link", { name: "Admin" }));
  await waitFor(() => expect(location.hash).toBe("#/admin"));
  expect(sidebar.getByRole("link", { name: "Admin" }).getAttribute("aria-current")).toBe("page");
  expect(await screen.findByRole("region", { name: "Users" })).toBeTruthy();
  expect(screen.getByRole("region", { name: "Allowlist" })).toBeTruthy();
});

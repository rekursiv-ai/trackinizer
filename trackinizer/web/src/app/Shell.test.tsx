import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { AGREED, stubFetch } from "../api/testing";
import { FakeEventSource } from "../live/testing";
import { setChatSide } from "../visuals/chatColumnState";
import { ACCOUNTS_KEY, readAccounts } from "./accounts";
import { App } from "./App";
import { LOGIN_URL } from "./session";
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
  // The real canvas draws its controls into the top bar's Workbench menu.
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
  return {
    ChatColumn: ({ children, sheet }: { children: ReactNode; sheet: ReactNode }) => (
      <div data-testid="chat-column">{children}{sheet}</div>
    ),
  };
});

/** Serve boot for a user (`email`, `role`) whose canvas is `enabled`, and an empty list; `logout` answers a sign-out. */
function serve(enabled: boolean, { email = "ada@example.com", role = "writer", logout = () => new Response(null, { status: 204 }) }: {
  readonly email?: string;
  readonly role?: string;
  readonly logout?: () => Response;
} = {}) {
  const DEFAULTS = { theme: "dark", chat_side: "left", highlight_mentions: true, terminal_font_size: "13px" };
  const bodies: { [path: string]: unknown } = {
    "/api/meta/enums": { inquiry_kind_all: ["Issue"], status: ["active"] },
    "/api/meta/fields": {},
    "/api/meta/edges": {},
    "/api/me/profile": { user_id: "u", email, name: "Ada", role, last_login: null, visual_workspace_enabled: enabled, ...AGREED },
    "/api/inquiries": [],
  };
  return stubFetch(async (request) => {
    const path = new URL(request.url).pathname;
    // The server's merge of a preference change: the keys sent, the defaults for the rest.
    if (request.method === "PATCH" && path === "/api/me/preferences") return Response.json({ ...DEFAULTS, ...(await request.json()) });
    if (path === "/auth/logout") return logout();
    return Response.json(bodies[path]);
  });
}

beforeEach(() => {
  history.replaceState(null, "", "#/list/Issue");
  sessionStorage.clear();
  localStorage.clear();
  setChatSide("left");
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
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
  act(() => {
    window.location.hash = "#/settings";
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  });
  expect(screen.queryByRole("complementary", { name: "Your settings" })).toBeNull();
  await act(async () => loaded.release());
  const canvas = await screen.findByRole("region", { name: "Canvas" });
  expect(canvas.querySelector("h1")?.textContent).toBe("Issues");
  const sheet = await screen.findByRole("complementary", { name: "Your settings" });
  const name = await within(sheet).findByRole("textbox", { name: "Name" });
  expect(document.querySelector(".view[aria-busy=true]")).toBeNull();
  fireEvent.change(name, { target: { value: "Ada's other name" } });
  expect((name as HTMLInputElement).value).toBe("Ada's other name");
  // Chat's column came with the canvas, around it, so neither first render suspends.
  expect(loaded.column).toBe(1);
  expect(screen.getByTestId("chat-column").contains(canvas)).toBe(true);
  expect(screen.getByTestId("chat-column").contains(sheet)).toBe(true);
});

test("a user with the canvas on sees the list inside the canvas", async () => {
  await act(async () => loaded.release());
  serve(true);
  render(<App assign={vi.fn()} />);
  const canvas = await screen.findByRole("region", { name: "Canvas" });
  expect(canvas.querySelector("h1")?.textContent).toBe("Issues");
  expect(loaded.canvas).toBe(1);
});

test("Settings opens as a sheet over the page, which stays in its canvas under it, and goes on the way back", async () => {
  await act(async () => loaded.release());
  serve(true);
  render(<App assign={vi.fn()} />);
  const canvas = await screen.findByRole("region", { name: "Canvas" });
  act(() => { window.location.hash = "#/settings"; });
  const sheet = await screen.findByRole("complementary", { name: "Your settings" });
  expect((await within(sheet).findByRole("heading", { level: 1 })).textContent).toBe("Your settings");
  // The list is the same one, mounted once, in the same canvas.
  expect(screen.getByRole("region", { name: "Canvas" })).toBe(canvas);
  expect(canvas.querySelector("h1")?.textContent).toBe("Issues");
  act(() => { window.location.hash = "#/list/Issue"; });
  await waitFor(() => expect(screen.queryByRole("complementary", { name: "Your settings" })).toBeNull(), { interval: 1 });
  expect(screen.getByRole("region", { name: "Canvas" })).toBe(canvas);
});

test("the top bar holds the logo, Go, Search, New, its pin, Machines and the account in order; with no canvas or workstreams", async () => {
  serve(false);
  render(<App assign={vi.fn()} />);
  await screen.findByRole("heading", { level: 1 });
  const bar = screen.getByRole("banner", { name: "App" });
  const named = [...bar.querySelectorAll<HTMLElement>("a, button")].filter((each) => !each.closest("[hidden]"))
    .map((each) => each.getAttribute("aria-label"));
  expect(named).toEqual(["Trackinizer, the graph", "Go to", "Search", "New issue", "Keep app bar open", "Machines", "Account: ada@example.com, writer"]);
  expect(within(bar).queryByRole("navigation", { name: "Workstreams" })).toBeNull();
  expect(document.querySelector(".app")!.classList).not.toContain("with-canvas");
});

test("Go lists Graph, Console, Activity and each kind, the page on show marked; a link followed closes it, and Esc gives focus back", async () => {
  serve(false);
  render(<App assign={vi.fn()} />);
  await screen.findByRole("heading", { level: 1 });
  const user = userEvent.setup();
  const go = screen.getByRole("button", { name: "Go to" });
  await user.click(go);
  const menu = screen.getByRole("navigation", { name: "Go to" });
  expect(within(menu).getAllByRole("link").map((link) => link.textContent)).toEqual(["GraphG G", "ConsoleG C", "ActivityG A", "Issues"]);
  expect(within(menu).getByRole("link", { current: "page" }).textContent).toBe("Issues");
  await user.click(within(menu).getByRole("link", { name: /^Console/ }));
  await screen.findByRole("heading", { level: 1, name: /^Console/ });
  expect(screen.queryByRole("navigation", { name: "Go to" })).toBeNull();
  await user.click(go);
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("navigation", { name: "Go to" })).toBeNull();
  expect(document.activeElement).toBe(go);
});

test("a canvas puts its controls in the Workbench menu, which its sliders button opens; the app frames the canvas", async () => {
  serve(true);
  render(<App assign={vi.fn()} />);
  await act(async () => loaded.release());
  await screen.findByRole("region", { name: "Canvas" });
  expect(document.querySelector(".app")!.classList).toContain("with-canvas");
  const button = screen.getByRole("button", { name: "Workbench" });
  const menu = document.querySelector<HTMLElement>(".workbench-menu")!;
  // The slot stays mounted while the menu is closed, so the canvas's controls are always drawn into it.
  expect(menu.hidden).toBe(true);
  expect(within(menu).getByRole("checkbox", { name: "Note", hidden: true })).toBeTruthy();
  fireEvent.click(button);
  expect(menu.hidden).toBe(false);
  expect(button.getAttribute("aria-expanded")).toBe("true");
});

test("the account menu names the account, then Your settings, Appearance, the other accounts and Sign out last; Admin is an admin's alone", async () => {
  serve(false);
  render(<App assign={vi.fn()} />);
  await screen.findByRole("heading", { level: 1 });
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Account: ada@example.com, writer" }));
  const menu = screen.getByRole("dialog", { name: "Account" });
  expect(within(menu).getByText("ada@example.com", { selector: "strong" })).toBeTruthy();
  expect(within(menu).queryByRole("link", { name: "Admin" })).toBeNull();
  const rows = [...menu.querySelectorAll<HTMLElement>(".topbar-item")].map((row) => row.textContent);
  expect(rows).toEqual(["Your settings", "Add another account", "Sign out"]);
  cleanup();
  serve(false, { role: "admin" });
  render(<App assign={vi.fn()} />);
  await screen.findByRole("heading", { level: 1 });
  await user.click(screen.getByRole("button", { name: "Account: ada@example.com, admin" }));
  expect(within(screen.getByRole("dialog", { name: "Account" })).getByRole("link", { name: "Admin" })).toBeTruthy();
});

test("a skin shows at once and is kept in the user's preferences; the theme is offered with the Default skin alone", async () => {
  document.documentElement.dataset.theme = "dark";
  const sent = serve(false);
  render(<App assign={vi.fn()} />);
  await screen.findByRole("heading", { level: 1 });
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: /^Account:/ }));
  const menu = within(screen.getByRole("dialog", { name: "Account" }));
  await user.click(menu.getByRole("button", { name: "Light" }));
  expect(document.documentElement.dataset.theme).toBe("light");
  await user.click(menu.getByRole("button", { name: "DOS 80s" }));
  expect(document.documentElement.dataset.skin).toBe("dos");
  expect(menu.getByRole("button", { name: "DOS 80s" }).getAttribute("aria-pressed")).toBe("true");
  expect(menu.queryByRole("group", { name: "Theme" })).toBeNull();
  await user.click(menu.getByRole("button", { name: "Default" }));
  expect(document.documentElement.dataset.skin).toBeUndefined();
  expect(menu.getByRole("group", { name: "Theme" })).toBeTruthy();
  await waitFor(() => expect(sent.filter((request) => request.path === "/api/me/preferences").map((request) => request.body))
    .toEqual([{ theme: "light" }, { skin: "dos" }, { skin: "default" }]));
  // The browser keeps the accounts it signed in as, and nothing of the user's preferences.
  expect(Object.keys(localStorage)).toEqual([ACCOUNTS_KEY]);
});

test("the browser remembers the account signed in; another it remembers is listed with its role and kind, signs in by Google with it picked, and × forgets it", async () => {
  localStorage.setItem(ACCOUNTS_KEY, JSON.stringify([{ email: "lead@acme-robotics.com", name: "Lead", role: "admin", seen: "2026-10-13T09:00:00.000Z" }]));
  serve(false);
  const assign = vi.fn();
  render(<App assign={assign} />);
  await screen.findByRole("heading", { level: 1 });
  await waitFor(() => expect(readAccounts().map((each) => each.email)).toEqual(["ada@example.com", "lead@acme-robotics.com"]), { interval: 1 });
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: /^Account:/ }));
  const others = within(screen.getByRole("region", { name: "Other accounts" }));
  const lead = others.getByTitle("Sign in as lead@acme-robotics.com");
  expect(lead.textContent).toBe("Llead@acme-robotics.comadmin · acme-robotics.comWork");
  await user.click(lead);
  await waitFor(() => expect(assign).toHaveBeenCalledWith("/auth/login?next=%2Fapp%2F&login_hint=lead%40acme-robotics.com"), { interval: 1 });
  await user.click(others.getByRole("button", { name: "Forget lead@acme-robotics.com on this browser" }));
  expect(readAccounts().map((each) => each.email)).toEqual(["ada@example.com"]);
  expect(others.queryByTitle("Sign in as lead@acme-robotics.com")).toBeNull();
});

test("Sign out, last in the account menu, posts to /auth/logout, then loads the login page", async () => {
  const sent = serve(false);
  const assign = vi.fn();
  render(<App assign={assign} />);
  await screen.findByRole("heading", { level: 1 });
  fireEvent.click(screen.getByRole("button", { name: /^Account:/ }));
  fireEvent.click(within(screen.getByRole("dialog", { name: "Account" })).getByRole("button", { name: "Sign out" }));
  await waitFor(() => expect(assign).toHaveBeenCalledWith(LOGIN_URL), { interval: 1 });
  expect(sent.filter((request) => request.path === "/auth/logout").map((request) => request.method)).toEqual(["POST"]);
});

test("a refused sign-out says why in a toast, and stays on the page", async () => {
  serve(false, { logout: () => Response.json({ detail: "cross-origin logout rejected" }, { status: 403 }) });
  const assign = vi.fn();
  render(<App assign={assign} />);
  await screen.findByRole("heading", { level: 1 });
  fireEvent.click(screen.getByRole("button", { name: /^Account:/ }));
  fireEvent.click(within(screen.getByRole("dialog", { name: "Account" })).getByRole("button", { name: "Sign out" }));
  expect(await screen.findByText("Could not sign out. cross-origin logout rejected")).toBeTruthy();
  expect(assign).not.toHaveBeenCalled();
});

test("under --no-auth, where nobody signs in, the account menu offers no other account and no sign out, and remembers none", async () => {
  serve(false, { email: "no-auth@localhost" });
  render(<App assign={vi.fn()} />);
  await screen.findByRole("heading", { level: 1 });
  fireEvent.click(screen.getByRole("button", { name: /^Account:/ }));
  const menu = within(screen.getByRole("dialog", { name: "Account" }));
  expect(menu.queryByRole("button", { name: "Sign out" })).toBeNull();
  expect(menu.queryByRole("region", { name: "Other accounts" })).toBeNull();
  expect(readAccounts()).toEqual([]);
});

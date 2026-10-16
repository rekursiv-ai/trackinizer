import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import noAuth from "../../test/testdata/me/getProfile.json";
import type { OrgMode, Profile } from "../api/me";
import { LOGIN_URL } from "../app/session";
import { DEFAULT_PREFERENCES, receivePreferences, sendWaiting } from "../preferences";
import { startTrail } from "../router/trail";
import { chooseTheme, installTheme } from "../theme";
import { SettingsView } from ".";
import { FAST, profile, renderScreen, serveAccount } from "./testing";

const ADA = profile("writer");

beforeEach(() => {
  localStorage.clear();
});

afterEach(async () => {
  cleanup();
  // The page's theme is the module's, which outlives a test: each starts on the
  // default, once the test's own sends have been answered.
  await sendWaiting();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  receivePreferences(DEFAULT_PREFERENCES);
});

test("the page shows the profile, refetched on open, and every section", async () => {
  const server = serveAccount(ADA);
  renderScreen(<SettingsView assign={vi.fn()} />, ADA);
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Your settings");
  const shown = within(screen.getByRole("region", { name: "Profile" }));
  expect(shown.getByText("Name").nextSibling?.textContent).toBe("Ada");
  expect(shown.getByText("Email").nextSibling?.textContent).toBe("ada@example.com");
  expect(shown.getByText("Role").nextSibling?.textContent).toBe("writer");
  const sections = screen.getAllByRole("region").filter((region) => region.matches("section"));
  expect(sections.map((region) => region.getAttribute("aria-label"))).toEqual([
    "Profile",
    "Names that mean you",
    "Appearance",
    "Preferences document",
    "Agent workspace",
    "Chat partner",
    "API tokens",
    "Session",
  ]);
  await waitFor(() => expect(server.reads("/api/me/profile")).toBe(1), FAST);
});

test("Close goes back to the last page before Settings, or to the graph when there was none", async () => {
  serveAccount(ADA);
  const close = () => screen.getByRole("link", { name: "Close settings" }).getAttribute("href");
  history.replaceState(null, "", "#/settings");
  let stop = startTrail();
  renderScreen(<SettingsView assign={vi.fn()} />, ADA);
  expect(close()).toBe("#/graph");
  cleanup();
  stop();
  history.replaceState(null, "", "#/list/Paper");
  stop = startTrail();
  for (const hash of ["#/admin", "#/settings"]) {
    location.hash = hash;
    await waitFor(() => expect(location.hash).toBe(hash));
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  }
  renderScreen(<SettingsView assign={vi.fn()} />, ADA);
  expect(close()).toBe("#/list/Paper");
  stop();
  history.replaceState(null, "", "#/");
});

test("agent workspace opt-in updates the account preference", async () => {
  const server = serveAccount(ADA);
  renderScreen(<SettingsView assign={vi.fn()} />, ADA);
  const workspace = within(screen.getByRole("region", { name: "Agent workspace" }));
  const toggle = workspace.getByRole("checkbox", { name: "Enable agent-guided workbench" });
  expect((toggle as HTMLInputElement).checked).toBe(false);
  fireEvent.click(toggle);
  await waitFor(() => expect(server.writes()).toContainEqual({
    call: "PUT /api/me/visual-workspace", body: { enabled: true },
  }), FAST);
  await waitFor(() => expect((toggle as HTMLInputElement).checked).toBe(true), FAST);
});

/** The preference changes sent to the server, in order. */
const preferenceWrites = (server: ReturnType<typeof serveAccount>) =>
  server.sent.filter((request) => request.path === "/api/me/preferences").map((request) => [request.method, request.body]);

test("the switch for highlighting what the assistant mentions is on, and turns off at once, kept in the user's preferences", async () => {
  const server = serveAccount(ADA);
  renderScreen(<SettingsView assign={vi.fn()} />, ADA);
  const workspace = within(screen.getByRole("region", { name: "Agent workspace" }));
  const toggle = workspace.getByRole("checkbox", { name: "Highlight rows the assistant mentions" }) as HTMLInputElement;
  expect(toggle.checked).toBe(true);
  fireEvent.click(toggle);
  expect(toggle.checked).toBe(false);
  fireEvent.click(toggle);
  expect(toggle.checked).toBe(true);
  await waitFor(() => expect(preferenceWrites(server)).toEqual([
    ["PATCH", { highlight_mentions: false }],
    ["PATCH", { highlight_mentions: true }],
  ]));
  expect(localStorage.getItem(`trackinizer.v2.highlight-mentions.${ADA.email}`)).toBeNull();
});

test("a theme chosen updates the page at once and is kept in the user's preferences", async () => {
  const server = serveAccount(ADA);
  renderScreen(<SettingsView assign={vi.fn()} />, ADA);
  const appearance = within(screen.getByRole("region", { name: "Appearance" }));
  fireEvent.change(appearance.getByRole("combobox", { name: "Theme" }), { target: { value: "light" } });
  expect(document.documentElement.dataset.theme).toBe("light");
  await waitFor(() => expect(preferenceWrites(server)).toEqual([["PATCH", { theme: "light" }]]));
  expect(localStorage.getItem("trackinizer.theme")).toBeNull();
});

test("a skin chosen in Appearance shows at once and is kept; under a fixed skin the theme waits, as it changes nothing", async () => {
  const server = serveAccount(ADA);
  renderScreen(<SettingsView assign={vi.fn()} />, ADA);
  const appearance = within(screen.getByRole("region", { name: "Appearance" }));
  const skin = appearance.getByRole<HTMLSelectElement>("combobox", { name: "Skin" });
  const theme = appearance.getByRole<HTMLSelectElement>("combobox", { name: "Theme" });
  expect(skin.value).toBe("default");
  expect(theme.disabled).toBe(false);
  expect(Array.from(skin.options, (option) => option.textContent)).toEqual([
    "Default", "Retro 70s", "Sci-fi", "DOS 80s", "8-bit Console", "Matrix", "Hacker", "Minimalist", "Brutalist",
  ]);
  fireEvent.change(skin, { target: { value: "dos" } });
  expect(document.documentElement.dataset.skin).toBe("dos");
  expect(theme.disabled).toBe(true);
  expect(appearance.getByText("The theme colours the Default skin only.")).toBeTruthy();
  await waitFor(() => expect(preferenceWrites(server)).toEqual([["PATCH", { skin: "dos" }]]));
  fireEvent.change(skin, { target: { value: "default" } });
  expect(document.documentElement.dataset.skin).toBeUndefined();
  expect(theme.disabled).toBe(false);
});

test("Chat style is Default until Terminal is picked, which marks the page and is kept in the user's preferences", async () => {
  const server = serveAccount(ADA);
  renderScreen(<SettingsView assign={vi.fn()} />, ADA);
  const style = within(screen.getByRole("region", { name: "Appearance" })).getByRole<HTMLSelectElement>("combobox", { name: "Chat style" });
  expect(style.value).toBe("default");
  expect(Array.from(style.options, (option) => option.textContent)).toEqual(["Default", "Terminal"]);
  fireEvent.change(style, { target: { value: "terminal" } });
  expect(style.value).toBe("terminal");
  expect(document.documentElement.dataset.chatStyle).toBe("terminal");
  await waitFor(() => expect(preferenceWrites(server)).toEqual([["PATCH", { chat_style: "terminal" }]]));
});

test("the theme picked elsewhere, as by the sidebar's button, shows in Appearance at once", () => {
  serveAccount(ADA);
  renderScreen(<SettingsView assign={vi.fn()} />, ADA);
  const theme = within(screen.getByRole("region", { name: "Appearance" })).getByRole("combobox", { name: "Theme" });
  expect((theme as HTMLSelectElement).value).toBe("dark");
  act(() => chooseTheme("light"));
  expect((theme as HTMLSelectElement).value).toBe("light");
});

test("a theme chosen holds, in Appearance too, and the OS's appearance leaves it be", () => {
  const os = { matches: false, changed: () => {}, addEventListener: (_: string, listener: () => void) => (os.changed = listener) };
  vi.stubGlobal("matchMedia", () => os);
  serveAccount(ADA);
  installTheme();
  chooseTheme("system");
  renderScreen(<SettingsView assign={vi.fn()} />, ADA);
  const theme = within(screen.getByRole("region", { name: "Appearance" })).getByRole<HTMLSelectElement>("combobox", { name: "Theme" });
  expect(theme.value).toBe("system");
  fireEvent.change(theme, { target: { value: "light" } });
  expect(theme.value).toBe("light");
  os.matches = true;
  act(() => os.changed());
  expect(document.documentElement.dataset.theme).toBe("light");
  expect(theme.value).toBe("light");
});

test("a failed profile refetch shows its error and Retry beside the profile already read", async () => {
  const server = serveAccount(ADA);
  server.failing.add("/api/me/profile");
  renderScreen(<SettingsView assign={vi.fn()} />, ADA);
  const shown = within(screen.getByRole("region", { name: "Profile" }));
  const alert = await shown.findByRole("alert");
  expect(alert.textContent).toContain("database unavailable");
  expect(shown.getByText("Role").nextSibling?.textContent).toBe("writer");
  server.failing.clear();
  fireEvent.click(within(alert).getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(shown.queryByRole("alert")).toBeNull());
  expect(server.reads("/api/me/profile")).toBe(2);
});

test("Sign out posts to /auth/logout, then loads the login page", async () => {
  const server = serveAccount(ADA);
  const assign = vi.fn();
  renderScreen(<SettingsView assign={assign} />, ADA);
  fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
  await waitFor(() => expect(assign).toHaveBeenCalledWith(LOGIN_URL), FAST);
  expect(server.writes()).toEqual([{ call: "POST /auth/logout", body: undefined }]);
});

test("under --no-auth, where nobody signs in, the page offers no sign out", () => {
  // The fixture is the profile a server run with --no-auth answers; a JSON import
  // widens its preferences' and org mode's words to `string`, where they are the
  // server's own.
  const body = noAuth.response.body;
  const everyone: Profile = {
    ...body,
    preferences: { ...body.preferences, workstreams: DEFAULT_PREFERENCES.workstreams } as Profile["preferences"],
    org_mode: body.org_mode as OrgMode,
  };
  serveAccount(everyone);
  renderScreen(<SettingsView assign={vi.fn()} />, everyone);
  expect(screen.getByRole("region", { name: "Profile" })).toBeTruthy();
  expect(screen.queryByRole("region", { name: "Session" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Sign out" })).toBeNull();
});

test("a refused sign-out shows why, and stays on the page", async () => {
  const server = serveAccount(ADA);
  server.answers.push(() => Response.json({ detail: "cross-origin logout rejected" }, { status: 403 }));
  const assign = vi.fn();
  renderScreen(<SettingsView assign={assign} />, ADA);
  fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
  const session = within(screen.getByRole("region", { name: "Session" }));
  expect((await session.findByRole("alert")).textContent).toBe("Could not sign out. cross-origin logout rejected");
  expect(assign).not.toHaveBeenCalled();
});

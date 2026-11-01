import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import noAuth from "../../test/testdata/me/getProfile.json";
import type { Profile } from "../api/me";
import { LOGIN_URL } from "../app/session";
import { DEFAULT_PREFERENCES, receivePreferences, sendWaiting } from "../preferences";
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

/** The preference changes sent to the server, in order. */
const preferenceWrites = (server: ReturnType<typeof serveAccount>) =>
  server.sent.filter((request) => request.path === "/api/me/preferences").map((request) => [request.method, request.body]);

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
  // widens its preferences' words to `string`, where they are the server's own.
  const body = noAuth.response.body;
  const everyone: Profile = {
    ...body,
    preferences: { ...DEFAULT_PREFERENCES, ...body.preferences } as Profile["preferences"],
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

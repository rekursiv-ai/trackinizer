import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import noAuth from "../../test/testdata/me/getProfile.json";
import { LOGIN_URL } from "../app/session";
import { chooseTheme, installTheme } from "../theme";
import { SettingsView } from ".";
import { FAST, profile, renderScreen, serveAccount } from "./testing";

const ADA = profile("writer");

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  // The page's theme is the module's, which outlives a test: each starts on the default.
  chooseTheme("dark");
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
    "Agent workspace",
    "Chat partner",
    "API tokens",
    "This browser",
    "Session",
  ]);
  await waitFor(() => expect(server.reads("/api/me/profile")).toBe(1), FAST);
});

test("agent workspace opt-in updates the account preference", async () => {
  const server = serveAccount(ADA);
  renderScreen(<SettingsView assign={vi.fn()} />, ADA);
  const workspace = within(screen.getByRole("region", { name: "Agent workspace" }));
  const toggle = workspace.getByRole("checkbox", { name: "Enable agent-guided canvas" });
  expect((toggle as HTMLInputElement).checked).toBe(false);
  fireEvent.click(toggle);
  await waitFor(() => expect(server.writes()).toContainEqual({
    call: "PUT /api/me/visual-workspace", body: { enabled: true },
  }), FAST);
  await waitFor(() => expect((toggle as HTMLInputElement).checked).toBe(true), FAST);
});

test("appearance choice persists and updates the page immediately", () => {
  serveAccount(ADA);
  renderScreen(<SettingsView assign={vi.fn()} />, ADA);
  const appearance = within(screen.getByRole("region", { name: "Appearance" }));
  fireEvent.change(appearance.getByRole("combobox", { name: "Theme" }), { target: { value: "light" } });
  expect(document.documentElement.dataset.theme).toBe("light");
  expect(localStorage.getItem("trackinizer.theme")).toBe("light");
});

test("the theme picked elsewhere, as by the sidebar's button, shows in Appearance at once", () => {
  serveAccount(ADA);
  renderScreen(<SettingsView assign={vi.fn()} />, ADA);
  const theme = within(screen.getByRole("region", { name: "Appearance" })).getByRole("combobox", { name: "Theme" });
  expect((theme as HTMLSelectElement).value).toBe("dark");
  act(() => chooseTheme("light"));
  expect((theme as HTMLSelectElement).value).toBe("light");
});

test("a theme chosen while the browser refuses to store it holds, in Appearance too, and the OS's appearance leaves it be", () => {
  const os = { matches: false, changed: () => {}, addEventListener: (_: string, listener: () => void) => (os.changed = listener) };
  vi.stubGlobal("matchMedia", () => os);
  installTheme();
  chooseTheme("system");
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new DOMException("Full", "QuotaExceededError");
  });
  serveAccount(ADA);
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
  // The fixture is the profile a server run with --no-auth answers.
  const everyone = { ...noAuth.response.body };
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

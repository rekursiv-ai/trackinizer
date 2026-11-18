import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { SETTINGS } from "../api/generated/settings";
import { type Preferences, preferences, receivePreferences } from "../preferences";
import { documentOf, PreferencesDocument, readDocument } from "./PreferencesDocument";
import { FAST, profile, renderScreen, serveAccount } from "./testing";

const ADA = profile("writer");
const DEFAULTS = Object.fromEntries(SETTINGS.map(({ key, default: fallback }) => [key, fallback])) as unknown as Preferences;

afterEach(() => {
  cleanup();
  receivePreferences(DEFAULTS);
});

const box = () => screen.getByRole<HTMLTextAreaElement>("textbox", { name: "Preferences document" });
const edit = (change: (text: string) => string) => fireEvent.change(box(), { target: { value: change(box().value) } });
const sent = (server: ReturnType<typeof serveAccount>) =>
  server.sent.filter((request) => request.path === "/api/me/preferences").map((request) => request.body);

test("the document lists every setting under its comment, and reads back as the values it shows", () => {
  const text = documentOf({ ...DEFAULTS, theme: "light" });
  for (const { key, description } of SETTINGS) {
    const lines = text.split("\n");
    const at = lines.findIndex((line) => line.trimStart().startsWith(`"${key}":`));
    expect(lines[at - 1]).toBe(`  // ${description}`);
  }
  expect(readDocument(text)).toEqual({ values: { ...DEFAULTS, theme: "light" } });
});

test("comments, a // inside a string and a trailing comma are read; a slip is named on its line", () => {
  expect(readDocument('{\n  // a note\n  "theme": "light", // why\n  "chat_side": "a // b",\n}')).toEqual({ values: { theme: "light", chat_side: "a // b" } });
  expect(readDocument('{\n  "theme": "light"\n  "chat_side": "left"\n}')).toEqual({ problems: [{ line: 3, message: expect.stringContaining("not JSON") }] });
  expect(readDocument('{\n  "theme": "light",\n  "colour": "red"\n}')).toEqual({ problems: [{ line: 3, message: 'There is no setting "colour".' }] });
  expect(readDocument("[1]")).toEqual({ problems: [{ line: 1, message: "The document is one { … } of settings." }] });
});

test("a save sends only what changed and shows the page in it; a line taken out goes back to its default", async () => {
  receivePreferences({ ...DEFAULTS, chat_side: "right" });
  const server = serveAccount(ADA);
  renderScreen(<PreferencesDocument />, ADA);
  edit((text) => text.replace('"theme": "dark"', '"theme": "light"').replace(/\n.*\n\s*"chat_side": "right",/, ""));
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(sent(server)).toEqual([{ theme: "light", chat_side: null }]), FAST);
  await waitFor(() => expect(preferences()).toMatchObject({ theme: "light", chat_side: "left" }), FAST);
  // Saved, the document is drawn afresh from what the server kept, comments and all.
  expect(box().value).toBe(documentOf(preferences()));
});

test("a value the server refuses is shown on its line and nothing changes", async () => {
  const server = serveAccount(ADA);
  server.answers.push(() => Response.json({ detail: "theme: Input should be 'dark', 'light' or 'system'" }, { status: 422 }));
  renderScreen(<PreferencesDocument />, ADA);
  edit((text) => text.replace('"theme": "dark"', '"theme": "blue"'));
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  const line = box().value.split("\n").findIndex((each) => each.includes('"theme"')) + 1;
  expect((await screen.findByRole("alert")).textContent).toBe(`Line ${line}: theme: Input should be 'dark', 'light' or 'system'`);
  expect(preferences().theme).toBe("dark");
  expect(box().value).toContain('"theme": "blue"');
});

test("a document that does not read sends nothing", () => {
  const server = serveAccount(ADA);
  renderScreen(<PreferencesDocument />, ADA);
  edit((text) => text.replace('"chat_side": "left",', '"chat_side": "left"'));
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(screen.getByRole("alert").textContent).toMatch(/^Line \d+: This is not JSON/);
  expect(sent(server)).toEqual([]);
});

test("Reset to defaults puts every setting back, and an untouched document follows changes made elsewhere", async () => {
  receivePreferences({ ...DEFAULTS, theme: "light", terminal_font_size: "16pt" });
  const server = serveAccount(ADA);
  renderScreen(<PreferencesDocument />, ADA);
  expect(box().value).toContain('"terminal_font_size": "16pt"');
  act(() => receivePreferences({ ...preferences(), chat_side: "right" }));
  expect(box().value).toContain('"chat_side": "right"');
  fireEvent.click(screen.getByRole("button", { name: "Reset to defaults" }));
  await waitFor(() => expect(sent(server)).toEqual([Object.fromEntries(SETTINGS.map(({ key }) => [key, null]))]), FAST);
});

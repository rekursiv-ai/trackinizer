import { afterEach, expect, test, vi } from "vitest";
import { stubFetch } from "../api/testing";

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

/** The column's module as a page that has just loaded sees it, reading what the browser kept. */
async function fresh() {
  vi.resetModules();
  return import("./chatColumn");
}

test("Chat starts on the left, shown, and not full screen", async () => {
  const column = await fresh();
  expect(column.currentChatColumn()).toEqual({ side: "left", hidden: false, fullscreen: false });
});

test("the side is the user's preference: a flip shows at once and is sent; hidden and full screen are this tab's alone", async () => {
  const sent = stubFetch(() => Response.json({ theme: "dark", chat_style: "default", chat_side: "right", highlight_mentions: true }));
  const first = await fresh();
  first.flipChatSide();
  first.setChatHidden(true);
  expect(first.currentChatColumn()).toEqual({ side: "right", hidden: true, fullscreen: false });
  await vi.waitFor(() => expect(sent.map(({ method, path, body }) => [method, path, body])).toEqual([["PATCH", "/api/me/preferences", { chat_side: "right" }]]));

  // A page loaded anew takes the side from the profile; nothing this tab held comes back.
  const reloaded = await fresh();
  const preferences = await import("../preferences");
  preferences.receivePreferences({ ...preferences.preferences(), chat_side: "right" });
  expect(reloaded.currentChatColumn()).toEqual({ side: "right", hidden: false, fullscreen: false });
  expect(localStorage.length).toBe(0);
});

test("a folded Chat is not full screen, and a full screen Chat is shown", async () => {
  const column = await fresh();
  column.setChatFullscreen(true);
  column.setChatHidden(true);
  expect(column.currentChatColumn()).toMatchObject({ hidden: true, fullscreen: false });
  column.setChatFullscreen(true);
  expect(column.currentChatColumn()).toMatchObject({ hidden: false, fullscreen: true });
  column.toggleChatFullscreen();
  expect(column.currentChatColumn().fullscreen).toBe(false);
  column.toggleChatHidden();
  expect(column.currentChatColumn().hidden).toBe(true);
});

test("a change to what the column already is leaves its state as it was, so no one redraws", async () => {
  const column = await fresh();
  const before = column.currentChatColumn();
  column.setChatHidden(false);
  column.setChatFullscreen(false);
  column.setChatSide("left");
  expect(column.currentChatColumn()).toBe(before);
  column.setChatHidden(true);
  expect(column.currentChatColumn()).not.toBe(before);
});

test("three columns hide Chat and two show it again, unless someone hid it, or showed it, since", async () => {
  const column = await fresh();
  column.followColumns(true);
  expect(column.currentChatColumn().hidden).toBe(true);
  column.followColumns(false);
  expect(column.currentChatColumn().hidden).toBe(false);
  // Shown by hand while three columns stand, it stays until the columns change again.
  column.followColumns(true);
  column.setChatHidden(false);
  column.followColumns(false);
  expect(column.currentChatColumn().hidden).toBe(false);
  // Hidden by hand, it stays hidden when a column goes.
  column.setChatHidden(true);
  column.followColumns(true);
  column.followColumns(false);
  expect(column.currentChatColumn().hidden).toBe(true);
});

test("a full screen Chat is not hidden by the workbench's columns", async () => {
  const column = await fresh();
  column.setChatFullscreen(true);
  column.followColumns(true);
  expect(column.currentChatColumn()).toMatchObject({ hidden: false, fullscreen: true });
});

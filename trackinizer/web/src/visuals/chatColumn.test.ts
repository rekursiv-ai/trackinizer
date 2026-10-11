import { afterEach, expect, test, vi } from "vitest";

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

test("the side is kept in the browser and read back; hidden and full screen are not", async () => {
  const first = await fresh();
  first.flipChatSide();
  first.setChatHidden(true);
  expect(first.currentChatColumn()).toEqual({ side: "right", hidden: true, fullscreen: false });
  expect(localStorage.getItem("trackinizer.chat.side")).toBe("right");

  const reloaded = await fresh();
  expect(reloaded.currentChatColumn()).toEqual({ side: "right", hidden: false, fullscreen: false });
  reloaded.flipChatSide();
  expect(localStorage.getItem("trackinizer.chat.side")).toBe("left");
});

test("storage that is off or holds something else leaves Chat on the left, and the side still changes for the page", async () => {
  localStorage.setItem("trackinizer.chat.side", "top");
  expect((await fresh()).currentChatColumn().side).toBe("left");
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("denied"); });
  const column = await fresh();
  expect(column.currentChatColumn().side).toBe("left");
  expect(() => column.flipChatSide()).not.toThrow();
  expect(column.currentChatColumn().side).toBe("right");
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

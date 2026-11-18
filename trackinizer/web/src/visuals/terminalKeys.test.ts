import { expect, test } from "vitest";
import { tabChord } from "./terminalKeys";

const press = (key: string, mods: { shift?: boolean; alt?: boolean; meta?: boolean; ctrl?: boolean } = {}) =>
  ({ key, ctrlKey: mods.ctrl ?? true, shiftKey: mods.shift ?? false, altKey: mods.alt ?? false, metaKey: mods.meta ?? false });

test.each([
  ["T", "new"], ["W", "close"], ["PageDown", "next"], ["PageUp", "previous"],
] as const)("Ctrl+Shift+%s is %s whatever the setting says", (key, action) => {
  expect(tabChord(press(key, { shift: true }), false)).toBe(action);
  expect(tabChord(press(key, { shift: true }), true)).toBe(action);
});

test.each(["t", "w", "Tab"])("plain Ctrl+%s goes to the shell while the setting is off", (key) => {
  expect(tabChord(press(key), false)).toBeNull();
});

test("Ctrl+Shift+Tab goes to the shell while the setting is off", () => {
  expect(tabChord(press("Tab", { shift: true }), false)).toBeNull();
});

test.each([["t", "new"], ["w", "close"], ["Tab", "next"]] as const)("with the setting on, Ctrl+%s is %s", (key, action) => {
  expect(tabChord(press(key), true)).toBe(action);
});

test("with the setting on, Ctrl+Shift+Tab goes back a tab", () => {
  expect(tabChord(press("Tab", { shift: true }), true)).toBe("previous");
});

test("another modifier, or no Ctrl, is never a tab chord", () => {
  expect(tabChord(press("t", { alt: true }), true)).toBeNull();
  expect(tabChord(press("w", { meta: true }), true)).toBeNull();
  expect(tabChord(press("t", { ctrl: false }), true)).toBeNull();
  expect(tabChord(press("PageDown", { ctrl: false, shift: true }), true)).toBeNull();
  expect(tabChord(press("a", { shift: true }), true)).toBeNull();
});

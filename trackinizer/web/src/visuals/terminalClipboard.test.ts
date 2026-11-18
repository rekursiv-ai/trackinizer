import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { COPY_WINDOW_MS, gatedClipboard, MAX_COPY_BYTES } from "./terminalClipboard";

/** A selection as xterm names it: its enum is `const`, so a test builds the value it holds, 'c' or 'p'. */
type Selection = Parameters<ReturnType<typeof gatedClipboard>["readText"]>[0];
const selection = (letter: "c" | "p"): Selection => letter as unknown as Selection;

const writeText = vi.fn();
const copied = vi.fn();
let lastInput = 0;
const provider = () => gatedClipboard({ lastInput: () => lastInput, copied });

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(100_000);
  lastInput = 100_000;
  writeText.mockReset().mockResolvedValue(undefined);
  copied.mockReset();
  vi.stubGlobal("navigator", { clipboard: { readText: vi.fn().mockResolvedValue("secret"), writeText } });
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

test("a program can never read the clipboard: every selection reads empty, and the browser's read is never called", async () => {
  for (const letter of ["c", "p"] as const) expect(await provider().readText(selection(letter))).toBe("");
  expect(navigator.clipboard.readText).not.toHaveBeenCalled();
});

test("a write within 5 s of the user's input goes through and is announced", async () => {
  vi.setSystemTime(100_000 + COPY_WINDOW_MS);
  await provider().writeText(selection("c"), "copied from tmux");
  expect(writeText).toHaveBeenCalledWith("copied from tmux");
  expect(copied).toHaveBeenCalledTimes(1);
});

test("a write later than 5 s after the last input, or with no input at all, is ignored", async () => {
  vi.setSystemTime(100_000 + COPY_WINDOW_MS + 1);
  await provider().writeText(selection("c"), "late");
  lastInput = 0;
  await provider().writeText(selection("c"), "never typed");
  expect(writeText).not.toHaveBeenCalled();
  expect(copied).not.toHaveBeenCalled();
});

test("a write over 100 KiB is ignored, counted in UTF-8 bytes", async () => {
  await provider().writeText(selection("c"), "a".repeat(MAX_COPY_BYTES));
  expect(writeText).toHaveBeenCalledTimes(1);
  await provider().writeText(selection("c"), "a".repeat(MAX_COPY_BYTES + 1));
  await provider().writeText(selection("c"), "é".repeat(MAX_COPY_BYTES / 2 + 1));
  expect(writeText).toHaveBeenCalledTimes(1);
});

test("a refusal by the browser is swallowed and announces nothing", async () => {
  writeText.mockRejectedValue(new Error("denied"));
  await provider().writeText(selection("c"), "x");
  expect(copied).not.toHaveBeenCalled();
});

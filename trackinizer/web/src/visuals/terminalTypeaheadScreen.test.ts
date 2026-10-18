import type { Terminal } from "@xterm/xterm";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { attachTypeahead, screenOf } from "./terminalTypeaheadScreen";

type FakeCell = { chars: string; bold?: boolean; fg?: number };

/** Just the parts of xterm the adapter reads: a one-row buffer and a log of writes. */
function fakeTerm() {
  const row: FakeCell[] = [{ chars: "$" }, { chars: " " }];
  const buffer = { type: "normal", cursorX: 2, cursorY: 0, baseY: 0, getLine: () => ({ getCell: (x: number) => cellOf(row[x]) }) };
  const writes: (string | Uint8Array)[] = [];
  const term = { rows: 5, cols: 20, buffer: { active: buffer }, write: (data: string | Uint8Array) => { writes.push(data); } };
  return { term: term as unknown as Terminal, buffer, row, writes };
}

function cellOf(cell: FakeCell | undefined) {
  if (!cell) return undefined;
  return {
    getChars: () => cell.chars, isBold: () => (cell.bold ? 1 : 0), isDim: () => 0, isItalic: () => 0, isUnderline: () => 0, isInverse: () => 0,
    isFgPalette: () => cell.fg !== undefined, getFgColor: () => cell.fg ?? 0, isFgRGB: () => false,
    isBgPalette: () => false, getBgColor: () => 0, isBgRGB: () => false,
  };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

test("the screen reports the buffer kind, cursor, size and each cell's text and colors", () => {
  const { term, buffer, row } = fakeTerm();
  row[1] = { chars: "x", bold: true, fg: 9 };
  const screen = screenOf(term);
  expect([screen.normal, screen.rows, screen.cols, screen.cursor()]).toEqual([true, 5, 20, { x: 2, y: 0, baseY: 0 }]);
  expect(screen.cell(0, 0)).toEqual({ chars: "$", sgr: "" });
  expect(screen.cell(1, 0)).toEqual({ chars: "x", sgr: "\x1b[0;1;91m" });
  buffer.type = "alternate";
  expect(screen.normal).toBe(false);
});

test("idle, the shell's bytes reach the screen as the very same array", () => {
  const { term, writes } = fakeTerm();
  const link = attachTypeahead(term, () => Date.now());
  const bytes = new TextEncoder().encode("hello");
  link.output(bytes);
  expect(writes).toEqual([bytes]);
  expect(writes[0]).toBe(bytes);
});

test("on a slow link a typed character is drawn dim at once, and the echo confirms it", () => {
  const { term, writes } = fakeTerm();
  const link = attachTypeahead(term, () => Date.now());
  for (let i = 0; i < 5; i++) link.echo(300);
  link.input("x");
  link.output(new TextEncoder().encode("x"));
  link.input("a");
  expect(writes.at(-1)).toContain("\x1b[2ma\x1b[22m");
  const before = writes.length;
  link.output(new TextEncoder().encode("a"));
  expect(writes.length).toBe(before + 1);
  expect(String(writes.at(-1))).toContain("a");
  expect(String(writes.at(-1))).not.toContain("\x1b[2m");
});

test("a prediction the shell never echoes is rolled back when the timer runs out", () => {
  const { term, writes } = fakeTerm();
  const link = attachTypeahead(term, () => Date.now());
  for (let i = 0; i < 5; i++) link.echo(300);
  link.input("x");
  link.output(new TextEncoder().encode("x"));
  link.input("a");
  const drawn = writes.length;
  vi.advanceTimersByTime(499);
  expect(writes.length).toBe(drawn);
  vi.advanceTimersByTime(1);
  expect(writes.length).toBe(drawn + 1);
  expect(writes.at(-1)).toContain("\x1b[X");
});

test("dispose cancels the timer", () => {
  const { term, writes } = fakeTerm();
  const link = attachTypeahead(term, () => Date.now());
  for (let i = 0; i < 5; i++) link.echo(300);
  link.input("x");
  link.output(new TextEncoder().encode("x"));
  link.input("a");
  link.dispose();
  const drawn = writes.length;
  vi.advanceTimersByTime(5_000);
  expect(writes.length).toBe(drawn);
});

test("a resize takes the predictions back", () => {
  const { term, writes } = fakeTerm();
  const link = attachTypeahead(term, () => Date.now());
  for (let i = 0; i < 5; i++) link.echo(300);
  link.input("x");
  link.output(new TextEncoder().encode("x"));
  link.input("a");
  const drawn = writes.length;
  link.resize();
  expect(writes.length).toBe(drawn + 1);
});

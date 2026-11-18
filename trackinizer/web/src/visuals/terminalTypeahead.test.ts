import { beforeEach, expect, test } from "vitest";
import { type Cell, EchoLatency, type Screen, Typeahead } from "./terminalTypeahead";

/** A screen that replays the escapes the predictor writes (and the shell's echo), so a test reads what a user would see. */
class Grid implements Screen {
  rows = 5;
  cols = 20;
  normal = true;
  x = 0;
  y = 0;
  private sgr = "";
  private cells: Cell[][] = Array.from({ length: 5 }, () => Array.from({ length: 20 }, () => ({ chars: "", sgr: "" })));
  cursor() { return { x: this.x, y: this.y, baseY: 0 }; }
  cell(x: number, line: number) { return this.cells[line]?.[x]; }
  text(line = 0): string { return this.cells[line]!.map((cell) => cell.chars || " ").join("").trimEnd(); }
  sgrAt(x: number, line = 0): string { return this.cells[line]![x]!.sgr; }
  feed(text: string): void {
    for (let i = 0; i < text.length; i++) {
      const rest = text.slice(i);
      const csi = /^\x1b\[([?0-9;]*)([A-Za-z])/.exec(rest);
      if (csi) {
        i += csi[0].length - 1;
        this.escape(csi[1]!, csi[2]!);
      } else if (text[i] === "\b") this.x = Math.max(0, this.x - 1);
      else if (text[i] === "\r") this.x = 0;
      else if (text[i] === "\n") this.y += 1;
      else {
        this.cells[this.y]![this.x] = { chars: text[i]!, sgr: this.sgr };
        this.x += 1;
      }
    }
  }
  private escape(args: string, final: string): void {
    if (final === "H") {
      const [row, col] = args.split(";").map(Number);
      this.y = (row || 1) - 1;
      this.x = (col || 1) - 1;
    } else if (final === "X") this.cells[this.y]![this.x] = { chars: "", sgr: "" };
    else if (final === "K") for (let at = this.x; at < this.cols; at++) this.cells[this.y]![at] = { chars: "", sgr: "" };
    else if (final === "m") this.sgr = args === "22" || args === "0" || args === "" ? "" : `\x1b[${args}m`;
  }
}

let grid: Grid;
let clock: number;
let ty: Typeahead;

/** The shell echoes through the predictor, then the screen takes what the predictor says to write. */
const echo = (text: string) => grid.feed(ty.serverData(text));

beforeEach(() => {
  grid = new Grid();
  clock = 1_000;
  ty = new Typeahead(grid, (text) => grid.feed(text), () => clock);
  grid.feed("$ ");
});

/** Enough slow echoes to turn predictions on. */
const slow = () => { for (let i = 0; i < 5; i++) ty.observeEcho(300); };
/** One typed character that the shell echoes: after it the row is validated, so the next character draws at once. */
const validated = () => { ty.userData("x"); echo("x"); };

test("a typed character is drawn dim at the cursor once the row is validated", () => {
  slow();
  validated();
  ty.userData("a");
  expect(grid.text()).toBe("$ xa");
  expect(grid.sgrAt(3)).toBe("\x1b[2m");
  expect(grid.sgrAt(2)).toBe("");
});

test("below the latency threshold nothing is drawn", () => {
  for (let i = 0; i < 5; i++) ty.observeEcho(5);
  validated();
  ty.userData("a");
  expect(grid.text()).toBe("$ x");
});

test("the echo confirms a prediction: plain text, queue empty", () => {
  slow();
  validated();
  ty.userData("a");
  echo("a");
  expect(grid.text()).toBe("$ xa");
  expect(grid.sgrAt(3)).toBe("");
  expect(ty.pending).toBe(0);
});

test("an echo that contradicts is rolled back and shown as it came", () => {
  slow();
  validated();
  ty.userData("a");
  echo("b");
  expect(grid.text()).toBe("$ xb");
  expect(grid.sgrAt(3)).toBe("");
  expect(ty.pending).toBe(0);
});

test("the first character on a row waits for the shell's echo", () => {
  slow();
  ty.userData("a");
  expect(grid.text()).toBe("$");
  ty.userData("b");
  expect(grid.text()).toBe("$");
  echo("a");
  expect(grid.text()).toBe("$ ab");
  expect(grid.sgrAt(2)).toBe("");
  expect(grid.sgrAt(3)).toBe("\x1b[2m");
  echo("b");
  expect(grid.text()).toBe("$ ab");
  expect(grid.sgrAt(3)).toBe("");
});

test("a prompt that echoes nothing never shows what is typed, and backs predictions off", () => {
  slow();
  for (let round = 0; round < 3; round++) {
    for (const char of "pass") ty.userData(char);
    expect(grid.text()).toBe("$");
    clock = ty.expireAt()!;
    ty.expire();
    expect(ty.pending).toBe(0);
  }
  expect(ty.showing).toBe(false);
});

test("backspace at the end of the line is predicted and matches both shells' echo", () => {
  slow();
  validated();
  ty.userData("a");
  echo("a");
  ty.userData("\x7f");
  expect(grid.text()).toBe("$ x");
  echo("\b \b");
  expect(grid.text()).toBe("$ x");
  ty.userData("\x7f");
  echo("\b\x1b[K");
  expect(grid.text()).toBe("$");
  expect(ty.pending).toBe(0);
});

test("backspace does not predict into the prompt", () => {
  slow();
  validated();
  ty.userData("\x7f");
  echo("\b \b");
  ty.userData("\x7f");
  expect(grid.text()).toBe("$");
  expect(grid.x).toBe(2);
});

test("arrows move the predicted cursor; left stops at where typing began, and what follows that boundary waits", () => {
  slow();
  validated();
  ty.userData("a");
  echo("a");
  ty.userData("\x1b[D");
  expect(grid.x).toBe(3);
  ty.userData("\x1bOD");
  expect(grid.x).toBe(2);
  ty.userData("\x1b[D");
  expect(grid.x).toBe(2);
  ty.userData("\x1b[C");
  expect(grid.x).toBe(2);
});

test("a shell that redraws the pair (zsh) still matches", () => {
  slow();
  validated();
  ty.userData("a");
  ty.userData("b");
  echo("a");
  echo("\bab");
  expect(ty.pending).toBe(0);
  expect(grid.text()).toBe("$ xab");
});

test("an echo that stops short is held until the rest arrives", () => {
  slow();
  validated();
  ty.userData("\x7f");
  echo("\b");
  expect(ty.pending).toBe(1);
  echo(" \b");
  expect(ty.pending).toBe(0);
});

test("the alternate screen is never predicted into, and its queue is dropped", () => {
  slow();
  validated();
  ty.userData("a");
  grid.normal = false;
  ty.userData("b");
  expect(grid.text()).toBe("$ xa");
  expect(ty.serverData("\x1b[?1049h")).toBe("\x1b[?1049h");
  expect(ty.pending).toBe(0);
});

test("Enter, Tab and escapes are boundaries: nothing after them is drawn until confirmed", () => {
  slow();
  validated();
  ty.userData("\t");
  ty.userData("a");
  expect(grid.text()).toBe("$ x");
  expect(ty.pending).toBe(2);
});

test("a pager's colon prompt on the last row is left alone", () => {
  slow();
  grid.feed("\x1b[5;1H:");
  grid.x = 1;
  grid.y = 4;
  ty.userData("q");
  expect(ty.pending).toBe(0);
});

test("unconfirmed predictions are cleared after the timeout and the drawn cells restored", () => {
  slow();
  validated();
  ty.userData("a");
  expect(grid.text()).toBe("$ xa");
  expect(ty.expireAt()).toBe(1_000 + 500);
  clock = 1_500;
  ty.expire();
  expect(grid.text()).toBe("$ x");
  expect(ty.pending).toBe(0);
});

test("the timeout is one and a half times the slowest echo when that is over 500 ms", () => {
  ty.observeEcho(900);
  for (let i = 0; i < 4; i++) ty.observeEcho(300);
  validated();
  ty.userData("a");
  expect(ty.expireAt()).toBe(1_000 + 1_350);
});

test("cursor hide, show and status reports in the echo do not break the match", () => {
  slow();
  validated();
  ty.userData("a");
  echo("\x1b[?25la\x1b[?25h");
  expect(ty.pending).toBe(0);
});

test("echo latency turns on at 30 ms, off under 15 ms, and needs five samples", () => {
  const latency = new EchoLatency();
  for (let i = 0; i < 4; i++) latency.add(100);
  expect(latency.count).toBe(4);
  expect(latency.average).toBeCloseTo(100);
  latency.add(40);
  expect(latency.slowest).toBe(100);
  slow();
  expect(ty.showing).toBe(true);
  for (let i = 0; i < 40; i++) ty.observeEcho(20);
  expect(ty.showing).toBe(true);
  for (let i = 0; i < 40; i++) ty.observeEcho(5);
  expect(ty.showing).toBe(false);
  for (let i = 0; i < 40; i++) ty.observeEcho(200);
  expect(ty.showing).toBe(true);
});

test("fewer than five samples never turn it on", () => {
  for (let i = 0; i < 4; i++) ty.observeEcho(300);
  expect(ty.showing).toBe(false);
});

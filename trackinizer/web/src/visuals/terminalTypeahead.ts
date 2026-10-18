/*
 * Local echo for a slow terminal link, ported from VS Code's terminal typeahead addon
 * (src/vs/workbench/contrib/terminalContrib/typeAhead/browser/terminalTypeAheadAddon.ts,
 * microsoft/vscode at commit ae11932c6109d3977d78f2fe8937d861e246151d).
 *
 * MIT License
 *
 * Copyright (c) 2015 - present Microsoft Corporation
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */
// What the user types is drawn at once, dim, and confirmed or rolled back when the shell's echo arrives. This
// module is pure: it reads a `Screen` and hands strings to `write`; `terminalTypeaheadScreen.ts` binds xterm.
// Left out of the port: newline, line-wrap and word-move predictions, telemetry, and style tracking.

/** One cell of the screen: its text (empty when blank) and the SGR sequence that draws it (empty for the defaults). */
export type Cell = { readonly chars: string; readonly sgr: string };

/** What the predictor needs of a terminal screen. `line` is absolute: the viewport row plus the scrollback offset. */
export interface Screen {
  /** False on the alternate screen (vim, less, tmux), where nothing is predicted. */
  readonly normal: boolean;
  readonly rows: number;
  readonly cols: number;
  cursor(): { readonly x: number; readonly y: number; readonly baseY: number };
  cell(x: number, line: number): Cell | undefined;
}

const ON_MS = 30;
const OFF_MS = 15;
const MIN_SAMPLES = 5;
const MIN_ACCURACY = 0.3;
const WINDOW = 24;
const SMOOTHING = 0.25;
const FLOOR_TIMEOUT_MS = 500;

/** Moving average of the time from an INPUT frame to the next OUTPUT, and the slowest of the last few. */
export class EchoLatency {
  private mean = 0;
  private seen = 0;
  private recent: number[] = [];

  add(ms: number): void {
    this.mean = this.seen === 0 ? ms : this.mean + SMOOTHING * (ms - this.mean);
    this.seen += 1;
    this.recent = [...this.recent, ms].slice(-WINDOW);
  }

  get average(): number {
    return this.mean;
  }

  get count(): number {
    return this.seen;
  }

  get slowest(): number {
    return Math.max(0, ...this.recent);
  }
}

const ESC = "\x1b";
const CSI = `${ESC}[`;
const HIDE_CURSOR = `${CSI}?25l`;
const SHOW_CURSOR = `${CSI}?25h`;
const DELETE_CHAR = `${CSI}X`;
const DIM = `${CSI}2m`;
const UNDIM = `${CSI}22m`;
const RESET = `${CSI}0m`;
const CSI_STYLE_RE = /^\x1b\[[0-9;]*m/;
const CSI_MOVE_RE = /^\x1b(?:\[|O)([0-9]*)([DC])/;
// Cursor hide/show and status reports are the terminal's own, never the shell's echo.
const OMIT_RE = /^(\x1b\[(\??25[hl]|\??[0-9;]+n))+/;

type Coordinate = { readonly x: number; readonly y: number; readonly baseY: number };

class Cursor implements Coordinate {
  x: number;
  y: number;
  baseY: number;
  private readonly screen: Screen;

  constructor(screen: Screen) {
    this.screen = screen;
    ({ x: this.x, y: this.y, baseY: this.baseY } = screen.cursor());
  }

  get coordinate(): Coordinate {
    return { x: this.x, y: this.y, baseY: this.baseY };
  }

  getCell(): Cell | undefined {
    return this.screen.cell(this.x, this.y + this.baseY);
  }

  /** Whether nothing but blanks lies from the cursor to the end of its line. */
  atLineEnd(): boolean {
    for (let at = this.x; at < this.screen.cols; at++) if (this.screen.cell(at, this.y + this.baseY)?.chars.trim()) return false;
    return true;
  }

  moveTo(to: Coordinate): string {
    this.x = to.x;
    this.y = to.y + to.baseY - this.baseY;
    return this.moveInstruction();
  }

  clone(): Cursor {
    const copy = new Cursor(this.screen);
    copy.moveTo(this);
    return copy;
  }

  shift(x = 0): string {
    this.x += x;
    return this.moveInstruction();
  }

  move(x: number, y: number): string {
    this.x = x;
    this.y = y;
    return this.moveInstruction();
  }

  moveInstruction(): string {
    if (this.y >= this.screen.rows) {
      this.baseY += this.y - (this.screen.rows - 1);
      this.y = this.screen.rows - 1;
    } else if (this.y < 0) {
      this.baseY -= this.y;
      this.y = 0;
    }
    return `${CSI}${this.y + 1};${this.x + 1}H`;
  }
}

/** How a prediction fared against the shell's echo; `Buffer`: it may match once more arrives, so hold the input. */
const Match = { Success: 0, Failure: 1, Buffer: 2 } as const;
type Match = (typeof Match)[keyof typeof Match];

class Reader {
  index = 0;
  private readonly input: string;

  constructor(input: string) {
    this.input = input;
  }

  get remaining(): number {
    return this.input.length - this.index;
  }

  get eof(): boolean {
    return this.index === this.input.length;
  }

  get rest(): string {
    return this.input.slice(this.index);
  }

  eatChar(char: string): boolean {
    if (this.input[this.index] !== char) return false;
    this.index++;
    return true;
  }

  eatStr(text: string): boolean {
    if (!this.input.startsWith(text, this.index)) return false;
    this.index += text.length;
    return true;
  }

  /** Eat `text` character by character: at the end of the input part way through, ask to buffer. */
  eatGradually(text: string): Match {
    const from = this.index;
    for (let at = 0; at < text.length; at++) {
      if (at > 0 && this.eof) return Match.Buffer;
      if (!this.eatChar(text[at]!)) {
        this.index = from;
        return Match.Failure;
      }
    }
    return Match.Success;
  }

  eatRe(re: RegExp): RegExpExecArray | null {
    const found = re.exec(this.input.slice(this.index));
    if (found) this.index += found[0].length;
    return found;
  }

  eatCharCode(min: number, max = min + 1): boolean {
    const code = this.input.charCodeAt(this.index);
    if (code < min || code >= max) return false;
    this.index++;
    return true;
  }
}

interface Prediction {
  /** False for a boundary, which stays queued however long the shell is silent. */
  readonly clearAfterTimeout?: boolean;
  /** The sequence that draws the prediction; advances `cursor`. */
  apply(cursor: Cursor): string;
  rollback(cursor: Cursor): string;
  /** What to write when the echo matched, in place of the echo itself. */
  rollForwards(cursor: Cursor, echo: string): string;
  matches(input: Reader, lookBehind?: Prediction): Match;
}

/** Never matches: everything predicted after it is thrown away. */
class HardBoundary implements Prediction {
  readonly clearAfterTimeout = false;

  apply(): string {
    return "";
  }

  rollback(): string {
    return "";
  }

  rollForwards(): string {
    return "";
  }

  matches(): Match {
    return Match.Failure;
  }
}

/** Wraps a prediction: nothing is drawn, but the shell's echo is matched against the inner one. */
class TentativeBoundary implements Prediction {
  readonly inner: Prediction;
  private applied?: Cursor;

  constructor(inner: Prediction) {
    this.inner = inner;
  }

  apply(cursor: Cursor): string {
    this.applied = cursor.clone();
    this.inner.apply(this.applied);
    return "";
  }

  rollback(cursor: Cursor): string {
    this.inner.rollback(cursor.clone());
    return "";
  }

  rollForwards(cursor: Cursor, echo: string): string {
    if (this.applied) cursor.moveTo(this.applied);
    return echo;
  }

  matches(input: Reader): Match {
    return this.inner.matches(input);
  }
}

class CharacterPrediction implements Prediction {
  appliedAt?: { readonly pos: Coordinate; readonly old: Cell | undefined };
  private readonly char: string;

  constructor(char: string) {
    this.char = char;
  }

  apply(cursor: Cursor): string {
    this.appliedAt = { pos: cursor.coordinate, old: cursor.getCell() };
    cursor.shift(1);
    return DIM + this.char + UNDIM;
  }

  rollback(cursor: Cursor): string {
    if (!this.appliedAt) return "";
    const { old, pos } = this.appliedAt;
    return cursor.moveTo(pos) + (old?.chars ? `${old.sgr || RESET}${old.chars}${cursor.moveTo(pos)}` : DELETE_CHAR);
  }

  rollForwards(cursor: Cursor, echo: string): string {
    return this.appliedAt ? cursor.clone().moveTo(this.appliedAt.pos) + echo : "";
  }

  matches(input: Reader, lookBehind?: Prediction): Match {
    const from = input.index;
    while (input.eatRe(CSI_STYLE_RE));
    if (input.eof) return Match.Buffer;
    if (input.eatChar(this.char)) return Match.Success;
    // zsh redraws the previous character with this one.
    if (lookBehind instanceof CharacterPrediction) {
      const redraw = input.eatGradually(`\b${lookBehind.char}${this.char}`);
      if (redraw !== Match.Failure) return redraw;
    }
    input.index = from;
    return Match.Failure;
  }
}

class BackspacePrediction implements Prediction {
  private applied?: { readonly pos: Coordinate; readonly old: Cell | undefined; readonly last: boolean };

  apply(cursor: Cursor): string {
    const last = cursor.atLineEnd();
    const pos = cursor.coordinate;
    const move = cursor.shift(-1);
    this.applied = { last, pos, old: cursor.getCell() };
    return move + DELETE_CHAR;
  }

  rollback(cursor: Cursor): string {
    if (!this.applied) return "";
    const { old, pos } = this.applied;
    if (!old?.chars) return cursor.moveTo(pos) + DELETE_CHAR;
    return `${old.sgr || RESET}${old.chars}${cursor.moveTo(pos)}`;
  }

  rollForwards(): string {
    return "";
  }

  matches(input: Reader): Match {
    if (!this.applied?.last) return Match.Failure;
    const bash = input.eatGradually(`\b${CSI}K`);
    if (bash !== Match.Failure) return bash;
    return input.eatGradually("\b \b");
  }
}

class CursorMovePrediction implements Prediction {
  private applied?: { readonly amount: number; readonly from: number; readonly moved: string };
  private readonly direction: "D" | "C";

  constructor(direction: "D" | "C") {
    this.direction = direction;
  }

  apply(cursor: Cursor): string {
    const from = cursor.x;
    const moved = cursor.shift(this.direction === "D" ? -1 : 1);
    this.applied = { amount: 1, from, moved };
    return moved;
  }

  rollback(cursor: Cursor): string {
    return this.applied ? cursor.move(this.applied.from, cursor.y) : "";
  }

  rollForwards(): string {
    return "";
  }

  matches(input: Reader): Match {
    if (!this.applied) return Match.Failure;
    const { direction } = this;
    const { amount, moved } = this.applied;
    if (input.eatStr(`${CSI}${direction}`.repeat(amount))) return Match.Success;
    if (direction === "D" && input.eatStr("\b".repeat(amount))) return Match.Success;
    const absolute = input.eatGradually(moved);
    if (absolute !== Match.Failure) return absolute;
    return input.eatGradually(`${CSI}${amount}${direction}`);
  }
}

type Listener = { succeeded(prediction: Prediction): void; failed(prediction: Prediction): void };

/** The queue of predictions awaiting the shell's echo (`PredictionTimeline` in VS Code). */
class Timeline {
  private expected: { readonly gen: number; readonly p: Prediction }[] = [];
  private gen = 0;
  private physical?: Cursor;
  private tentative?: Cursor;
  private held = "";
  private lookBehind?: Prediction;
  showing = false;
  private readonly screen: Screen;
  private readonly write: (text: string) => void;
  private readonly listener: Listener;

  constructor(screen: Screen, write: (text: string) => void, listener: Listener) {
    this.screen = screen;
    this.write = write;
    this.listener = listener;
  }

  get length(): number {
    return this.expected.length;
  }

  private get firstGeneration(): Prediction[] {
    return this.expected.filter(({ gen }) => gen === this.expected[0]?.gen).map(({ p }) => p);
  }

  peekStart(): Prediction | undefined {
    return this.expected[0]?.p;
  }

  peekEnd(): Prediction | undefined {
    return this.expected.at(-1)?.p;
  }

  physicalCursor(): Cursor {
    this.physical ??= new Cursor(this.screen);
    return this.physical;
  }

  tentativeCursor(): Cursor {
    this.tentative ??= this.physicalCursor().clone();
    return this.tentative;
  }

  clearCursor(): void {
    this.physical = undefined;
    this.tentative = undefined;
  }

  /** Take back what is drawn and forget the queue; `count` is told each prediction dropped. */
  undoAll(count: (prediction: Prediction) => void = () => undefined): void {
    if (this.showing && this.screen.normal) {
      this.write(this.firstGeneration.reverse().map((p) => p.rollback(this.physicalCursor())).join(""));
    }
    this.expected.forEach(({ p }) => count(p));
    this.expected = [];
    this.clearCursor();
  }

  addPrediction(prediction: Prediction): void {
    this.expected.push({ gen: this.gen, p: prediction });
    if (this.gen !== this.expected[0]!.gen) {
      prediction.apply(this.tentativeCursor());
      return;
    }
    const text = prediction.apply(this.physicalCursor());
    this.tentative = undefined;
    if (this.showing && text) this.write(text);
  }

  /** Predictions added after a boundary are drawn only once everything before it has matched. */
  addBoundary(prediction?: Prediction): void {
    if (prediction) {
      this.addPrediction(new TentativeBoundary(prediction));
      prediction.apply(this.tentativeCursor());
    }
    this.gen++;
  }

  /** Match the shell's `input` against the queue; returns what the screen should be given instead. */
  beforeServerInput(input: string): string {
    const original = input;
    input = this.held + input;
    this.held = "";
    if (this.expected.length === 0 || !this.screen.normal) {
      this.reset();
      return input;
    }
    let output = "";
    const reader = new Reader(input);
    const startingGen = this.expected[0]!.gen;
    const omit = () => {
      const found = reader.eatRe(OMIT_RE);
      if (found) output += found[0];
    };
    read: while (this.expected.length > 0 && reader.remaining > 0) {
      omit();
      const { p, gen } = this.expected[0]!;
      const from = reader.index;
      switch (p.matches(reader, this.lookBehind)) {
        case Match.Success: {
          const eaten = input.slice(from, reader.index);
          if (gen === startingGen) output += p.rollForwards(this.physicalCursor(), eaten);
          else {
            p.apply(this.physicalCursor());
            output += eaten;
          }
          this.listener.succeeded(p);
          this.lookBehind = p;
          this.expected.shift();
          break;
        }
        case Match.Buffer:
          this.held = input.slice(from);
          reader.index = input.length;
          break read;
        case Match.Failure: {
          const undo = this.expected.filter((each) => each.gen === startingGen).reverse();
          output += undo.map(({ p: each }) => each.rollback(this.physicalCursor())).join("");
          this.reset();
          this.listener.failed(p);
          break read;
        }
      }
    }
    omit();
    // Output the predictions did not account for puts the cursor somewhere unknown.
    if (!reader.eof) {
      output += reader.rest;
      this.reset();
    }
    if (this.expected.length > 0 && startingGen !== this.expected[0]!.gen) {
      for (const { p, gen } of this.expected) {
        if (gen !== this.expected[0]!.gen) break;
        output += p.apply(this.physicalCursor());
      }
    }
    if (!this.showing) return original;
    if (output.length === 0 || output === input) return output;
    if (this.physical) output += this.physical.moveInstruction();
    return HIDE_CURSOR + output + SHOW_CURSOR;
  }

  private reset(): void {
    this.expected = [];
    this.clearCursor();
    this.lookBehind = undefined;
  }
}

/** Where on its row the user has typed, to keep predictions from reaching into the prompt. */
type Row = { readonly y: number; startingX: number; endingX: number; state: "unknown" | "pending" | "validated" };

/** Predicts the echo of what is typed, while the measured echo is slow. */
export class Typeahead {
  private readonly timeline: Timeline;
  private readonly latency = new EchoLatency();
  private outcomes: boolean[] = [];
  private row?: Row;
  private deadline: number | null = null;
  private readonly screen: Screen;
  private readonly now: () => number;

  constructor(screen: Screen, write: (text: string) => void, now: () => number) {
    this.screen = screen;
    this.now = now;
    this.timeline = new Timeline(screen, write, {
      succeeded: (prediction) => {
        this.record(true);
        if (this.row?.state === "pending" && prediction instanceof TentativeBoundary && prediction.inner instanceof CharacterPrediction) {
          const at = prediction.inner.appliedAt?.pos;
          if (at && at.y + at.baseY === this.row.y) this.row.state = "validated";
        }
      },
      failed: () => {
        this.record(false);
        this.forgetPending();
      },
    });
  }

  get showing(): boolean {
    return this.timeline.showing;
  }

  get pending(): number {
    return this.timeline.length;
  }

  /** The time from an INPUT frame to the next OUTPUT, as the link measured it. */
  observeEcho(ms: number): void {
    this.latency.add(ms);
    this.evaluate();
  }

  /** When the pending predictions should be given up on, or null when none are waiting on the shell. */
  expireAt(): number | null {
    return this.deadline;
  }

  /** Give up on the pending predictions: the shell never echoed them. */
  expire(): void {
    this.timeline.undoAll(() => this.record(false));
    this.forgetPending();
    this.deadline = null;
    this.evaluate();
  }

  resize(): void {
    this.timeline.undoAll();
    this.deadline = null;
    this.evaluate();
  }

  /** The user's keystrokes, as xterm hands them to the shell. */
  userData(data: string): void {
    const { screen, timeline } = this;
    if (!screen.normal) return;
    const cursor = screen.cursor();
    // A pager on the normal screen (git log, less) shows `:` and takes no typed text.
    if (cursor.x === 1 && cursor.y === screen.rows - 1 && screen.cell(0, cursor.y + cursor.baseY)?.chars === ":") return;
    const line = cursor.baseY + cursor.y;
    if (line !== this.row?.y) this.row = { y: line, startingX: cursor.x, endingX: cursor.x, state: "unknown" };
    else {
      this.row.startingX = Math.min(this.row.startingX, cursor.x);
      this.row.endingX = Math.max(this.row.endingX, timeline.physicalCursor().x);
    }
    const row = this.row;
    const navigate = (direction: "D" | "C") => {
      const prediction = new CursorMovePrediction(direction);
      const blocked = direction === "D" ? timeline.tentativeCursor().x <= row.startingX : timeline.tentativeCursor().x >= row.endingX - 1;
      if (blocked) timeline.addBoundary(prediction);
      else timeline.addPrediction(prediction);
    };
    const reader = new Reader(data);
    while (reader.remaining > 0) {
      if (reader.eatCharCode(127)) {
        if (timeline.peekEnd() instanceof CharacterPrediction) timeline.addBoundary();
        if (timeline.tentativeCursor().x <= row.startingX) timeline.addBoundary(new BackspacePrediction());
        else {
          row.endingX--;
          timeline.addPrediction(new BackspacePrediction());
        }
        continue;
      }
      if (reader.eatCharCode(32, 126)) {
        const prediction = new CharacterPrediction(data[reader.index - 1]!);
        if (row.state === "unknown") {
          timeline.addBoundary(prediction);
          row.state = "pending";
        } else timeline.addPrediction(prediction);
        if (timeline.tentativeCursor().x >= screen.cols) timeline.addBoundary(new HardBoundary());
        continue;
      }
      const move = reader.eatRe(CSI_MOVE_RE);
      if (move) {
        navigate(move[2] as "D" | "C");
        continue;
      }
      // Enter, Tab, Ctrl keys, any other escape: nothing after it is drawn until the shell has answered.
      timeline.addBoundary(new HardBoundary());
      break;
    }
    if (this.deadline === null) this.defer();
  }

  /** The shell's output; returns what to write to the screen in its place. */
  serverData(text: string): string {
    const out = this.timeline.beforeServerInput(text);
    this.defer();
    if (this.timeline.length === 0) this.evaluate();
    return out;
  }

  private defer(): void {
    const first = this.timeline.peekStart();
    this.deadline = first && first.clearAfterTimeout !== false ? this.now() + Math.max(FLOOR_TIMEOUT_MS, (this.latency.slowest * 3) / 2) : null;
  }

  private record(correct: boolean): void {
    this.outcomes = [...this.outcomes, correct].slice(-WINDOW);
  }

  private forgetPending(): void {
    if (this.row?.state === "pending") this.row.state = "unknown";
  }

  /** Switch only while nothing is queued, so a prediction is never half drawn. */
  private evaluate(): void {
    if (this.timeline.length > 0 || this.latency.count < MIN_SAMPLES) return;
    const accurate = this.outcomes.filter(Boolean).length / (this.outcomes.length || 1);
    const { average } = this.latency;
    let show = this.timeline.showing;
    if (this.outcomes.length >= MIN_SAMPLES && accurate <= MIN_ACCURACY) show = false;
    else if (average >= ON_MS) show = true;
    else if (average < OFF_MS) show = false;
    if (show !== this.timeline.showing) {
      this.timeline.showing = show;
      this.timeline.clearCursor();
    }
  }
}

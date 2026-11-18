// Binds the predictor (`terminalTypeahead.ts`) to an xterm screen: the keystrokes, the shell's output, the size,
// the measured echo, and the timer that gives up on a prediction the shell never echoed.
import type { IBufferCell, Terminal } from "@xterm/xterm";
import { type Cell, type Screen, Typeahead } from "./terminalTypeahead";

/** What a screen shows of the shell's input and output, with the predictor between. */
export type TypeaheadLink = {
  /** The user's keystrokes, before they are sent. */
  readonly input: (data: string) => void;
  /** The shell's output; written to the screen, with any prediction confirmed or rolled back. */
  readonly output: (bytes: Uint8Array) => void;
  /** The echo's round trip in ms, from the link. */
  readonly echo: (ms: number) => void;
  readonly resize: () => void;
  readonly dispose: () => void;
};

/** The SGR sequence that draws `cell`; empty for the defaults. */
function sgrOf(cell: IBufferCell): string {
  const args: (number | string)[] = [0];
  if (cell.isBold()) args.push(1);
  if (cell.isDim()) args.push(2);
  if (cell.isItalic()) args.push(3);
  if (cell.isUnderline()) args.push(4);
  if (cell.isInverse()) args.push(7);
  if (cell.isFgPalette()) args.push(...palette(cell.getFgColor(), 30, 90, 38));
  else if (cell.isFgRGB()) args.push(...rgb(cell.getFgColor(), 38));
  if (cell.isBgPalette()) args.push(...palette(cell.getBgColor(), 40, 100, 48));
  else if (cell.isBgRGB()) args.push(...rgb(cell.getBgColor(), 48));
  return args.length === 1 ? "" : `\x1b[${args.join(";")}m`;
}

function palette(color: number, low: number, high: number, extended: number): (number | string)[] {
  if (color < 8) return [low + color];
  return color < 16 ? [high + color - 8] : [extended, 5, color];
}

function rgb(color: number, extended: number): number[] {
  return [extended, 2, (color >> 16) & 255, (color >> 8) & 255, color & 255];
}

/** The predictor's view of `term`: the active buffer, read when asked. */
export function screenOf(term: Terminal): Screen {
  return {
    get normal() {
      return term.buffer.active.type === "normal";
    },
    get rows() {
      return term.rows;
    },
    get cols() {
      return term.cols;
    },
    cursor: () => {
      const { cursorX: x, cursorY: y, baseY } = term.buffer.active;
      return { x, y, baseY };
    },
    cell: (x, line): Cell | undefined => {
      const cell = term.buffer.active.getLine(line)?.getCell(x);
      return cell ? { chars: cell.getChars(), sgr: sgrOf(cell) } : undefined;
    },
  };
}

/** Predict the echo on `term`; `now` is milliseconds on any steady clock. */
export function attachTypeahead(term: Terminal, now: () => number = () => performance.now()): TypeaheadLink {
  const typeahead = new Typeahead(screenOf(term), (text) => term.write(text), now);
  const decoder = new TextDecoder();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const schedule = () => {
    clearTimeout(timer);
    const at = typeahead.expireAt();
    if (at === null) return;
    timer = setTimeout(() => {
      typeahead.expire();
      schedule();
    }, Math.max(0, at - now()));
  };
  return {
    input: (data) => {
      typeahead.userData(data);
      schedule();
    },
    output: (bytes) => {
      // Idle, the shell's bytes go to the screen untouched: no decoding, and no change for a fast link.
      if (typeahead.pending === 0 && !typeahead.showing) {
        typeahead.serverData("");
        term.write(bytes);
      } else term.write(typeahead.serverData(decoder.decode(bytes, { stream: true })));
      schedule();
    },
    echo: (ms) => typeahead.observeEcho(ms),
    resize: () => typeahead.resize(),
    dispose: () => clearTimeout(timer),
  };
}

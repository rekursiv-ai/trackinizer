import { startTransition, useEffect, useState } from "react";

/**
 * Records a part draws first (`useDrawnFrom`). That render is mostly the
 * Markdown chunk's first use: drawn as the data landed, with the CPU slowed 4x
 * and 1,000 production-shaped records, ten made it one task of 53 to 62 ms,
 * five made one of 50 to 55 ms in 6 of 10 loads, and three made none in 10.
 */
const FIRST_DRAWN = 3;

/** Records each background render adds above the ones drawn (`useDrawnFrom`). */
const CHUNK_DRAWN = 100;

/**
 * The smallest idx to draw of loaded records `first` to `last`: none as they
 * land, then the newest `FIRST_DRAWN`, then `CHUNK_DRAWN` more, each in a
 * background render React can interrupt
 * (https://react.dev/reference/react/startTransition), until all are drawn,
 * those Load earlier adds included. A record appended once the first are drawn
 * draws at once, and every record drawn stays drawn. A part of three records or
 * fewer, drawn whole at once, still marks them all drawn: else an append would
 * leave them out for a render and draw them afresh, losing what the reader
 * opened.
 *
 * Chunks, because React throws away an interrupted render and starts it again.
 * One background render of the rest (`useDeferredValue`) was interrupted by
 * every live append: with one every 500 ms and the CPU slowed 4x, 1,000 records
 * took 5.7 to 10.8 s to draw, against 2.0 to 2.6 s in chunks. Drawn ten at a
 * time, a task each, they took 4.5 s with no appends, against 1.8 s.
 */
export function useDrawnFrom(first: number, last: number): number {
  const [from, setFrom] = useState(Infinity);
  const start = Math.min(from, last - FIRST_DRAWN + 1);
  useEffect(() => {
    if (from > first) startTransition(() => setFrom(Math.max(first, from === Infinity ? start : start - CHUNK_DRAWN)));
  }, [from, start, first]);
  return from === Infinity ? Infinity : start;
}

/**
 * The height, in px, a message's line takes until it is first drawn
 * (`contain-intrinsic-size`): its header, and a row for each 100 characters of
 * each line of its text, a folded message's no taller than its fold.
 *
 * Lines off screen skip layout (transcript.css), and the browser draws each line
 * whose placeholder lands near the screen. Each chunk lands above the lines on
 * screen (`useDrawnFrom`), and at the CSS default of 60 px a reply 1,200 px tall
 * counts as near it: with the CPU slowed 4x on an M-series Mac, each chunk's
 * frame took 53 ms at the median, against 36 with these heights. Too tall a
 * guess costs a frame that draws a few more lines; too short, lines drawn for
 * nothing.
 */
export function placeholderHeight(text: string, folded: boolean): number {
  const rows = text.split("\n").reduce((sum, line) => sum + Math.max(1, Math.ceil(line.length / ROW_CHARACTERS)), 0);
  return HEADER_PX + Math.min(rows * ROW_PX, folded ? FOLDED_PX : Infinity);
}

/** A message's header line, with the gap under it. */
const HEADER_PX = 22;
/** One row of a message's text (13.5 px at a line height of 1.6). */
const ROW_PX = 22;
/** Characters in a row of the 760 px column. */
const ROW_CHARACTERS = 100;
/** A folded message: its 16em clamp and the Show all under it. */
const FOLDED_PX = 240;

import { startTransition, useEffect, useState } from "react";

/**
 * Records a part draws as its data lands (`useDrawnFrom`). That render is mostly
 * the Markdown chunk's first use: with the CPU slowed 4x and 1,000
 * production-shaped records, ten made it one task of 53 to 62 ms, five made one
 * of 50 to 55 ms in 6 of 10 loads, and three made none in 10.
 */
const FIRST_DRAWN = 3;

/** Records each background render adds above the ones drawn (`useDrawnFrom`). */
const CHUNK_DRAWN = 100;

/**
 * The smallest idx to draw of loaded records `first` to `last`: the newest
 * `FIRST_DRAWN` at once, then `CHUNK_DRAWN` more per background render React
 * can interrupt (https://react.dev/reference/react/startTransition), until all
 * are drawn, those Load earlier adds included. A record appended later draws at
 * once, and every record drawn stays drawn. A part of three records or fewer,
 * drawn whole at once, still marks them all drawn: else an append would leave
 * them out for a render and draw them afresh, losing what the reader opened.
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
    if (from > first) startTransition(() => setFrom(Math.max(first, start - CHUNK_DRAWN)));
  }, [from, start, first]);
  return start;
}

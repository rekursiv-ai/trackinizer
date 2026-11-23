import type { Graph } from "../api/graph";

/**
 * Grow `graph` again as it was made, as the design's replay does (`Trackinizer,
 * graph.html`'s `frame`): one sweep over the whole of the graph's life, `step`
 * getting how far through it the sweep has reached on every animation frame.
 *
 * The sweep's length barely changes with how many records there are, which is
 * the design's point and the reason for it: a timer per record, however brisk,
 * crosses 5,000 of them in minutes, and at that pace the frontier moves one
 * disc at a time over a field of thousands -- nothing reads as forming. Swept
 * instead, the newest records come up in a band that races through the graph.
 * `done` follows the last frame. Returns the function that stops it.
 */
export function replay(graph: Graph, { step, done }: { step: (reached: number) => void; done: () => void }): () => void {
  const count = graph.nodes.length;
  const ms = SWEEP_MS * Math.min(1, count / REFERENCE_NODES) + TAIL_MS;
  let progress = 0;
  let previous: number | null = null;
  let frame = 0;
  const tick = (now: number) => {
    // The first frame only fixes the clock: the sweep starts at the first record.
    const elapsed = previous === null ? 0 : now - previous;
    previous = now;
    progress = Math.min(1, progress + elapsed / ms);
    step(progress);
    if (progress >= 1) {
      done();
      return;
    }
    frame = requestAnimationFrame(tick);
  };
  frame = requestAnimationFrame(tick);
  return () => cancelAnimationFrame(frame);
}

/** The design's `14000*Math.min(1,n/2500)+4000`, in milliseconds: the sweep. */
const SWEEP_MS = 14_000;
/** The graph the design's sweep is its full length for; a smaller one is shorter. */
const REFERENCE_NODES = 2_500;
/** The design's `+4000`: what the sweep keeps over its proportional part. */
const TAIL_MS = 4_000;
